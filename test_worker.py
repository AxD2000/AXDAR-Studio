"""Deterministic unit and integration tests for AXDAR Studio V2 Worker layer (Phases 1-6)."""
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import production
import story_engine as se
import worker_runtime
from test_production import movie


class Phase1ShotIdentityAndTakesTests(unittest.TestCase):
    def test_shot_codes_are_stable_across_reorder_delete_and_new_shots(self):
        db = se.new_project()
        ep = se.new_episode("Episode 2: Rain")
        db["episodes"] = [ep]
        proposal = se.parse_story("Scene 1\nShot 1: Tom enters.\nShot 2: Emma smiles.\nShot 3: Jack waves.\n")
        se.apply_story(db, ep["id"], proposal)
        codes = [s["shot_code"] for s in ep["shots"]]
        self.assertEqual(codes, ["EP02S01", "EP02S02", "EP02S03"])

        s1, s2, s3 = ep["shots"]
        ep["shots"] = [s3, s1]
        self.assertFalse(se.assign_shot_codes(db, ep))
        self.assertEqual([s["shot_code"] for s in ep["shots"]], ["EP02S03", "EP02S01"])

        extra = se.parse_story("Scene 1\nShot 1: Rain falls.\n")
        se.apply_story(db, ep["id"], extra)
        self.assertEqual(ep["shots"][-1]["shot_code"], "EP02S04")
        self.assertFalse(se.assign_shot_codes(db, ep))

    def test_sequential_takes_preserve_history_and_friendly_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            eid = str(uuid.uuid4())
            sid = str(uuid.uuid4())
            shot = {"id": sid, "revision": "rev1", "seconds": 3, "prompt": "Tom walks", "shot_code": "EP01S04"}
            wf = {"name": "fixture", "output": "141"}
            calls = []

            def api(url, path, obj=None):
                if path == "/prompt":
                    calls.append(obj)
                    return {"prompt_id": str(len(calls))}
                if path.startswith("/history/"):
                    ticket = path.rsplit("/", 1)[-1]
                    return {ticket: {"status": {"status_str": "success", "completed": True}}}
                return {"queue_running": [], "queue_pending": []}

            ctx = {
                "DATA": root,
                "prompt": lambda s: s["prompt"],
                "prepare_shot": lambda _u, _f, _e, s, _w, _j: {"id": s["id"]},
                "json_api": api,
                "download_output": lambda _u, f, _h, j: (f / "video.part").write_bytes(movie(j["settings"]["seconds"], False)),
            }
            with patch("production.time.sleep"):
                self.assertEqual(production.render(ctx, "http://fx", time.time() + 60, eid, shot, wf, 0, lambda: None), "done")
                self.assertEqual(
                    production.render(ctx, "http://fx", time.time() + 60, eid, shot, wf, 0, lambda: None, force_new=True),
                    "done",
                )
            jobs = sorted((root / eid / sid).glob("*/job.json"))
            self.assertEqual(len(jobs), 2)
            j1, j2 = production.read(jobs[0]), production.read(jobs[1])
            self.assertEqual((j1["take_number"], j1["display_filename"]), (1, "EP01S04_T01.mp4"))
            self.assertEqual((j2["take_number"], j2["display_filename"]), (2, "EP01S04_T02.mp4"))
            self.assertTrue((jobs[0].parent / "video.mp4").is_file())
            self.assertTrue((jobs[1].parent / "video.mp4").is_file())

            db = se.new_project()
            ep = se.new_episode("EP 1")
            ep["id"] = eid
            ep["shots"] = [dict(shot, scene_id="sc1", takes=[])]
            db["episodes"] = [ep]
            self.assertTrue(se.sync_takes(db, root))
            self.assertFalse(se.sync_takes(db, root))
            takes = ep["shots"][0]["takes"]
            self.assertEqual([t["take_number"] for t in takes], [1, 2])
            self.assertEqual([t["display_filename"] for t in takes], ["EP01S04_T01.mp4", "EP01S04_T02.mp4"])

            se.select_take(db, eid, sid, takes[0]["id"])
            self.assertEqual(ep["shots"][0]["selected_take_id"], takes[0]["id"])
            with self.assertRaises(ValueError):
                se.select_take(db, eid, sid, "nonexistent-take")

            se.set_shot_skipped(db, eid, sid, True)
            self.assertTrue(ep["shots"][0]["skipped"])
            self.assertEqual(worker_runtime.canonical_shot_state(ep["shots"][0], {"status": "done", "video": "/v"}), "Skipped")
            self.assertEqual(worker_runtime.canonical_shot_state(ep["shots"][0], {"status": "queued"}), "Generating")
            se.set_shot_skipped(db, eid, sid, False)
            self.assertEqual(worker_runtime.canonical_shot_state(ep["shots"][0], {"status": "done", "video": "/v"}), "Completed")
            self.assertEqual(worker_runtime.canonical_shot_state(ep["shots"][0], {"status": "error"}), "Failed")
            self.assertEqual(worker_runtime.canonical_shot_state(ep["shots"][0], None), "Pending")

    def test_status_index_cache_avoids_rereading_unchanged_jobs_and_detects_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "ep1" / "sh1" / "100"
            folder.mkdir(parents=True)
            job_path = folder / "job.json"
            job_path.write_text(json.dumps({
                "shot": "sh1", "revision": "r1", "status": "queued",
                "shot_code": "EP01S01", "take_number": 1, "display_filename": "EP01S01_T01.mp4",
            }), "utf-8")
            cache = worker_runtime.StatusIndexCache(root)
            first = cache.list_results()
            self.assertEqual(len(first), 1)
            self.assertEqual(first[0]["displayFilename"], "EP01S01_T01.mp4")

            with patch("worker_runtime.json.loads", side_effect=AssertionError("Should use cache")):
                second = cache.list_results()
            self.assertEqual(second[0]["status"], "queued")

            (folder / "video.mp4").write_bytes(b"mp4")
            job_path.write_text(json.dumps({
                "shot": "sh1", "revision": "r1", "status": "done",
                "shot_code": "EP01S01", "take_number": 1, "display_filename": "EP01S01_T01.mp4",
            }), "utf-8")
            third = cache.list_results()
            self.assertEqual(third[0]["status"], "done")
            self.assertTrue(third[0]["video"].endswith("/ep1/sh1/100"))
            self.assertEqual(worker_runtime.take_download_name(root, "ep1", "sh1", "100"), "EP01S01_T01.mp4")


