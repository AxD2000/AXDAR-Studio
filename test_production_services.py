import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import drive_archive as da
import production as p
import production_services as services
import take_quality


class DriveTests(unittest.TestCase):
    def test_local_oauth_uses_pkce_and_drive_file_only(self):
        pending,url=da.authorization({"installed":{"client_id":"id.apps.googleusercontent.com","client_secret":"secret"}},
                                     "http://127.0.0.1:18753/drive/callback")
        self.assertIn("code_challenge_method=S256",url)
        self.assertIn("drive.file",url)
        self.assertNotIn(pending["client_secret"],url)
        with patch.object(da,"token") as exchange:
            with self.assertRaisesRegex(ValueError,"declined"):
                da.connect(pending,"wrong","code")
            exchange.assert_not_called()

    def test_resumable_upload_recovers_ack_and_reuses_verified_identity(self):
        import hashlib
        payload=b"abcdefgh"
        md5=hashlib.md5(payload).hexdigest()
        key=da.key_for("fixture:"+md5)
        meta={"id":"drive_file","appProperties":{"axdar":key},"parents":["take"],"size":"8","md5Checksum":md5}
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            path=folder/"take.zip"
            path.write_bytes(payload)
            journal=folder/"state.json"
            assumed_uri=da.UPLOAD+"?upload_id=fake"
            client=da.Archive({},journal)
            with patch.object(client,"api",side_effect=[
                (200,{},{"files":[]}), (200,{},{"ids":["drive_file"]}), (200,{"Location":assumed_uri},{}),
                ConnectionError("lost acknowledgement"),
            ]):
                with self.assertRaises(ConnectionError):
                    client.upload(path,"take.zip","take","fixture")
            self.assertEqual(p.read(journal)[key]["session"],assumed_uri)
            client=da.Archive({},journal)
            with patch.object(client,"api",side_effect=[
                (404,{},{}),(308,{"Range":"bytes=0-3"},{}),(200,{},{}),(200,{},meta)
            ]) as network:
                self.assertEqual(client.upload(path,"take.zip","take","fixture"),"drive_file")
                self.assertEqual(network.call_args_list[2].args[2],b"efgh")
                self.assertEqual(network.call_args_list[2].args[3]["Content-Range"],"bytes 4-7/8")
            with patch.object(client,"api",return_value=(200,{},meta)) as fetch:
                self.assertEqual(client.upload(path,"take.zip","take","fixture"),"drive_file")
                fetch.assert_called_once()
            with self.assertRaisesRegex(ValueError,"checksum"):
                client.verify(dict(meta,md5Checksum="bad"),key,"take",size=8,md5=md5)


class LifecycleTests(unittest.TestCase):
    def test_warning_gates_countdown_and_cancellation_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            spec={"id":"test","episode":"episode","jobs":[["episode",{"id":"shot"},[]]],"shotRetries":1,"url":"http://127.0.0.1:8188"}
            status={"status":"finished","shots":{"shot":"done"},"backup":{"state":"verified"}}
            ctx={"json_api":lambda *_:{"queue_running":[],"queue_pending":[]}}
            held=dict(status,backup={"state":"failed"})
            self.assertIn("archive",services.countdown(folder,spec,held,ctx,None)["reason"])
            self.assertIn("delivered",services.countdown(folder,spec,status,ctx,None)["reason"])
            active={"json_api":lambda *_:{"queue_running":[["job"]],"queue_pending":[]}}
            self.assertIn("active",services.countdown(folder,spec,status,active,None)["reason"])
            def local_delivery(run,_endpoint):
                rows=p.read(run/"events.json")
                for event in rows.values():
                    event["delivery"]="delivered"
                return rows
            with patch.object(services,"deliver",side_effect=local_delivery):
                outcome=services.countdown(folder,spec,status,ctx,"https://fixture.invalid")
            self.assertEqual(outcome["status"],"countdown")
            self.assertLess(abs(outcome["deadline"]-time.time()-600),1)
            p.atomic(folder/"lifecycle-cancel.json",{"reason":"Cancelled by user."})
            self.assertEqual(services.countdown(folder,spec,status,ctx,None)["status"],"cancelled")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),"FFmpeg binaries required for real decoding.")
class DecodeTests(unittest.TestCase):
    def test_decodes_real_video_and_checks_required_audio_resolution_and_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            file=Path(tmp)/"video.mp4"
            subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i","testsrc2=s=160x90:r=24:d=2",
                            "-c:v","libx264","-y",str(file)],capture_output=True,check=True)
            job={"settings":{"seconds":2},"resolution":[160,90]}
            clean=take_quality.check(file,job)
            self.assertEqual((clean["technical"],clean["frames"],clean["width"],clean["height"]),("passed",48,160,90))
            self.assertEqual(clean["visual"],"review required")
            self.assertEqual(take_quality.check(file,dict(job,audioRequired=True))["technical"],"failed")
            self.assertEqual(take_quality.check(file,dict(job,resolution=[320,180]))["technical"],"failed")
            file.write_bytes(file.read_bytes()[:300])
            self.assertEqual(take_quality.check(file,job)["technical"],"failed")
