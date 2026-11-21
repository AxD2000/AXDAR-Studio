import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

import editor_media as media
import episode_editor as e
import production


def tools_available():
    try:
        media.tool("ffmpeg")
        media.tool("ffprobe")
        return True
    except ValueError:
        return False


class TimelineTests(unittest.TestCase):
    def test_signal_alignment_retains_dialogue_and_trims_only_confirmed_edges(self):
        speech=[{"speaker":"A","text":"أغلق الباب."},{"speaker":"B","text":"I can hear you."}]
        signal={"silent":[[0,.7],[1.5,2.1],[3.5,4]],"frozen":[[0,.7],[3.5,4]]}
        inferred=e.alignment(speech,4,signal)
        self.assertEqual([c["method"] for c in inferred],["audio estimate"]*2)
        self.assertEqual(e.edge_trim({"dialog":True},4,signal,speech),(0,4))
        self.assertEqual(e.edge_trim({"sound":"room tone"},4,signal,[]),(0,4))
        start,end=e.edge_trim({},4,signal,[])
        self.assertAlmostEqual(start,.5)
        self.assertAlmostEqual(end,3.7)
        self.assertEqual(e.edge_trim({},4,{"silent":signal["silent"]},[]),(0,4))
        self.assertEqual(media.time_text(65.125),"00:01:05,125")

    def test_frame_roles_and_input_validation(self):
        self.assertEqual(e.output_options({"width":540,"height":960,"fps":30})["fps"],30)
        for broken in (dict(width=320,height=600),dict(font="Arial,Outline=0"),dict(fps=23.5)):
            with self.assertRaises(ValueError):
                e.output_options(broken)


@unittest.skipUnless(tools_available(),"Requires FFmpeg and ffprobe")
class RenderTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data=Path(tmp.name)
        self.ep=dict(id=e.uid(),title="Original script fixture",scenes=[],shots=[])
        self.line="الباب مفتوح."
        self.db=dict(id=e.uid(),episodes=[self.ep],characters=[])
        self.studio=e.Editor({"DATA":self.data,"LOCK":__import__("threading").RLock(),
                              "project":lambda:self.db,"actions":SimpleNamespace(check=lambda *_:None)})
        sample=self.data/"source.mp4"
        subprocess.run([media.tool("ffmpeg"),"-v","error","-f","lavfi","-i","testsrc2=s=320x180:r=24:d=2",
                        "-f","lavfi","-i","sine=frequency=440:duration=2","-c:v","libx264","-c:a","aac",
                        "-shortest",str(sample)],capture_output=True,check=True)
        self.hash=production.digest(sample)
        self.paths=[]
        # One shot has a second valid take; the second shot begins the next Scene.
        for i in range(2):
            sid=e.uid()
            scene=e.uid()
            self.ep["scenes"].append(dict(id=scene,title=f"Scene {i+1}"))
            self.ep["shots"].append(dict(id=sid,scene_id=scene,approved=True,seconds=2,
                                      action=("سامي: "+self.line) if not i else "Sami walks away.",sourceHeading=f"Shot {i+1}"))
            for v in range(1,3 if i==0 else 2):
                target=self.data/self.ep["id"]/sid/str(v)
                target.mkdir(parents=True)
                output=target/"video.mp4"
                shutil.copy2(sample,output)
                self.paths.append(output)
                production.atomic(target/"job.json",dict(shot=sid,take_id=e.uid(),status="done",sha256=self.hash,review="accepted"))

    def action(self,name,**args):
        ep=self.ep["id"]
        return self.studio.action({"episode":ep,"rev":self.studio.load(ep)["rev"],"action":name,**args})

    def finish(self,jid):
        deadline=time.monotonic()+50
        while time.monotonic()<deadline:
            job=self.action("poll",job=jid)
            if job["status"]!="rendering":
                self.assertEqual(job["status"],"done",job.get("message"))
                return self.studio.job_dir(self.ep["id"],jid),job
            time.sleep(.05)
        self.fail("The media render did not finish.")

    def test_render_approval_burned_original_subtitles_and_invalidation(self):
        first=self.action("assemble")
        id0,id1=[c["id"] for c in first["clips"]]
        self.assertEqual(first["clips"][0]["version"],"2")
        self.action("replace",clip=id0,version="1")
        with self.assertRaisesRegex(ValueError,"draft"):
            self.action("export")
        self.action("trim",clip=id0,start=.25,end=1.75,fade=.2)
        self.action("subtitle",clip=id0,line=0,start=.5,end=1.25)
        trimmed=self.action("split",clip=id0,at=1.5)
        self.assertAlmostEqual(trimmed["cues"][0]["start"],.25)
        extra=trimmed["clips"][1]["id"]
        self.action("remove",clip=extra)
        self.action("move",clip=id1,step=-1)
        self.action("move",clip=id1,step=1)
        self.action("options",options=dict(width=320,height=180,fps=24,burn=True))
        draft=self.action("draft")["draft"]
        folder,job=self.finish(draft["id"])
        self.assertEqual((job["media"]["width"],job["media"]["height"]),(320,180))
        self.assertIn(self.line,(folder/"subtitles.srt").read_text(encoding="utf8"))
        self.assertIn("00:00:00,250 --> 00:00:01,000",(folder/"subtitles.srt").read_text(encoding="utf8"))
        self.assertAlmostEqual(job["media"]["duration"],3.25,places=1)
        with self.assertRaisesRegex(ValueError,"Approve"):
            self.action("export")
        self.assertTrue(self.action("approve")["approved"])
        final=self.action("export",output=dict(width=180,height=320,fps=24))["finals"][0]
        _,verified=self.finish(final["id"])
        self.assertTrue(verified["media"]["audio"])
        self.assertEqual((verified["media"]["width"],verified["media"]["height"]),(180,320))
        self.action("flag",shot=self.ep["shots"][0]["id"],note="Check continuity")
        self.assertFalse(self.action("get")["approved"])
        self.assertTrue(all(production.digest(f)==self.hash for f in self.paths))
