import io
import struct
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

import production as p

ROOT = Path(__file__).resolve().parent


def box(k, v):
    return struct.pack(">I4s", len(v) + 8, k) + v


def movie(seconds, audio=True):
    header = bytes(12) + struct.pack(">II", 1000, round(seconds * 1000))
    track = box(b"trak", box(b"mdia", box(b"hdlr", bytes(8) + b"vide")))
    if audio:
        track += box(b"trak", box(b"mdia", box(b"hdlr", bytes(8) + b"soun")))
    return box(b"ftyp", b"isom") + box(b"mdat", b"sample") + box(b"moov", box(b"mvhd", header) + track)


class QueueTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.status = self.dir / "status.json"
        self.eid = str(uuid.uuid4())
        self.requests = []
        self.failed_shots = set()
        self.lose = False
        self.network = 0
        self.workflow = {"name": "fixture", "output": "141"}
        self.ctx = {
            "DATA": self.dir,
            "prompt": lambda s: s["prompt"],
            "prepare_shot": lambda url, folder, eid, s, w, j: {"id": s["id"]},
            "json_api": self.api,
            "download_output": lambda u, f, h, j: (f / "video.part").write_bytes(movie(j["settings"]["seconds"])),
        }
        self.url = "http://fixture"
        self.deadline = time.time() + 600
        self.sleep = patch("production.time.sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def jobs(self, n):
        return [(self.eid, dict(id=str(uuid.uuid4()), revision="rev", seconds=5, prompt="Tom")) for _ in range(n)]

    def api(self, url, path, obj=None):
        if path == "/prompt":
            self.requests.append(obj)
            if self.lose:
                self.lose = False
                raise ConnectionError("lost acknowledgement")
            return {"prompt_id": str(len(self.requests))}
        if path == "/queue":
            return dict(queue_running=[[i, str(i), x["prompt"], x["extra_data"]] for i, x in enumerate(self.requests, 1)], queue_pending=[])
        if path == "/history":
            return {}
        if self.network:
            self.network -= 1
            raise ConnectionError("reset")
        ticket = path.split("/")[-1]
        shot = self.requests[int(ticket) - 1]["prompt"]["id"]
        return {ticket: {"status": {"status_str": "error" if shot in self.failed_shots else "success"}}}

    def test_direct_output_is_available_after_technical_checks(self):
        eid, shot = self.jobs(1)[0]
        ctx = dict(self.ctx, direct=True, instance=12,
                   technical_qc=lambda path, job: {"technical": "passed", "visual": "review required"})
        args = (eid, shot, self.workflow, 0, lambda: None)
        self.assertEqual(p.render(ctx, self.url, self.deadline, *args), "done")
        record = next((self.dir / eid / shot["id"]).glob("*/job.json"))
        take = p.read(record)
        self.assertEqual(take["instance"], 12)
        self.assertNotIn("review", take)
        self.assertEqual(take["quality"], {"technical": "passed"})
        self.assertTrue((record.parent / "video.mp4").is_file())
        self.assertEqual(p.render(ctx, "http://new-tunnel", self.deadline, *args), "done")
        self.assertEqual(len(self.requests), 1)

    def test_direct_upgrade_reconciles_legacy_numeric_instance_id(self):
        eid, shot = self.jobs(1)[0]
        prior = dict(self.ctx, direct=True, instance=12)
        args = (eid, shot, self.workflow, 0, lambda: None)
        self.network = 1
        self.assertEqual(p.render(prior, "http://old-tunnel", self.deadline, *args), "waiting")
        updated = dict(prior, instance="12")
        self.assertEqual(p.render(updated, "http://new-tunnel", self.deadline, *args), "done")
        self.assertEqual(len(self.requests), 1)

    def test_atomic_retries_when_windows_reader_holds_destination(self):
        error = PermissionError("sharing violation")
        error.winerror = 32
        replace = p.os.replace

        def retryable_replace(src, dest):
            if mocked.call_count == 1:
                raise error
            replace(src, dest)

        with patch("production.os.replace", side_effect=retryable_replace) as mocked:
            p.atomic(self.status, {"phase": "done"})
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(p.read(self.status), {"phase": "done"})

    def test_ambiguous_submit_and_transient_poll(self):
        self.lose = True
        self.network = 1
        eid, shot = self.jobs(1)[0]
        self.assertEqual(p.render(self.ctx, self.url, self.deadline, eid, shot, self.workflow, 2, lambda: None), "done")
        self.assertEqual(len(self.requests), 1)

    def test_interruption_after_acknowledgement_preserves_the_prompt(self):
        eid, shot = self.jobs(1)[0]

        def interrupted(url, path, obj=None):
            if path.startswith("/history/"):
                raise RuntimeError("The local session stopped monitoring")
            return self.api(url, path, obj)

        self.ctx["json_api"] = interrupted
        args = (self.ctx, self.url, self.deadline, eid, shot, self.workflow, 0, lambda: None)
        self.assertEqual(p.render(*args), "waiting")
        [record] = (self.dir / eid / shot["id"]).glob("*/job.json")
        job = p.read(record)
        self.assertEqual(job["ticket"], "1")
        p.atomic(record, dict(job, status="error"))
        self.ctx["json_api"] = self.api
        self.assertEqual(p.render(*args, retry_failed=True), "done")
        self.assertEqual(len(self.requests), 1)

    def test_http_validation_rejection_remains_a_definite_error(self):
        eid, shot = self.jobs(1)[0]
        error = HTTPError(self.url + "/prompt", 400, "invalid input", {}, io.BytesIO(b""))
        self.addCleanup(error.close)

        def rejected(url, path, obj=None):
            self.assertEqual(path, "/prompt")
            raise ValueError("ComfyUI 400: invalid input") from error

        self.ctx["json_api"] = rejected
        result = p.render(self.ctx, self.url, self.deadline, eid, shot, self.workflow, 0, lambda: None)
        self.assertEqual(result, "error")
        [record] = (self.dir / eid / shot["id"]).glob("*/job.json")
        self.assertNotIn("ticket", p.read(record))

    def test_missing_ack_no_evidence_never_resubmits(self):
        eid, shot = self.jobs(1)[0]

        def no_ack(url, path, obj=None):
            if path == "/prompt":
                self.requests.append(obj)
                raise ConnectionError("lost")
            if path == "/queue":
                return dict(queue_running=[], queue_pending=[])
            return {}

        self.ctx["json_api"] = no_ack
        self.assertEqual(p.render(self.ctx, self.url, self.deadline, eid, shot, self.workflow, 2, lambda: None), "uncertain")
        self.assertEqual(len(self.requests), 1)

    def test_bad_video_and_wrong_duration_fail(self):
        eid, shot = self.jobs(1)[0]
        self.ctx["download_output"] = lambda url, folder, h, j: (folder / "video.part").write_bytes(movie(5)[:-9])
        self.assertEqual(p.render(self.ctx, self.url, self.deadline, eid, shot, self.workflow, 0, lambda: None), "error")
        file = self.dir / "wrong.mp4"
        file.write_bytes(movie(25))
        with self.assertRaises(ValueError):
            p.inspect_video(file, 5)

    def test_frame_grid_duration_is_used_for_render_and_resume(self):
        from test_workflow_profiles import generic_graph
        from workflow_profiles import inspect_workflow

        graph, metadata = generic_graph()
        graph["out"]["inputs"]["fps"] = 10
        metadata["MyVideo"]["input"]["required"]["frame_count"][1]["step"] = 20
        self.workflow = inspect_workflow("long frame steps", graph, metadata)
        self.assertEqual(self.workflow["issues"], [])
        eid, shot = self.jobs(1)[0]
        shot["seconds"] = 1
        self.ctx["download_output"] = lambda url, folder, history, j: (folder / "video.part").write_bytes(movie(2.1))
        self.assertEqual(p.render(self.ctx, self.url, self.deadline, eid, shot, self.workflow, 0, lambda: None), "done")
        record, = self.dir.glob("*/*/*/job.json")
        self.assertEqual(p.read(record)["playback_seconds"], 2.1)
        self.assertEqual(p.render(self.ctx, self.url, self.deadline, eid, shot, self.workflow, 0, lambda: None), "done")
        self.assertEqual(len(self.requests), 1)

    def test_output_audio_is_checked_against_the_runtime_graph(self):
        jobs = self.jobs(2)
        silent, voiced = jobs[0][1]["id"], jobs[1][1]["id"]
        self.workflow["graph"] = {"141": {"inputs": {"audio": ["51", 0]}}}

        def prepare(_url, _folder, _eid, shot, _workflow, _job):
            return {"id": shot["id"], "141": {"inputs": {"audio": ["51", 0]} if shot["id"] == voiced else {}}}

        self.ctx["prepare_shot"] = prepare
        self.ctx["download_output"] = lambda url, folder, history, j: (folder / "video.part").write_bytes(movie(5, False))
        results = {s["id"]: p.render(self.ctx, self.url, self.deadline, eid, s, self.workflow, 0, lambda: None) for eid, s in jobs}
        self.assertEqual(results, {silent: "done", voiced: "error"})

    def test_missing_audio_track(self):
        path = self.dir / "video.mp4"
        path.write_bytes(movie(5, False))
        with self.assertRaisesRegex(ValueError, "audio track"):
            p.inspect_video(path, 5, True)


if __name__ == "__main__":
    unittest.main()
