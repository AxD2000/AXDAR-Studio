"""Persistent, non-destructive edit decisions backed by AXDAR's production Takes."""
import copy
import hashlib
import json
import math
import re
import threading
import time
import uuid
from pathlib import Path

import editor_media as media
import production
import story_engine

DEFAULTS=dict(width=1280,height=720,fps=24,fit="pad",cropX=.5,cropY=.5,normalize=True,
              burn=False,font="Arial",fontSize=24,placement="bottom",color="#FFFFFF",outline=2,margin=28)


def uid():
    return str(uuid.uuid4())


def valid_id(value):
    try:
        return isinstance(value,str) and str(uuid.UUID(value))==value
    except ValueError:
        return False


def number(value,lo,hi,label):
    if isinstance(value,bool):
        raise ValueError(f"Check {label}.")
    try:
        result=float(value)
    except (TypeError,ValueError):
        raise ValueError(f"Check {label}.") from None
    if not math.isfinite(result) or not lo<=result<=hi:
        raise ValueError(f"{label} must be between {lo} and {hi}.")
    return result


def integer(value,lo,hi,label):
    result=number(value,lo,hi,label)
    if not result.is_integer():
        raise ValueError(f"{label} must be a whole number.")
    return int(result)


def output_options(values):
    opts={**DEFAULTS,**values}
    opts["width"]=integer(opts["width"],144,3840,"Output width")
    opts["height"]=integer(opts["height"],144,3840,"Output height")
    w,h=opts["width"],opts["height"]
    if w%2 or h%2 or not (9*w==16*h or 16*w==9*h):
        raise ValueError("Use even dimensions with a 16:9 or 9:16 aspect ratio.")
    opts["fps"]=integer(opts["fps"],12,60,"Frame rate")
    opts["cropX"]=number(opts["cropX"],0,1,"Horizontal crop position")
    opts["cropY"]=number(opts["cropY"],0,1,"Vertical crop position")
    if opts["fit"] not in ("pad","crop") or opts["placement"] not in ("top","bottom"):
        raise ValueError("Choose a supported crop mode and subtitle placement.")
    if not isinstance(opts["font"],str) or not re.fullmatch(r"[\w ()-]{1,60}",opts["font"]):
        raise ValueError("Enter an installed font family name.")
    if not isinstance(opts["color"],str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}",opts["color"]):
        raise ValueError("Choose a valid subtitle color.")
    opts["fontSize"]=integer(opts["fontSize"],10,100,"Subtitle size")
    opts["outline"]=integer(opts["outline"],0,8,"Subtitle outline")
    opts["margin"]=integer(opts["margin"],0,100,"Subtitle margin")
    opts["burn"]=opts["burn"] is True
    opts["normalize"]=opts["normalize"] is not False
    return {key:opts[key] for key in DEFAULTS}


def ordered_shots(ep):
    order={scene["id"]:i for i,scene in enumerate(ep.get("scenes",[]))}
    return sorted(ep["shots"],key=lambda shot:order.get(shot.get("scene_id"),len(order)))


def subtract(duration,silent):
    cursor=0
    speech=[]
    for start,end in silent:
        if start>cursor+.04:
            speech.append([cursor,start])
        cursor=max(end,cursor)
    if duration>cursor+.04:
        speech.append([cursor,duration])
    return speech


def source_dialogue(shot,characters):
    if shot.get("source_dialogue_missing") and not shot.get("action"):
        return []
    if "action" in shot and not shot["action"] and shot.get("dialogue"):
        entries=shot["dialogue"]
    else:
        entries=story_engine.assignments(shot,characters)["dialogue"]
    return [{"speaker":d.get("speaker",""),"text":d["text"]} for d in entries if d.get("text","").strip()]


def alignment(dialogue,duration,analysis):
    spoken=subtract(duration,analysis.get("silent",[]))
    if dialogue and len(spoken)==len(dialogue):
        # Signal boundaries provide plausible cue positions. They do not identify words or speakers.
        return [{"start":max(0,a-.05),"end":min(duration,b+.08),"method":"audio estimate"}
                for a,b in spoken]
    start=spoken[0][0] if spoken else 0
    end=spoken[-1][1] if spoken else duration
    count=sum(max(1,len(d["text"].split())) for d in dialogue)
    cues=[]
    for line in dialogue:
        length=(end-start)*max(1,len(line["text"].split()))/count
        cues.append(dict(start=start,end=start+length,method="script estimate"))
        start+=length
    return cues