class Phase2WorkerConfigAndUnifiedExecutionTests(unittest.TestCase):
    def test_mandatory_invariants_cannot_be_disabled_and_optional_settings_persist(self):
        import worker_config
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = worker_config.WorkerConfigStore(root / "settings.json", root / "overrides")
            for invariant in worker_config.MANDATORY_INVARIANTS:
                with self.assertRaisesRegex(ValueError, "mandatory"):
                    store.update({invariant: False})
            updated = store.update({
                "autoRetry": True,
                "maxRetries": 3,
                "runLimit": 2,
                "seedMode": "fixed",
                "fixedSeed": 12345,
                "sessionMinutes": 0,
                "resolutionPreset": "720p",
                "aspectRatio": "16:9",
                "fpsOverride": 24,
            })
            self.assertEqual(updated["maxRetries"], 3)
            self.assertEqual(updated["runLimit"], 2)
            self.assertEqual(updated["fixedSeed"], 12345)
            reloaded = worker_config.WorkerConfigStore(root / "settings.json", root / "overrides")
            self.assertEqual(reloaded.get()["resolutionPreset"], "720p")
            self.assertEqual(reloaded.get()["fpsOverride"], 24)
            # Sidecar mapping override does not touch .zip packages
            ver = "a" * 64
            store.save_mapping_override(ver, {"seed": "63.noise_seed", "fps": "141.frame_rate"})
            self.assertEqual(store.get_mapping_override(ver)["seed"], "63.noise_seed")
            store.clear_mapping_override(ver)
            self.assertEqual(store.get_mapping_override(ver), {})

    def test_run_limit_and_step_mode_pause_and_resume_on_single_direct_session(self):
        import threading
        from types import SimpleNamespace
        import direct_session
        import workflow_profiles as wprof
        import worker_config

        eid = str(uuid.uuid4())
        shots = [
            [eid, {"id": str(uuid.uuid4()), "revision": f"r{i}", "seconds": 2, "prompt": f"Shot {i}", "shot_code": f"EP01S0{i}"}]
            for i in range(1, 4)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "production").mkdir()
            cfg = worker_config.WorkerConfigStore(root / "production" / "ws.json", root / "overrides")
            submitted_ids = []

            def api(url, path, obj=None):
                if path == "/object_info":
                    return {"Output": {}}
                if path == "/prompt":
                    submitted_ids.append(obj["prompt"]["id"])
                    return {"prompt_id": str(uuid.uuid4())}
                if path.startswith("/history/"):
                    t = path.rsplit("/", 1)[-1]
                    return {t: {"status": {"status_str": "success", "completed": True}, "outputs": {"141": {}}}}
                return {"queue_running": [], "queue_pending": []}

            wf = {"version": "a" * 64, "name": "Fix", "output": "141", "graph": {"141": {"class_type": "Output", "inputs": {}}}}
            store_stub = SimpleNamespace(get=lambda _v: wf)
            ctx = {
                "DATA": root, "LOCK": threading.RLock(), "json_api": api,
                "prepare_shot": lambda _u, _f, _e, s, _w, _r: {"id": s["id"], "141": {"inputs": {}}},
                "download_output": lambda _u, f, _h, _j: (f / "video.part").write_bytes(movie(2, False)),
                "prompt": lambda s: s["prompt"],
                "take_quality": SimpleNamespace(check=lambda _p, _j: {"technical": "passed"}),
                "actions": SimpleNamespace(store=store_stub),
                "connection": SimpleNamespace(snapshot=lambda: {"url": "http://fixture"}, target=lambda: None),
                "worker_config": cfg,
                "workflow_manager": SimpleNamespace(busy=lambda: False),
                "running": True,
            }
            session = direct_session.DirectSession(ctx)
            ctx["run_direct"] = lambda u, w, t: session.run(u, w, t)
            view = {"end": time.time() + 600, "episode": eid, "submitted": 3, "phase": "Preparing ComfyUI", "completed": 0, "failed": 0}
            session.start(view, shots, wf, None, "http://fixture", run_limit=1, worker_settings=cfg.get())
            with patch.object(direct_session, "runtime_requirements", return_value={"graph": wf["graph"]}), \
                 patch.object(wprof, "timing", return_value={"playback_seconds": 2}), \
                 patch.object(production.time, "sleep"):
                session.run("http://fixture", wf)
                self.assertEqual(len(submitted_ids), 1)
                self.assertEqual(view["phase"], "Paused")
                self.assertIn("Paused after 1 shot", view.get("pauseReason", ""))
                self.assertEqual(session.plan["index"], 1)

                # Resume with runLimit=0 (all remaining) finishes shots 2 and 3
                session.control({"action": "resume", "episode": eid, "minutes": 0, "runLimit": 0})
                self.assertEqual(len(submitted_ids), 3)
                self.assertEqual(view["completed"], 3)
                self.assertEqual(view["phase"], "Finished")
                self.assertTrue(view.get("unlimited"))


if __name__ == "__main__":
    unittest.main()
