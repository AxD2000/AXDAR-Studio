import json
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import story_engine as se


class MigrationTests(unittest.TestCase):
    def test_explicit_backup_idempotence_and_take_discovery(self):
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            file = folder/"project.json"
            eid, sid = se.uid(), se.uid()
            old = {"episodes":[{"id":eid, "title":"Original", "story":"old story", "shots":[
                {"id":sid, "revision":"keep", "prompt":"", "image":"cat.png","refs":["cat.wav"],"approved":True}
            ]}], "rules":[], "other":{"field":1}}
            original = json.dumps(old,indent=4).encode()
            file.write_bytes(original)
            record = folder/eid/sid/"1"/"job.json"
            record.parent.mkdir(parents=True)
            record.write_text('{"status":"complete"}')
            orphan = folder/eid/"deleted-shot"/"2"/"job.json"
            orphan.parent.mkdir(parents=True)
            orphan.write_text("{}")
            preview = se.migration_preview(old,folder)
            self.assertTrue(preview["required"])
            self.assertEqual(preview["counts"]["takes"],2)
            self.assertEqual(file.read_bytes(),original)
            with self.assertRaisesRegex(ValueError,"changed"):
                se.migrate(file,folder,"stale")
            self.assertEqual(len(list(folder.glob("*pre-story*"))),0)
            result=se.migrate(file,folder,preview["token"])
            db=result["project"]
            self.assertEqual(Path(result["backup"]).read_bytes(),original)
            self.assertEqual(db["other"],old["other"])
            episode=db["episodes"][0]
            shot=episode["shots"][0]
            self.assertEqual({k:shot[k] for k in old["episodes"][0]["shots"][0]},old["episodes"][0]["shots"][0])
            self.assertEqual(shot["scene_id"],episode["scenes"][0]["id"])
            self.assertEqual(shot["takes"][0]["job"],f"{eid}/{sid}/1/job.json")
            self.assertTrue(db["archived_takes"][0]["id"])
            self.assertFalse(se.sync_takes(db,folder))
            sibling=record.parent.with_name("2")/"job.json"
            sibling.parent.mkdir();sibling.write_text("{}")
            self.assertTrue(se.sync_takes(db,folder))
            self.assertEqual(len({t["id"] for t in shot["takes"]}),2)
            self.assertIsNone(se.migrate(file,folder,"ignored")["backup"])
            self.assertEqual(record.read_text(),'{"status":"complete"}')

    def test_failed_write_keeps_old_project_and_exact_backup(self):
        with TemporaryDirectory() as temp:
            folder=Path(temp); file=folder/"project.json"
            old={"episodes":[],"rules":[]}
            raw=json.dumps(old).encode();file.write_bytes(raw)
            token=se.migration_preview(old,folder)["token"]
            with patch("story_engine.os.replace",side_effect=OSError("file locked")):
                with self.assertRaisesRegex(OSError,"locked"):
                    se.migrate(file,folder,token)
            self.assertEqual(file.read_bytes(),raw)
            self.assertEqual(next(folder.glob("*pre-story*")).read_bytes(),raw)
            self.assertFalse(list(folder.glob("*.tmp")))


def minimal_pdf():
    content=b"BT /F1 12 Tf 100 700 Td (Scene 1 - Home) Tj 0 -14 Td (A clock ticks.) Tj ET"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length "+str(len(content)).encode()+b" >>\nstream\n"+content+b"\nendstream"]
    raw=b"%PDF-1.4\n"; positions=[0]
    for i, obj in enumerate(objects,1):
        positions.append(len(raw));raw += str(i).encode()+b" 0 obj\n"+obj+b"\nendobj\n"
    offset=len(raw)
    raw+=b"xref\n0 6\n0000000000 65535 f \n"+b"".join(f"{p:010} 00000 n \n".encode() for p in positions[1:])
    return raw+b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"+str(offset).encode()+b"\n%%EOF"


