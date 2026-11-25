"""Controls and reconnection for the existing direct Production queue."""
import re
import time
import uuid
from http.client import HTTPException
from urllib.parse import quote

import production
from vast_credentials import identity
from workflow_profiles import runtime_requirements
from workflow_install_remote import redact, scan
from workflow_gate import verify_first_shot


def dependency_info(api, url, graph, deadline):
    try:
        return api(url, "/object_info")
    except (OSError, HTTPException):
        # A large registry response can reset a tunnel while smaller requests still work.
        pass
    info = {}
    for kind in sorted({node["class_type"] for node in graph.values()}):
        path = "/object_info/" + quote(kind, safe="")
        for attempt in range(2):
            if time.time() >= deadline:
                raise TimeoutError("ComfyUI dependency validation ran out of session time.")
            try:
                info.update(api(url, path))
                break
            except (OSError, HTTPException) as exc:
                if attempt:
                    raise RuntimeError("Could not read the ComfyUI schema for " + kind + ". Resume when the connection is ready.") from exc
                time.sleep(1)
    return info


class DirectSession:
    def __init__(self, ctx):
        self.ctx=ctx
        self.path=ctx["DATA"]/"production"/"direct-session.json"
        self.plan=production.read(self.path,{})
        self.interrupted=False
        if self.plan:
            view=self.plan["view"]
            if view["phase"] in ("Preparing ComfyUI","Rendering","Waiting for connection","Verifying original workflow","Verifying first shot"):
                self.interrupted=True
                view.update(phase="Needs attention",error="Resume to reconcile the saved shot before submitting another.")
            ctx["session"]=view

    def save(self):
        production.atomic(self.path,self.plan)

    def archive_for_new_session(self):
        if self.plan:
            archive=self.path.parent/"session-history"
            archive.mkdir(exist_ok=True)
            production.atomic(archive/(uuid.uuid4().hex+".json"),self.plan)
            production.atomic(self.path,{})
        self.plan={}
        self.interrupted=False
        self.ctx["session"]={}

    def resume_interrupted(self):
        with self.ctx["LOCK"]:
            if not self.interrupted or self.plan.get("control")!="run":
                return
            plan=self.plan
        view=plan["view"]
        connection=self.ctx["connection"]
        while time.time()<view["end"]-10:
            with self.ctx["LOCK"]:
                if self.plan is not plan or self.ctx["running"] or plan.get("control")!="run":
                    return
                if not self.ctx["workflow_manager"].busy():
                    try:
                        target=connection.target() if plan["instance"] else None
                    except ValueError:
                        target=None
                    if not plan["instance"] or target and identity(target)==plan["instance"]:
                        try:
                            workflow=self.ctx["actions"].store.get(plan["version"])
                        except ValueError:
                            return
                        url=connection.snapshot()["url"] if target else plan["url"]
                        if url:
                            self.interrupted=False
                            view["phase"]="Preparing ComfyUI"
                            view.pop("error",None)
                            self.save()
                            self.ctx["run_direct"](url,workflow,target)
                            return
                    elif plan["instance"]:
                        connection.retry_saved(plan["instance"])
            time.sleep(2)

    def start(self, view, jobs, workflow, target, url, retry=False, force_new=False):
        saved=self.plan.get("view",{})
        if not force_new and saved.get("phase") in ("Paused","Needs attention") and self.plan.get("index",0)<len(self.plan["jobs"]):
            raise ValueError("Resume the saved production queue or stop it before starting a new session.")
        self.plan=dict(id=uuid.uuid4().hex,view=view,jobs=jobs,version=workflow["version"],index=0,control="run",
                       instance=identity(target) if target else None,url=url if not target else None,
                       retry=retry,force_new=force_new,skip_current=False)
        self.interrupted=False
        view.update(rate=(target or {}).get("rate"),started=time.time())
        self.ctx["session"]=view
        self.save()

    def control(self, request):
        ctx=self.ctx
        action=request.get("action")
        if action=="regenerate":
            if ctx["running"] or ctx["workflow_manager"].busy():
                raise ValueError("Wait for the current production or installation to finish.")
            wf=ctx.get("active_workflow")
            if not wf:
                raise ValueError(ctx.get("workflow_error") or "Select a valid Workflow Package first.")
            shot_id=request.get("shot")
            try:
                valid_shot=isinstance(shot_id,str) and str(uuid.UUID(shot_id))==shot_id
            except (ValueError,TypeError,AttributeError):
                valid_shot=False
            if not valid_shot:
                raise ValueError("Select a valid shot to regenerate.")
            connection=ctx["connection"]
            connected=connection.snapshot().get("url")
            target=connection.target() if connected else None
            url=(connected or "").strip().rstrip("/")
            if not url:
                raise ValueError("Connect to ComfyUI before regenerating a shot.")
            db=ctx["project"]()
            ep=next((e for e in db["episodes"] if e["id"]==request.get("episode")),None)
            if not ep:
                raise ValueError("Episode not found.")
            jobs,skipped=ctx["actions"].approved_jobs(db,ep)
            chosen=[row for row in jobs if row[1]["id"]==shot_id]
            if not chosen:
                reason=next(iter(skipped.get(shot_id) or ()),"Approve this shot before regenerating.")
                raise ValueError(reason)
            minutes=int(request.get("minutes",60))
            if not 2<=minutes<=1440:
                raise ValueError("Choose a session of 2–1440 minutes.")
            view=dict(end=time.time()+60*minutes,episode=ep["id"],submitted=1,blocked={},
                      phase="Preparing ComfyUI",completed=0,failed=0)
            self.start(view,chosen,wf,target,url,retry=True,force_new=True)
            ctx["run_direct"](url,wf,target)
            return {"ok":True,"submitted":1,"blocked":{}}
        view=self.plan.get("view")
        if not view or request.get("episode")!=view["episode"]:
            raise ValueError("This episode has no saved direct production session.")
        if action=="skip":
            if self.plan["index"]>=len(self.plan["jobs"]):
                raise ValueError("No remaining shot to skip.")
            if ctx["running"]:
                self.plan["skip_current"]=True
                view["requestedAction"]="skip"
            else:
                self.plan["index"]+=1
                view["skipped"]=view.get("skipped",0)+1
                if self.plan["index"]>=len(self.plan["jobs"]):
                    view["phase"]="Finished"
            self.save()
            return {"ok":True}
        if action in ("pause","stop"):
            self.plan["control"]=action
            view["requestedAction"]=action
            if not ctx["running"]:
                view["phase"]="Paused" if action=="pause" else "Stopped"
            self.save()
            return {"ok":True}
        if action!="resume":
            raise ValueError("Unknown direct production control.")
        if ctx["running"] or ctx["workflow_manager"].busy():
            raise ValueError("Wait for the current production or installation.")
        if self.plan["index"]>=len(self.plan["jobs"]):
            raise ValueError("This queue is complete. Retry an individual failed shot.")
        connection=ctx["connection"]
        target=connection.target() if self.plan["instance"] else None
        if target and identity(target)!=self.plan["instance"]:
            raise ValueError("Connect the original instance to reconcile its queued shot.")
        wf=ctx["actions"].store.get(self.plan["version"])
        minutes=int(request.get("minutes",60))
        if not 2<=minutes<=1440:
            raise ValueError("Choose a session of 2–1440 minutes.")
        view.update(end=time.time()+60*minutes,phase="Preparing ComfyUI")
        view.pop("error",None)
        view.pop("requestedAction",None)
        view.pop("finished",None)
        self.plan.pop("validated",None)
        self.plan["control"]="run"
        self.interrupted=False
        self.save()
        ctx["run_direct"](connection.snapshot().get("url") if target else self.plan["url"],wf,target)
        return {"ok":True}

    def target_url(self, deadline, target, original):
        if not target:
            return original
        connection=self.ctx["connection"]
        instance=identity(target)
        while time.time()<deadline:
            with connection.lock:
                try:
                    selected=connection.target()
                    url=connection.snapshot().get("url")
                except ValueError:
                    url=None
                if url and identity(selected)==instance:
                    return url
                connection.retry_saved(instance)
            self.plan["view"]["phase"]="Waiting for connection"
            time.sleep(min(2,max(0,deadline-time.time())))
        raise TimeoutError("Connection unavailable before the session limit. Resume the same prompt.")

    def release_unavailable_video(self, path, job, target):
        instance=str(job.get("instance", ""))
        if not target or not instance.isdecimal() or instance==identity(target):
            return
        if not job.get("ticket") or job.get("status") not in ("waiting","queued","uncertain"):
            return
        history=production.read(path.parent/"history.json",{})
        status=history.get("status",{})
        if status.get("status_str")!="success" or status.get("completed") is not True:
            return
        rows=self.ctx["connection"].discover_instances()
        if any(str(row["id"])==instance for row in rows):
            return
        job.pop("artifact_pending",None)
        job.update(status="error",error="The render finished, but its video is on an instance no longer listed on Vast.")
        production.atomic(path,job)

    def run(self, url, wf, target=None):
        ctx=self.ctx
        view=self.plan["view"]
        try:
            deadline=view["end"]
            render_ctx={
                "DATA": ctx["DATA"],
                "prompt": ctx.get("prompt"),
                "generation_images": ctx.get("generation_images"),
                "generation_voices": ctx.get("generation_voices"),
                "technical_qc": ctx["take_quality"].check,
                "direct": True,
                "instance": identity(target) if target else None,
            }
            def on_current(fn):
                def call(_old_url,*args):
                    current=self.target_url(deadline,target,url)
                    view["phase"]="Verifying first shot" if self.plan.get("verifying") else "Rendering"
                    return fn(current,*args)
                return call
            render_ctx["json_api"]=on_current(ctx["json_api"])
            render_ctx["prepare_shot"]=on_current(ctx["prepare_shot"])
            render_ctx["download_output"]=on_current(ctx["download_output"])
            cancel_unsupported=set()
            def cancel_prompt(ticket):
                if (self.plan["control"]!="stop" and not self.plan.get("skip_current")) or ticket in cancel_unsupported:
                    return False
                if not re.fullmatch(r"[0-9a-f-]{36}",ticket):
                    return False
                try:
                    reply=render_ctx["json_api"](url,f"/api/jobs/{ticket}/cancel",{})
                except (OSError,HTTPException):
                    return False
                except ValueError:
                    # Older servers lack prompt-scoped cancellation. Never send a global interrupt.
                    cancel_unsupported.add(ticket)
                    return False
                if isinstance(reply,dict) and reply.get("cancelled") is True:
                    return True
                cancel_unsupported.add(ticket)
                return False
            render_ctx["cancel_prompt"]=cancel_prompt
            jobs=self.plan["jobs"]
            # An already-submitted attempt must be reconciled before dependency
            # validation. Reconnecting only changes the tunnel, not its owner.
            for i in range(self.plan["index"],len(jobs)):
                if self.plan["control"]!="run":
                    view["phase"]="Paused" if self.plan["control"]=="pause" else "Stopped"
                    return
                if time.time()>=deadline-10:
                    raise TimeoutError("Session time limit reached. Resume to reconcile outstanding work.")
                eid,shot=jobs[i][0],jobs[i][1]
                view.update(current=shot["id"],phase="Rendering")
                self.save()
                latest=next(iter(sorted((ctx["DATA"]/eid/shot["id"]).glob("*/job.json"),reverse=True)),None)
                prior=production.read(latest,{}) if latest else {}
                same_revision=prior.get("revision")==shot["revision"]
                if same_revision and self.plan["retry"]:
                    self.release_unavailable_video(latest,prior,target)
                in_flight=bool(latest and same_revision and production.pending_prompt(prior,latest.parent))
                if same_revision and prior.get("status")=="cancelled":
                    queued=render_ctx["json_api"](url,"/queue")
                    if any(row[1]==prior["ticket"] for row in queued["queue_running"]+queued["queue_pending"]):
                        raise RuntimeError("ComfyUI is still cancelling the original prompt. Resume after it leaves the queue.")
                wm=ctx.get("workflow_manager")
                inst_id=identity(target) if target else "local"
                if not in_flight and i==self.plan["index"] and not self.plan.get("validated"):
                    if wf.get("packaged") and target and wm and hasattr(wm,"verify_live_environment"):
                        verified_fp=wm.verify_live_environment(wf.get("version"),target,wf)
                        if verified_fp:
                            self.plan["verifiedEnvFingerprint"]=verified_fp
                    graph=runtime_requirements(ctx,{"jobs":jobs[i:]},wf)["graph"]
                    info=dependency_info(render_ctx["json_api"],url,graph,deadline)
                    if wf.get("packaged"):
                        missing_classes=sorted({n["class_type"] for n in graph.values()} - info.keys())
                        if missing_classes:
                            raise ValueError("Resolve the missing package node classes before production: "+", ".join(missing_classes))
                    else:
                        dependencies=scan(graph,info)
                        missing=dependencies["nodes"]+[m["name"] for m in dependencies["models"] if m["status"]!="Installed"]
                        if missing:
                            raise ValueError("Resolve the missing workflow dependencies before production: "+", ".join(missing))
                    self.plan["validated"]=True
                needs_gate=(
                    (wf.get("format")=="ComfyUI UI JSON" and i==0 and not self.plan.get("sampleVerified"))
                    or (wf.get("packaged") and i==0 and not self.plan.get("sampleVerified")
                        and not (wm and hasattr(wm,"is_production_ready")
                                 and wm.is_production_ready(wf.get("version"),inst_id,self.plan.get("verifiedEnvFingerprint"))))
                )
                if needs_gate:
                    self.plan.setdefault("id",uuid.uuid4().hex)
                    self.plan["verifying"]=True
                    view["phase"]="Verifying first shot"
                    self.save()
                    try:
                        result=verify_first_shot(render_ctx,url,deadline,self.plan["id"],wf,jobs[i],self.plan["retry"])
                    finally:
                        self.plan.pop("verifying",None)
                    if result=="done":
                        self.plan["sampleVerified"]=True
                else:
                    result=production.render(render_ctx,url,deadline,eid,shot,wf,2,lambda:None,self.plan["retry"],force_new=bool(self.plan.get("force_new")))
                if result=="done" and wf.get("packaged") and wm and hasattr(wm,"record_production_readiness"):
                    done_path=next(iter(sorted((ctx["DATA"]/eid/shot["id"]).glob("*/job.json"),reverse=True)),None)
                    done_job=production.read(done_path,{}) if done_path else {}
                    quality=done_job.get("quality") or {}
                    wm.record_production_readiness(
                        wf.get("version"),
                        inst_id,
                        {
                            "takeId": done_job.get("take_id"),
                            "shotId": shot["id"],
                            "sha256": done_job.get("sha256"),
                            "duration": done_job.get("duration"),
                            "width": quality.get("width"),
                            "height": quality.get("height"),
                            "frames": quality.get("frames"),
                            "technical": quality.get("technical"),
                        },
                        passed=quality.get("technical")=="passed" and bool(done_job.get("sha256")),
                        env_fingerprint=self.plan.get("verifiedEnvFingerprint"),
                    )
                if result in ("waiting","uncertain"):
                    raise RuntimeError("The original prompt is unresolved. Resume on the same instance to reconcile it.")
                if result=="cancelled":
                    if self.plan.pop("skip_current",False) and self.plan["control"]=="run":
                        view.pop("requestedAction",None)
                        view["skipped"]=view.get("skipped",0)+1
                        self.plan["index"]=i+1
                        self.save()
                        continue
                    view["phase"]="Stopped"
                    return
                if self.plan.pop("skip_current",False):
                    view.pop("requestedAction",None)
                if result=="error":
                    view["failed"]+=1
                if result=="done":
                    view["completed"]+=1
                self.plan["index"]=i+1
                self.save()
            if self.plan["control"]=="stop":
                view["phase"]="Stopped"
            else:
                view["phase"]="Finished" if not view["failed"] else "Needs attention"
        except Exception as exc:
            view.update(phase="Needs attention",error=redact(str(exc))[:900])
        finally:
            with ctx["LOCK"]:
                view.pop("current",None)
                view["finished"]=time.time()
                self.save()
                ctx["running"]=False