def edge_trim(shot,duration,analysis,dialogue):
    # Trim only detectable silent, frozen boundary frames from a shot without protected sound.
    if dialogue or any(shot.get(key) for key in ("dialog","sound","manual_audio","dialogue_context","source_dialogue_missing")):
        return 0,duration
    if any(Path(name).suffix.lower() in story_engine.VOICES for name in shot.get("refs",[])):
        return 0,duration
    start,end=0,duration
    for a,b in analysis.get("frozen",[]):
        for x,y in analysis.get("silent",[]):
            left,right=max(a,x),min(b,y)
            if .38<=right-left<=1.5:
                if left<.07:
                    start=max(start,min(.65,right-.2))
                if right>=duration-.07:
                    end=min(end,max(duration-.65,left+.2))
    return (start,end) if end-start>=.8 else (0,duration)


def sample_hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()


class Editor:
    def __init__(self,ctx):
        self.ctx=ctx
        self.lock=threading.RLock()
        self.active=set()
        self.probes={}

    @property
    def data(self):
        return self.ctx["DATA"]

    def directory(self,eid):
        if not valid_id(eid):
            raise ValueError("Select a valid episode.")
        return self.data/"editor"/eid

    def load(self,eid):
        return production.read(self.directory(eid)/"timeline.json",
                               {"clips":[],"options":dict(DEFAULTS),"subtitles":{},"flags":{},"rejections":{},"draft":None,"finals":[],"approval":None,"rev":""})

    def save(self,eid, timeline, edited=False):
        parent=self.directory(eid)
        parent.mkdir(parents=True,exist_ok=True)
        if edited:
            timeline["rev"]=uid()
            timeline["approval"]=None
        production.atomic(parent/"timeline.json",timeline)

    def archive_groups(self,db):
        episodes={ep["id"]:ep for ep in db["episodes"]}
        known={ep["id"]:{s["id"] for s in ep["shots"]} for ep in db["episodes"]}
        groups={}
        for job in self.data.glob("*/*/*/job.json"):
            eid,sid=job.parents[2].name,job.parents[1].name
            if (eid in known and sid in known[eid]) or not valid_id(eid) or not valid_id(sid) or not (job.parent/"video.mp4").is_file():
                continue
            record=production.read(job,{})
            if record.get("status")=="done" and record.get("review") in (None,"accepted"):
                entry=groups.setdefault(eid,{"episode":eid,"shots":set(),"takes":0,
                    "title":episodes[eid]["title"] if eid in episodes else "Archived episode "+eid[:8]})
                entry["shots"].add(sid)
                entry["takes"]+=1
        return [{**row,"shots":len(row["shots"])} for row in groups.values()]

    def recover(self,db,eid):
        group=next((row for row in self.archive_groups(db) if row["episode"]==eid),None)
        if not group:
            raise ValueError("No completed archived Takes are available for this episode.")
        ep=next((e for e in db["episodes"] if e["id"]==eid),None)
        if ep is None:
            ep=story_engine.new_episode(group["title"])
            ep["id"]=eid
            db["episodes"].append(ep)
        existing={s["id"] for s in ep["shots"]}
        jobs={}
        for file in sorted((self.data/eid).glob("*/*/job.json")):
            sid=file.parents[1].name
            j=production.read(file,{})
            if valid_id(sid) and sid not in existing and j.get("status")=="done" and j.get("review") in (None,"accepted") and (file.parent/"video.mp4").is_file():
                jobs[sid]=j
        def index(row):
            head=row[1].get("settings",{}).get("sourceHeading","")
            label=re.search(r"(?i)(?:shot|SH|لقطة)\s*0*(\d+)",head)
            return int(label[1]) if label else 100000
        scenes={}
        for sid,j in sorted(jobs.items(),key=index):
            shot=copy.deepcopy(j.get("settings",{}))
            title_match=re.search(r"^(?:Scene|المشهد)\s+(\d+)",shot.get("prompt",""),re.I)
            title=f"Recovered Scene {title_match[1]}" if title_match else "Recovered archive"
            if title not in scenes:
                scenes[title]=story_engine.new_scene(title)
                ep["scenes"].append(scenes[title])
            has_source=bool(shot.get("action") or shot.get("dialogue"))
            shot.update(id=sid,scene_id=scenes[title]["id"],takes=[],revision=j.get("revision") or uid(),
                        source_dialogue_missing=bool(shot.get("dialog")) and not has_source)
            shot.setdefault("approved",True)
            shot.setdefault("action","")
            shot.setdefault("refs",[])
            shot.setdefault("image","")
            shot.setdefault("change","")
            shot.setdefault("dialog",False)
            shot.setdefault("prompt","")
            shot.setdefault("seconds",j.get("duration",5))
            ep["shots"].append(shot)
        story_engine.sync_takes(db,self.data)
        # These jobs now belong to restored shots, with their recovered stable take IDs.
        restored={s["id"] for s in ep["shots"]}
        db["archived_takes"]=[t for t in db.get("archived_takes",[]) if not (t["episode_id"]==eid and t["shot_id"] in restored)]
        self.ctx["actions"].commit(db)
        return {"project":db,"episode":eid}

    def takes(self,eid,sid,rejections=None):
        if not valid_id(sid):
            return []
        rows=[]
        for file in reversed(sorted((self.data/eid/sid).glob("*/job.json"))):
            version=file.parent.name
            if not re.fullmatch(r"\d{1,22}",version):
                continue
            j=production.read(file,{})
            if j.get("status")!="done":
                continue
            path=file.parent/"video.mp4"
            ready=path.is_file() and j.get("review") in ("accepted",None) and j.get("quality",{}).get("technical")!="failed"
            reason=""
            info={}
            rejected=version in (rejections or {}).get(sid,[])
            if rejected:
                ready=False
                reason="Rejected for this edit. Restore the Take if you want to use it again."
            elif ready:
                try:
                    stat=path.stat()
                    info=media.probe(path)
                    key=(str(path),stat.st_size,stat.st_mtime_ns)
                    if key not in self.probes:
                        self.probes[key]=production.digest(path)
                    sha=self.probes[key]
                    if j.get("sha256") and sha!=j["sha256"]:
                        raise ValueError("The take no longer matches the recorded checksum.")
                    if j.get("audioRequired") and not info["audio"]:
                        raise ValueError("Required audio is missing.")
                except (ValueError,OSError) as exc:
                    ready=False
                    reason=str(exc)
            elif not path.is_file():
                reason="Video missing locally. Download the accepted Take from Production."
            else:
                reason="Take rejected or awaiting approval in Production."
            rows.append(dict(version=version,id=j.get("take_id") or version,ready=ready,review=j.get("review"),
                             issue=reason,media=info,sha=sha if ready else "",path=path,editorRejected=rejected,
                             visibleIds=j.get("settings",{}).get("visible_ids"),
                             video=f"/video/{eid}/{sid}/{version}" if ready else ""))
        return sorted(rows,key=lambda row:(row["ready"],row["review"]=="accepted"),reverse=True)

    @staticmethod
    def clip(sid,take):
        return dict(id=uid(),shot=sid,version=take["version"],start=0,end=take["media"]["duration"],fade=0)

    def compile(self,db,ep,timeline,opts=None,scan=False):
        opts=opts or timeline["options"]
        eid=ep["id"]
        shots={s["id"]:s for s in ep["shots"]}
        order={s["id"]:i for i,s in enumerate(ordered_shots(ep))}
        issues=[]
        def issue(message,shot="",blocking=False):
            issues.append(dict(message=message,shot=shot,blocking=blocking))
        clips,cues,subtitle_sources=[],[],{}
        all_approved=[s for s in ordered_shots(ep) if s.get("approved")]
        scope=timeline.get("scope")
        planned=[s for s in all_approved if scope is None or s["id"] in scope]
        takes={s["id"]:self.takes(eid,s["id"],timeline.get("rejections")) for s in all_approved}
        if scope is not None:
            omitted=len(all_approved)-len(planned)
            issue(f"This review cut intentionally excludes {omitted} approved shot(s). Assemble the full first cut to review the complete episode.")
        active_shots=set()
        source_details={}
        offsets={}
        offset=0
        last_order=-1
        for item in timeline["clips"]:
            sid=item["shot"]
            shot=shots.get(sid)
            take=next((t for t in takes.get(sid,[]) if t["version"]==item["version"]),None)
            if not shot or not shot.get("approved") or scope is not None and sid not in scope:
                issue("This timeline shot needs production approval and inclusion in the edit.",sid,True)
                continue
            if not take or not take["ready"]:
                issue(take["issue"] if take else "The selected take is missing. Choose another take.",sid,True)
                continue
            info=take["media"]
            duration=info["duration"]
            start,end=item["start"],item["end"]
            if start<0 or end>duration+.015 or end-start<1/opts["fps"]:
                issue("Trim points are outside this take or shorter than one output frame.",sid,True)
                continue
            end=min(end,duration)
            length=max(1,round((end-start)*opts["fps"]))/opts["fps"]
            end=min(duration,start+length)
            fade=item["fade"]
            offsets[item["id"]]=offset
            clips.append(dict(item,scene=shot["scene_id"],path=take["path"],media=info,sha=take["sha"],
                              length=length,fade=fade,start=start,end=end))
            if order[sid]<last_order and sid!=clips[-2]["shot"]:
                issue("The timeline changes the planned Scene/Shot order. Review continuity.",sid)
            last_order=order[sid]
            if sid not in active_shots:
                if abs(duration-shot.get("seconds",duration))>1:
                    issue("The source duration differs from the planned shot by more than one second.",sid)
                if expected_missing_dialogue(shot,db):
                    issue("Original dialogue was not located for this shot. Add it in Shot Lab if available.",sid)
                if shot.get("dialog") and not info["audio"]:
                    issue("Potential missing dialogue audio. Inspect this take.",sid)
                if take.get("visibleIds") is not None and shot.get("visible_ids") is not None and set(take["visibleIds"])!=set(shot["visible_ids"]):
                    issue("The Take's frozen character assignments differ from the shot's current cast. Review visual continuity.",sid)
                if info["width"]*opts["height"]!=info["height"]*opts["width"]:
                    hint="Review the crop in the program monitor." if opts["fit"]=="crop" else "The source aspect ratio differs. Padding preserves the full frame."
                    issue(hint,sid)
            active_shots.add(sid)
            key=sid+":"+item["version"]
            if key not in source_details:
                original=source_dialogue(shot,db.get("characters",[]))
                analysis={}
                if scan or original:
                    try:
                        analysis=media.analyze(take["path"],info)
                    except (ValueError,OSError):
                        issue("Audio or idle analysis was inconclusive. Check the original segment.",sid)
                source_details[key]=(original,analysis)
                if scan and any(abs(left-start)<.1 or abs(right-end)<.1 for left,right in analysis.get("silent",[])):
                    issue("Silence near a cut. Preview the audio transition and preserve intentional pauses.",sid)
            dialogue,analysis=source_details[key]
            initial=alignment(dialogue,duration,analysis)
            override=timeline["subtitles"].get(key,{})
            subtitle_sources[key]=[]
            for i,(line,cue) in enumerate(zip(dialogue,initial,strict=True)):
                explicit=override.get(str(i))
                if explicit and explicit["text"]==line["text"]:
                    cue=dict(start=explicit["start"],end=explicit["end"],method="manual")
                subtitle_sources[key].append({**cue,**line,"duration":duration})
                left,right=max(start,cue["start"]),min(end,cue["end"])
                if right-left>=.015:
                    cues.append(dict(line,start=offset+left-start,end=offset+right-start,shot=sid))
            offset+=length
        for s in planned:
            if s["id"] not in active_shots:
                issue("Approved shot is absent from the timeline. Choose a completed Take.",s["id"],True)
        for i,c in enumerate(clips):
            if i+1==len(clips):
                c["fade"]=0
            elif c["fade"]*2>min(c["length"],clips[i+1]["length"]):
                issue("The fade is too long for a neighboring clip.",c["shot"],True)
            if i and c["media"]["audio"]!=clips[i-1]["media"]["audio"]:
                issue("Audio is present on only one side of this cut. Listen for a discontinuity.",c["shot"])
            if i and c["media"]["fps"]!=clips[i-1]["media"]["fps"]:
                issue("Source frame rates differ. AXDAR conforms both to the selected output rate.",c["shot"])
            if scan and i and c["media"]["audio"] and clips[i-1]["media"]["audio"] and not clips[i-1]["fade"]:
                previous=clips[i-1]
                try:
                    outgoing=media.sound_level(previous["path"],previous["media"],max(previous["start"],previous["end"]-.18),.18)
                    incoming=media.sound_level(c["path"],c["media"],c["start"],min(.18,c["length"]))
                    if outgoing is not None and incoming is not None and abs(outgoing-incoming)>=12:
                        issue(f"Audio level jumps by about {abs(outgoing-incoming):.0f} dB across this cut. Audition it before approval.",c["shot"])
                except (OSError,ValueError):
                    issue("The audio level at this cut could not be compared.",c["shot"])
        for sid,note in timeline["flags"].items():
            issue("Regeneration requested. "+note,sid,True)
        cues.sort(key=lambda cue:(cue["start"],cue["end"]))
        signature=sample_hash({"edit":timeline["rev"],"media":[(c["id"],c["sha"],c["scene"]) for c in clips],
                               "approved":[s["id"] for s in planned],"options":opts,"cues":cues})
        return dict(clips=clips,cues=cues,duration=offset,takes=takes,issues=issues,signature=signature,
                    subtitles=subtitle_sources,offsets=offsets)

    def assemble(self,db,ep,timeline,available=False):
        result=[]
        notes=[]
        for shot in ordered_shots(ep):
            if not shot.get("approved"):
                continue
            take=next((row for row in self.takes(ep["id"],shot["id"],timeline.get("rejections")) if row["ready"]),None)
            if not take:
                continue
            clip=self.clip(shot["id"],take)
            try:
                analysis=media.analyze(take["path"],take["media"])
                dialogue=source_dialogue(shot,db.get("characters",[]))
                clip["start"],clip["end"]=edge_trim(shot,clip["end"],analysis,dialogue)
                if clip["start"]>0 or clip["end"]<take["media"]["duration"]:
                    notes.append(dict(shot=shot["id"],message="Trimmed confirmed quiet, frozen frames at the shot edge."))
                elif analysis.get("silent") or analysis.get("frozen"):
                    notes.append(dict(shot=shot["id"],message="Retained quiet or static footage; review whether the pause is intentional."))
            except (ValueError,OSError):
                notes.append(dict(shot=shot["id"],message="Kept the full source because automatic idle analysis was inconclusive."))
            result.append(clip)
        if not result:
            raise ValueError("No completed Takes for approved shots. Review Production or recover an archived episode.")
        timeline.update(clips=result,analysis=notes,scope=[c["shot"] for c in result] if available else None)
        self.save(ep["id"],timeline,edited=True)

    def get_job(self,eid,jid):
        if not jid:
            return None
        folder=self.job_dir(eid,jid)
        with self.lock:
            job=production.read(folder/"status.json",{})
            active=jid in self.active
        if not job:
            return None
        if job.get("status")=="rendering" and not active:
            job=dict(job,status="error",message="Rendering was interrupted. Generate a new draft or export again.")
        return job

    def job_dir(self,eid,jid):
        if not valid_id(jid):
            raise ValueError("Invalid editor job.")
        return self.directory(eid)/"renders"/jid

    def public(self,db,ep,timeline):
        plan=self.compile(db,ep,timeline)
        def small(t):
            return {k:v for k,v in t.items() if k not in ("path","sha")}
        draft=self.get_job(ep["id"],timeline.get("draft"))
        finals=[j for jid in reversed(timeline["finals"]) if (j:=self.get_job(ep["id"],jid))]
        approval=timeline.get("approval")
        approved=bool(approval and approval["signature"]==plan["signature"] and not any(x["blocking"] for x in plan["issues"]))
        shots=[{"id":s["id"],"title":s.get("sourceHeading") or f"Shot {i+1}","scene":s.get("scene_id"),
                "takes":[small(t) for t in plan["takes"].get(s["id"],[])]}
               for i,s in enumerate(ordered_shots(ep)) if s.get("approved")]
        return {"episode":ep["id"],"rev":timeline["rev"],"clips":timeline["clips"],"scenes":ep.get("scenes",[]),
                "shots":shots,"issues":[*plan["issues"],*timeline.get("analysis",[])],"cues":plan["cues"],"subtitleSources":plan["subtitles"],
                "flags":timeline["flags"],"duration":plan["duration"],"offsets":plan["offsets"],"options":timeline["options"],
                "draft":draft,"finals":finals,
                "currentDraft":bool(draft and draft["status"]=="done" and draft["signature"]==plan["signature"]),
                "approved":approved,"reviewCut":timeline.get("scope") is not None,"archives":self.archive_groups(db)}

    def launch(self,ep,timeline,plan,opts,kind):
        if any(job and job["status"]=="rendering" for job in
               [self.get_job(ep["id"],timeline.get("draft")),*(self.get_job(ep["id"],i) for i in timeline["finals"])]):
            raise ValueError("Wait for this episode's current render to finish.")
        if not plan["clips"]:
            raise ValueError("Add ready takes to the timeline.")
        media.tool("ffmpeg")
        media.tool("ffprobe")
        jid=uid()
        folder=self.job_dir(ep["id"],jid)
        folder.mkdir(parents=True)
        status=dict(id=jid,kind=kind,status="rendering",progress=0,message="Starting FFmpeg",
                    signature=plan["signature"],updated=time.time(),options=opts,reviewCut=timeline.get("scope") is not None)
        self.active.add(jid)
        production.atomic(folder/"status.json",status)
        if kind=="draft":
            timeline["draft"]=jid
        else:
            timeline["finals"].append(jid)
        self.save(ep["id"],timeline)
        def worker():
            last=-1
            def update(progress,message):
                nonlocal last
                if int(progress)>last:
                    last=int(progress)
                    status.update(progress=round(progress,1),message=message,updated=time.time())
                    with self.lock:
                        production.atomic(folder/"status.json",status)
            try:
                info=media.render(plan["clips"],plan["cues"],opts,folder,update)
                base=f"/editor/files/{ep['id']}/{jid}"
                status.update(status="done",progress=100,message="Verified video and audio",
                              media=info,video=base+"/video.mp4",srt=base+"/subtitles.srt",download=base+"/download",
                              track=base+"/subtitles.vtt" if plan["cues"] and not opts["burn"] else "")
            except Exception as exc:
                status.update(status="error",message=str(exc)[:1300])
            finally:
                with self.lock:
                    try:
                        production.atomic(folder/"status.json",status)
                    finally:
                        self.active.discard(jid)
        threading.Thread(target=worker,daemon=True,name="AXDAR episode renderer").start()

    def action(self,obj):
        action=obj["action"]
        eid=obj.get("episode")
        with self.ctx["LOCK"],self.lock:
            db=self.ctx["project"]()
            self.ctx["actions"].check(db,{})
            if action=="recover":
                return self.recover(db,eid)
            ep=next((e for e in db["episodes"] if e["id"]==eid),None)
            if not ep:
                raise ValueError("Select an episode in AXDAR.")
            timeline=self.load(eid)
            if action=="get":
                return self.public(db,ep,timeline)
            if action=="poll":
                return self.get_job(eid,obj.get("job")) or {}
            if obj.get("rev")!=timeline["rev"]:
                raise ValueError("The edit changed in another tab. Reload the editor.")
            if action=="assemble":
                self.assemble(db,ep,timeline,available=obj.get("available") is True)
            elif action in ("draft","approve","export"):
                plan=self.compile(db,ep,timeline,scan=action=="draft")
                if action=="draft":
                    timeline["analysis"]=list({(i["shot"],i["message"]):i for i in
                        [*timeline.get("analysis",[]),*(i for i in plan["issues"] if not i["blocking"])]}.values())
                    self.launch(ep,timeline,plan,timeline["options"],"draft")
                else:
                    draft=self.get_job(eid,timeline["draft"])
                    if not draft or draft["status"]!="done" or draft["signature"]!=plan["signature"]:
                        raise ValueError("Generate and review a draft of the current edit before approval or export.")
                    if any(i["blocking"] for i in plan["issues"]):
                        raise ValueError("Resolve unfinished shots and regeneration flags before approval or export.")
                    if action=="approve":
                        timeline["approval"]=dict(signature=plan["signature"],time=time.time())
                        self.save(eid,timeline)
                    elif (timeline.get("approval") or {}).get("signature")!=plan["signature"]:
                        raise ValueError("Approve the current draft before exporting.")
                    else:
                        opts=output_options({**timeline["options"],**obj.get("output",{})})
                        allowed={"width","height","fps"}
                        if any(opts[k]!=timeline["options"][k] for k in DEFAULTS if k not in allowed):
                            raise ValueError("Preview and approve changes to crop, audio and subtitle settings first.")
                        previous=timeline["options"]
                        if opts["fit"]=="crop" and opts["width"]*previous["height"]!=opts["height"]*previous["width"]:
                            raise ValueError("Changing the aspect ratio changes the crop. Create and approve a new draft.")
                        export_plan=self.compile(db,ep,timeline,opts)
                        if any(i["blocking"] for i in export_plan["issues"]):
                            raise ValueError("The chosen output frame rate does not fit the approved cuts.")
                        self.launch(ep,timeline,export_plan,opts,"final")
            else:
                self.edit(db,ep,timeline,obj)
                self.save(eid,timeline,edited=True)
            return self.public(db,ep,timeline)

    def edit(self,db,ep,timeline,obj):
        action=obj["action"]
        clips=timeline["clips"]
        if action=="options":
            timeline["options"]=output_options(obj["options"])
            return
        sid=obj.get("shot")
        if action in ("add","flag","reject","restore"):
            if not any(s["id"]==sid and s.get("approved") for s in ep["shots"]):
                raise ValueError("Select an approved shot in this episode.")
            if action=="flag":
                if obj.get("clear"):
                    timeline["flags"].pop(sid,None)
                else:
                    timeline["flags"][sid]=str(obj.get("note",""))[:600]
            elif action=="add":
                take=self.required_take(ep["id"],sid,obj.get("version"),timeline)
                clips.append(self.clip(sid,take))
                if timeline.get("scope") is not None and sid not in timeline["scope"]:
                    timeline["scope"].append(sid)
            else:
                version=obj.get("version")
                existing=self.takes(ep["id"],sid,timeline.get("rejections"))
                if not any(t["version"]==version for t in existing):
                    raise ValueError("Choose an existing Take to review.")
                values=set(timeline.setdefault("rejections",{}).get(sid,[]))
                if action=="reject":
                    values.add(version)
                else:
                    values.discard(version)
                timeline["rejections"][sid]=sorted(values)
            return
        clip=next((c for c in clips if c["id"]==obj.get("clip")),None)
        if not clip:
            raise ValueError("Select a clip in the timeline.")
        index=clips.index(clip)
        sid=clip["shot"]
        if action=="remove":
            clips.pop(index)
        elif action=="move":
            step=obj.get("step")
            if type(step) is not int or step not in (-1,1) or not 0<=index+step<len(clips):
                raise ValueError("Choose an available neighboring position.")
            clips[index],clips[index+step]=clips[index+step],clip
        elif action=="replace":
            take=self.required_take(ep["id"],sid,obj.get("version"),timeline)
            clip.update(version=take["version"],start=0,end=take["media"]["duration"],fade=0)
        elif action in ("trim","split","subtitle"):
            take=self.required_take(ep["id"],sid,clip["version"],timeline)
            duration=take["media"]["duration"]
            frame=1/timeline["options"]["fps"]
            if action=="trim":
                left=number(obj.get("start"),0,duration,"Trim start")
                right=number(obj.get("end"),left+frame,duration+.0005,"Trim end")
                clip.update(start=left,end=min(right,duration),fade=number(obj.get("fade",0),0,2,"Fade duration"))
            elif action=="split":
                at=number(obj.get("at"),clip["start"]+frame,clip["end"]-frame,"Split position")
                second=dict(clip,id=uid(),start=at)
                clip.update(end=at,fade=0)
                clips.insert(index+1,second)
            else:
                shot=next(s for s in ep["shots"] if s["id"]==sid)
                dialogue=source_dialogue(shot,db.get("characters",[]))
                i=integer(obj.get("line"),0,len(dialogue)-1,"Subtitle line")
                start=number(obj.get("start"),0,duration,"Subtitle start")
                end=number(obj.get("end"),start+.015,duration+.0005,"Subtitle end")
                key=sid+":"+clip["version"]
                timeline["subtitles"].setdefault(key,{})[str(i)]=dict(start=start,end=min(end,duration),text=dialogue[i]["text"])
        else:
            raise ValueError("Unknown episode editing operation.")

    def required_take(self,eid,sid,version,timeline):
        match=next((t for t in self.takes(eid,sid,timeline.get("rejections")) if t["version"]==version),None)
        if not match or not match["ready"]:
            raise ValueError("Choose a technically valid and accepted local take.")
        return match


def expected_missing_dialogue(shot,db):
    return (shot.get("dialog") or shot.get("source_dialogue_missing")) and not source_dialogue(shot,db.get("characters",[]))