class ImportTests(unittest.TestCase):
    def test_docx_txt_and_errors(self):
        out=BytesIO()
        with ZipFile(out,"w") as z:
            z.writestr("word/document.xml",'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>مشهد ١</w:t><w:br/><w:t>النافذة مفتوحة.</w:t></w:r></w:p></w:body></w:document>')
        self.assertEqual(se.extract_story("in.docx",out.getvalue()),"مشهد ١\nالنافذة مفتوحة.")
        self.assertEqual(se.extract_story("in.txt",b"\xef\xbb\xbfabc\r\n"),"abc\r\n")
        for filename,data,msg in [("in.docx",b"bad","DOCX"),("in.txt",b"\xff","UTF-8"),("in.txt",b" ","extractable"),
                                  ("in.pdf",b"%PDF-fake","PDF"),("x.exe",b"x","Choose")]:
            with self.subTest(filename=filename), self.assertRaisesRegex(ValueError,msg):
                se.extract_story(filename,data)

    def test_pdf_text(self):
        try:
            import pypdf
        except ImportError:
            self.skipTest("Install project PDF dependency pypdf to exercise PDF extraction")
        text=se.extract_story("story.pdf",minimal_pdf())
        self.assertIn("A clock ticks.",text)
        self.assertEqual(len(se.parse_story(text)["scenes"]),1)

    def test_mixed_labels_boundaries_original_and_duplicate_safety(self):
        raw="## Scene 1 - Street\r\nEP1 · SH01 | Look\r\nA light blinks.\r\nShot 2\r\nAn engine starts.\r\nالمشهد ٢ | المنزل\r\nيفتح الباب. يرن الهاتف.\r\n"
        proposal=se.parse_story(raw)
        self.assertEqual([len(sc["shots"]) for sc in proposal["scenes"]],[2,2])
        self.assertEqual(proposal["original"],raw)
        ep=se.new_episode(); db=se.new_project(); db["episodes"]=[ep]
        se.apply_story(db,ep["id"],proposal)
        self.assertEqual(ep["story"],raw)
        self.assertEqual(ep["story_imports"][0]["original"],raw)
        self.assertEqual(ep["shots"][1]["scene_id"],ep["scenes"][0]["id"])
        self.assertTrue(all(not s["approved"] for s in ep["shots"]))
        old=json.dumps(ep)
        with self.assertRaisesRegex(ValueError,"duplicate"):
            se.apply_story(db,ep["id"],proposal)
        self.assertEqual(json.dumps(ep),old)
        more=se.parse_story("INT. HALL - DAY\nLight flickers.")
        se.apply_story(db,ep["id"],more)
        self.assertEqual(ep["story"],raw)
        self.assertEqual(ep["story_imports"][1]["original"],more["original"])
        with self.assertRaisesRegex(ValueError,"Missing shot"):
            se.parse_story("Scene 1\nShot 1\nShot 2\nDoor opens.")


    def test_edited_proposal_retains_source_and_rejects_invalid_shot_fields(self):
        source = "Scene 1: Hall\nTom waits."
        draft = se.parse_story(source)
        draft["filename"] = "first draft.txt"
        scene, = draft["scenes"]
        shot, = scene["shots"]
        scene["title"] = "At the station"
        shot["action"] = "Tom waits at the station and studies the timetable."
        shot.pop("seconds")
        shot.pop("prompt")
        ep = se.new_episode()
        db = se.new_project()
        db["episodes"] = [ep]
        for key,value in (("seconds",-3),("manual_images",["../outside.png"]),("overrides",{"visible_ids":["unknown"]})):
            shot[key] = value
            with self.assertRaises(ValueError):
                se.apply_story(db,ep["id"],draft,["station.png"])
            self.assertEqual(ep["scenes"],[])
            self.assertEqual(ep["shots"],[])
            shot.pop(key)
        shot["manual_images"] = ["station.png"]
        se.apply_story(db,ep["id"],draft,["station.png"])
        self.assertEqual(ep["story_imports"][0]["original"],source)
        self.assertEqual(ep["story_imports"][0]["filename"],"first draft.txt")
        self.assertEqual(ep["scenes"][0]["title"],"At the station")
        self.assertEqual(ep["shots"][0]["action"],shot["action"])
        self.assertEqual(ep["shots"][0]["prompt"],shot["action"])
        self.assertEqual(ep["shots"][0]["manual_images"],["station.png"])


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.assets=["ava.png","ava.wav","sam.jpg","sam.mp3","clock.jpg"]
        self.db=se.new_project()
        self.ep=se.new_episode();self.db["episodes"]=[self.ep]
        self.ava=self.character("Ava",["آفا","Artist"],["ava.png"],["ava.wav"])
        self.sam=self.character("Sam",["سام","Artist"],["sam.jpg"],["sam.mp3"])

    def character(self,name,aliases,images,voices):
        return se.save_character(self.db,dict(name=name,aliases=aliases,images=images,voices=voices,
                                              description=name+" description"),self.assets)

    def make_shot(self,text):
        p=se.parse_story(text,self.db["characters"])
        se.apply_story(self.db,self.ep["id"],p)
        return self.ep["shots"][-1]

    def preview(self,s,**limits):
        return se.preview_shot(self.db,self.ep,s,self.assets,limits)

    def edit(self,s,**changes):
        return se.update_shot(self.db,self.ep,s["id"],changes,self.assets)

    def test_offscreen_alias_dialogue_assets_and_overrides(self):
        s=self.make_shot("مشهد ١\nلقطة ١\nآفا تفتح الباب.\nسام (خارج الكادر): هل آفا هنا؟")
        p=self.preview(s,requires_image=True)
        self.assertTrue(p["ready"],p["blockers"])
        self.assertEqual((p["visible"],p["speaking"]),([self.ava["id"]],[self.sam["id"]]))
        self.assertEqual((p["images"],p["audio"]),(["ava.png"],["sam.mp3"]))
        self.assertEqual(p["dialogue"][0]["text"],"هل آفا هنا؟")
        self.assertTrue(p["dialogue"][0]["offscreen"])
        self.assertEqual(p["positive_prompt"].count("هل آفا هنا؟"),1)
        s2=self.make_shot("Shot 1\nArtist studies the wall.\nArtist (O.S.): Who?")
        p=self.preview(s2);self.assertFalse(p["ready"])
        self.assertEqual({r["kind"] for r in p["ambiguity_choices"]},{"name","speaker"})
        self.edit(s2,overrides={"aliases":{"Artist":self.ava["id"]},"speakers":{"Artist":self.sam["id"]}})
        p=self.preview(s2);self.assertTrue(p["ready"],p["blockers"])
        self.assertEqual((p["visible"],p["speaking"]),([self.ava["id"]],[self.sam["id"]]))
        self.edit(s2,dialogue_override=[{"speaker":"Artist","text":"Who?","offscreen":False,"character_id":self.sam["id"]}])
        self.assertIn(self.sam["id"],self.preview(s2)["visible"])

    def test_unassigned_refs_only_block_affected_and_edits_invalidate(self):
        ghost=self.character("Ghost",[],[],[])
        bad=self.make_shot("Ghost opens a door.")
        ok=self.make_shot("Ava closes it.")
        result=se.approve_shots(self.db,self.ep,None,self.assets,{"version":"v1"})
        self.assertEqual(result["approved"],[ok["id"]]);self.assertIn("visual reference",result["blocked"][bad["id"]][0])
        self.assertEqual(ok["image"],"ava.png")
        self.assertEqual(ok["workflow_version"],"v1")
        se.save_character(self.db,{**ghost,"description":"Changed"},self.assets)
        self.assertTrue(ok["approved"])
        se.save_character(self.db,{**self.ava,"description":"New costume"},self.assets)
        self.assertFalse(ok["approved"])
        se.approve_shots(self.db,self.ep,[ok["id"]],self.assets,{"version":"v1"})
        rev=ok["revision"]
        self.edit(ok,workflowParameters={"seed":42},camera="Medium",negative_prompt="Noise")
        self.assertNotEqual(ok["revision"],rev);self.assertFalse(ok["approved"])
        p=self.preview(ok,negative_prompt=False);self.assertIn("Camera: Medium",p["positive_prompt"])
        self.assertTrue(p["warnings"])
        with self.assertRaisesRegex(ValueError,"matching"):
            se.save_character(self.db,{**ghost,"images":["ava.wav"]},self.assets)
        with self.assertRaisesRegex(ValueError,"existing character"):
            self.edit(ok,overrides={"speakers":{"Ghost":"absent"}})

    def test_variable_time_workflow_constraints_split_and_legacy(self):
        tiny=self.make_shot("Ava sits.")
        long=self.make_shot("Scene 1\nShot 1\nSam (O.S.): "+" ".join(["Please listen to every word."]*15))
        self.assertGreater(long["seconds"],tiny["seconds"])
        p=self.preview(long,version="v1",max_seconds=9, fps=8, max_frames=65,frame_step=8,frame_base=1)
        self.assertFalse(p["ready"]); self.assertGreater(len(p["split_proposal"]),1)
        long["takes"]=[{"id":"archived"}]
        old=long["id"]
        pieces=se.split_shot(self.db,self.ep,old,self.assets,{"max_seconds":9})
        self.assertEqual(pieces[0]["id"],old); self.assertEqual(pieces[0]["takes"],[{"id":"archived"}])
        self.assertEqual(" ".join(s["action"] for s in pieces),long["action"])
        for s in pieces:
            self.assertFalse(s["approved"]);self.assertLessEqual(s["seconds"],9)
            self.assertIn(self.sam["id"],self.preview(s)["speaking"])
            self.assertFalse(self.preview(s)["visible"])
        legacy={"id":se.uid(),"prompt":"A ticking clock","image":"clock.jpg","refs":["ava.wav"],
                "seconds":2,"approved":False,"revision":se.uid(),"scene_id":tiny["scene_id"]}
        p=self.preview(legacy)
        self.assertEqual((p["images"],p["audio"]),(["clock.jpg"],["ava.wav"]))
        self.assertFalse(self.preview(legacy,max_audio=0)["ready"])

    def test_inline_shots_markdown_headings_timed_scenes_and_encodings(self):
        inline = se.parse_story("Scene 1\nShot 1: Ava enters the room.\nShot 2: Sam waves.\n")
        self.assertEqual([s["action"] for s in inline["scenes"][0]["shots"]], ["Ava enters the room.", "Sam waves."])
        md = se.parse_story("**Scene 1**\n- **Shot 1:** Ava opens the window.\n")
        self.assertEqual(len(md["scenes"][0]["shots"]), 1)
        ep02 = se.parse_story("EP02 · SH001 | The Window Seat\nScene 1 / 00:00 – 00:10\nAva looks outside.\n")
        self.assertEqual(ep02["scenes"][0]["title"], "Scene 1")
        self.assertEqual(ep02["scenes"][0]["shots"][0]["seconds"], 10)
        self.assertEqual(se.extract_story("ar.txt", "مشهد 1\nالنافذة".encode("utf-16")), "مشهد 1\nالنافذة")
        self.assertEqual(se.extract_story("ar.txt", "مشهد 1\nالنافذة مفتوحة".encode("cp1256")), "مشهد 1\nالنافذة مفتوحة")


if __name__=="__main__":
    unittest.main()
