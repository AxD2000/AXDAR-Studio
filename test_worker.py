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


class Phase3NodeDetectionAndOverridesTests(unittest.TestCase):
    def test_minimax_h3_package_node_detection_overrides_and_filename_prefix(self):
        import workflow_profiles as wprof
        import worker_config
        pkg_path = Path(__file__).resolve().parent / "packages" / "MiniMax_H3.zip"
        raw = pkg_path.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            store = wprof.WorkflowStore(Path(tmp))
            wf = store.import_package(pkg_path.name, raw)
            obj_info = {
                "ResolutionSelector": {
                    "input": {
                        "required": {
                            "aspect_ratio": [["9:16 (Portrait Widescreen)", "16:9 (Landscape Widescreen)", "1:1 (Square)"]],
                            "megapixels": ["FLOAT", {"default": 0.5}],
                            "multiple": ["INT", {"default": 32}],
                        }
                    }
                }
            }
            detected = wprof.detect_worker_nodes(wf, obj_info)
            nodes = detected["nodes"]
            self.assertEqual(nodes["seed"]["id"], "63.noise_seed")
            self.assertEqual(nodes["fps"]["id"], "141.frame_rate")
            self.assertEqual(nodes["fps"]["default"], 24)
            self.assertEqual(nodes["resolution"]["mode"], "selector")
            self.assertEqual(nodes["resolution"]["multiple"], 32)
            self.assertEqual(nodes["resolution"]["aspectRatio"]["id"], "200.aspect_ratio")
            self.assertEqual(nodes["resolution"]["megapixels"]["id"], "200.megapixels")
            self.assertTrue(nodes["resolution"]["hasUpscaler"])
            self.assertEqual(nodes["output"]["filenamePrefix"], "141.filename_prefix")
            # Warns about hardcoded 24fps ComfyMathExpression and downstream Upscaler
            joined_warn = "\n".join(detected["warnings"])
            self.assertIn("ComfyMathExpression", joined_warn)
            self.assertIn("Upscaler", joined_warn)

            settings = dict(worker_config.DEFAULT_SETTINGS,
                            seedMode="random", resolutionPreset="720p", aspectRatio="16:9", fpsOverride=30)
            shot = {"id": "s1", "seconds": 5, "shot_code": "EP01S04"}
            meta1 = worker_runtime.build_shot_worker_overrides(wf, settings, detected, eid="ep1", shot=shot, take_number=1)
            meta2 = worker_runtime.build_shot_worker_overrides(wf, settings, detected, eid="ep1", shot=shot, take_number=2)
            self.assertNotEqual(meta1["seed"], meta2["seed"])
            self.assertEqual(meta1["output_prefix"], "AXDAR/ep1/EP01S04_T01")
            self.assertEqual(meta1["worker_overrides"]["200.aspect_ratio"], "16:9 (Landscape Widescreen)")
            self.assertEqual(meta1["worker_overrides"]["141.frame_rate"], 30)
            self.assertAlmostEqual(meta1["worker_overrides"]["200.megapixels"], 0.9, places=1)
            # playback_seconds recalculated from 124 frames / 30 fps
            self.assertAlmostEqual(meta1["playback_seconds"], 124 / 30.0, places=3)

            # Applying overrides in runtime_graph passes verify_strict_integrity!
            g = wprof.runtime_graph(
                wf, shot, "Hero walks", ["hero.png"], ["hero.mp3"],
                lambda n: "up_" + n, meta1["output_prefix"],
                worker_overrides=meta1["worker_overrides"], apply_prefix=True,
            )
            self.assertEqual(g["63"]["inputs"]["noise_seed"], meta1["seed"])
            self.assertEqual(g["200"]["inputs"]["aspect_ratio"], "16:9 (Landscape Widescreen)")
            self.assertEqual(g["200"]["inputs"]["multiple"], 32)
            self.assertEqual(g["141"]["inputs"]["frame_rate"], 30)
            self.assertEqual(g["141"]["inputs"]["filename_prefix"], "AXDAR/ep1/EP01S04_T01")
            # Default runtime_graph without apply_prefix still preserves original filename_prefix
            g_default = wprof.runtime_graph(wf, shot, "Hero walks", ["hero.png"], [], lambda n: "up_" + n, "AXDAR/ignored")
            self.assertEqual(g_default["141"]["inputs"]["filename_prefix"], wf["graph"]["141"]["inputs"]["filename_prefix"])

    def test_numeric_width_height_workflow_youtube_preset_snapping_and_qc_compatibility(self):
        import workflow_profiles as wprof
        import worker_config
        import take_quality
        graph = {
            "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "pos"}},
            "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "neg"}},
            "3": {"class_type": "EmptyLatentImage", "inputs": {"width": 832, "height": 480, "batch_size": 1}},
            "4": {"class_type": "KSampler", "inputs": {"positive": ["1", 0], "negative": ["2", 0], "latent_image": ["3", 0], "seed": 777}},
            "5": {"class_type": "CustomVideo", "inputs": {"samples": ["4", 0], "num_frames": 49}},
            "6": {"class_type": "VHS_VideoCombine", "inputs": {"images": ["5", 0], "frame_rate": 16, "format": "video/h264-mp4", "filename_prefix": "orig"}},
        }
        wf = wprof.inspect_workflow("numeric-wf", graph, {}, {
            "output": "6", "positive": "1.text", "negative": "2.text", "frames": "5.num_frames", "fps": 16,
        })
        self.assertEqual(wf["issues"], [])
        detected = wprof.detect_worker_nodes(wf)
        self.assertEqual(detected["nodes"]["resolution"]["mode"], "numeric")
        self.assertEqual(detected["nodes"]["seed"]["id"], "4.seed")
        self.assertTrue(detected["nodes"]["frames"]["editable"])

        settings = dict(worker_config.DEFAULT_SETTINGS,
                        seedMode="fixed", fixedSeed=999, resolutionPreset="720p", aspectRatio="16:9", fpsOverride=24, framesOverride=72)
        meta = worker_runtime.build_shot_worker_overrides(wf, settings, detected, eid="ep1", shot={"id": "s1", "seconds": 3, "shot_code": "EP01S02"}, take_number=1)
        # 720p @ 16:9 snapped to multiple=32 -> 1280 x 704 (since 720 is not divisible by 32)
        self.assertEqual(meta["worker_overrides"]["3.width"], 1280)
        self.assertEqual(meta["worker_overrides"]["3.height"], 704)
        self.assertEqual(meta["worker_overrides"]["4.seed"], 999)
        self.assertEqual(meta["worker_overrides"]["5.num_frames"], 72)
        self.assertEqual(meta["worker_overrides"]["6.frame_rate"], 24)
        self.assertEqual(meta["playback_seconds"], 3.0)

        g = wprof.runtime_graph(
            wf, {"seconds": 3}, "Hello", [], [], str, meta["output_prefix"],
            worker_overrides=meta["worker_overrides"], apply_prefix=True,
        )
        self.assertEqual(g["3"]["inputs"]["width"], 1280)
        self.assertEqual(g["3"]["inputs"]["height"], 704)
        self.assertEqual(g["6"]["inputs"]["filename_prefix"], "AXDAR/ep1/EP01S02_T01")

        # Verify take_quality accepts selector mode without false mismatch against stale job['resolution']
        fake_probe = json.dumps({
            "streams": [{"codec_type": "video", "index": 0, "width": 1280, "height": 704, "duration": "3.0"}],
            "format": {"duration": "3.0"},
        }).encode()
        with patch("take_quality.tool", side_effect=lambda name: name), \
             patch("take_quality.subprocess.run") as run_mock:
            run_mock.side_effect = [
                type("R", (), {"stdout": fake_probe, "check_returncode": lambda self: None})(),
                type("R", (), {"stdout": b"frame=72\n", "check_returncode": lambda self: None})(),
            ]
            qc = take_quality.check(Path("fake.mp4"), {
                "settings": {"seconds": 3}, "playback_seconds": 3.0,
                "resolution": [832, 480], "applied_resolution": {"mode": "selector", "megapixels": 0.9},
            })
        self.assertEqual(qc["technical"], "passed")


class Phase4ErrorClassifierAndLogsTests(unittest.TestCase):
    def test_error_classifier_distinguishes_system_wide_connection_and_shot_errors(self):
        oom = worker_runtime.classify_error("RuntimeError: CUDA out of memory. Tried to allocate 2.00 GiB")
        self.assertEqual((oom["category"], oom["code"], oom["shouldHalt"]), ("system_wide", "cuda_oom", True))

        missing_model = worker_runtime.classify_error("FileNotFoundError: model.safetensors not found")
        self.assertEqual((missing_model["category"], missing_model["code"], missing_model["shouldHalt"]), ("system_wide", "missing_model", True))

        conn = worker_runtime.classify_error("SSH tunnel ended during the session.")
        self.assertEqual((conn["category"], conn["shouldHalt"]), ("connection", False))

        shot = worker_runtime.classify_error("Invalid MP4 or unexpected duration 1.00s (expected 5s)")
        self.assertEqual((shot["category"], shot["shouldHalt"]), ("shot_specific", False))

    def test_system_wide_error_halts_immediately_while_shot_error_retries_with_new_take(self):
        import threading
        from types import SimpleNamespace
        import direct_session
        import workflow_profiles as wprof
        import worker_config

        eid = str(uuid.uuid4())
        wf = {"version": "a" * 64, "name": "Fix", "output": "141", "graph": {"141": {"class_type": "Output", "inputs": {}}}}

        # Part A: System-Wide error on shot 1 MUST halt immediately without submitting shot 2
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "production").mkdir()
            cfg = worker_config.WorkerConfigStore(root / "production" / "ws.json", root / "overrides")
            logs = worker_runtime.WorkerLogStore(root / "production" / "worker.log")
            shots = [
                [eid, {"id": str(uuid.uuid4()), "revision": "r1", "seconds": 2, "prompt": "One", "shot_code": "EP01S01"}],
                [eid, {"id": str(uuid.uuid4()), "revision": "r2", "seconds": 2, "prompt": "Two", "shot_code": "EP01S02"}],
            ]
            submitted = []

            def api_oom(url, path, obj=None):
                if path == "/object_info":
                    return {"Output": {}}
                if path == "/prompt":
                    submitted.append(obj["prompt"]["id"])
                    return {"prompt_id": "t1"}
                if path == "/history/t1":
                    return {"t1": {"status": {"status_str": "error", "messages": [
                        ["execution_error", {"node_type": "KSampler", "exception_message": "CUDA out of memory"}]
                    ]}}}
                return {"queue_running": [], "queue_pending": []}

            ctx = {
                "DATA": root, "LOCK": threading.RLock(), "json_api": api_oom,
                "prepare_shot": lambda _u, _f, _e, s, _w, _r: {"id": s["id"], "141": {"inputs": {}}},
                "download_output": lambda _u, f, _h, _j: None,
                "prompt": lambda s: s["prompt"],
                "take_quality": SimpleNamespace(check=lambda _p, _j: {"technical": "passed"}),
                "worker_config": cfg, "worker_logs": logs, "running": True,
            }
            session = direct_session.DirectSession(ctx)
            view = {"end": time.time() + 600, "episode": eid, "submitted": 2, "phase": "Preparing ComfyUI", "completed": 0, "failed": 0}
            session.start(view, shots, wf, None, "http://fx", worker_settings=cfg.get())
            with patch.object(direct_session, "runtime_requirements", return_value={"graph": wf["graph"]}), \
                 patch.object(wprof, "timing", return_value={"playback_seconds": 2}), \
                 patch.object(production.time, "sleep"):
                session.run("http://fx", wf)
            self.assertEqual(len(submitted), 1)  # Shot 2 NEVER submitted!
            self.assertEqual(view["phase"], "Needs attention")
            self.assertEqual(view["errorClassification"]["code"], "cuda_oom")
            self.assertTrue(any("cuda_oom" in line for line in logs.tail()))

        # Part B: Shot-specific error retries automatically as Take 2 when autoRetry=True, then succeeds
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "production").mkdir()
            cfg = worker_config.WorkerConfigStore(root / "production" / "ws.json", root / "overrides")
            cfg.update({"autoRetry": True, "maxRetries": 1})
            logs = worker_runtime.WorkerLogStore(root / "production" / "worker.log")
            s1 = str(uuid.uuid4())
            shots = [[eid, {"id": s1, "revision": "r1", "seconds": 2, "prompt": "One", "shot_code": "EP01S01"}]]
            attempt_n = {"n": 0}

            def api_retry(url, path, obj=None):
                if path == "/object_info":
                    return {"Output": {}}
                if path == "/prompt":
                    attempt_n["n"] += 1
                    return {"prompt_id": f"t{attempt_n['n']}"}
                if path == "/history/t1":
                    return {"t1": {"status": {"status_str": "error", "messages": [
                        ["execution_error", {"node_type": "Render", "exception_message": "Transient single-shot render glitch"}]
                    ]}}}
                if path == "/history/t2":
                    return {"t2": {"status": {"status_str": "success", "completed": True}, "outputs": {"141": {}}}}
                return {"queue_running": [], "queue_pending": []}

            ctx = {
                "DATA": root, "LOCK": threading.RLock(), "json_api": api_retry,
                "prepare_shot": lambda _u, _f, _e, s, _w, _r: {"id": s["id"], "141": {"inputs": {}}},
                "download_output": lambda _u, f, _h, _j: (f / "video.part").write_bytes(movie(2, False)),
                "prompt": lambda s: s["prompt"],
                "take_quality": SimpleNamespace(check=lambda _p, _j: {"technical": "passed"}),
                "worker_config": cfg, "worker_logs": logs, "running": True,
            }
            session = direct_session.DirectSession(ctx)
            view = {"end": time.time() + 600, "episode": eid, "submitted": 1, "phase": "Preparing ComfyUI", "completed": 0, "failed": 0}
            session.start(view, shots, wf, None, "http://fx", worker_settings=cfg.get())
            with patch.object(direct_session, "runtime_requirements", return_value={"graph": wf["graph"]}), \
                 patch.object(wprof, "timing", return_value={"playback_seconds": 2}), \
                 patch.object(production.time, "sleep"):
                session.run("http://fx", wf)
            self.assertEqual(attempt_n["n"], 2)
            self.assertEqual(view["completed"], 1)
            self.assertEqual(view["phase"], "Finished")
            takes = sorted((root / eid / s1).glob("*/job.json"))
            self.assertEqual(len(takes), 2)
            self.assertEqual(production.read(takes[1])["take_number"], 2)

            # Verify 3-log aggregator returns comfyui, remote, and studio tabs cleanly
            bundle = worker_runtime.collect_three_logs({
                "worker_logs": logs,
                "connection": SimpleNamespace(snapshot=lambda: {"url": "http://fx"}, recent_logs=lambda n=120: ["[12:00:00] Ready"]),
                "json_api": lambda _u, _p: {"entries": [{"t": "12:00:01", "m": "Prompt executed"}]},
                "session": view,
            })
            self.assertIn("Prompt executed", bundle["comfyui"])
            self.assertIn("Ready", bundle["remote"])
            self.assertIn("Auto-retrying shot EP01S01", bundle["studio"])


class Phase5FinancialMonitorTests(unittest.TestCase):
    def test_financial_monitor_cold_start_exclusion_rolling_average_and_manual_ssh_mode(self):
        # 1) After Shot #1 (120s cold start), uses Shot #1 with coldStartOnly=True
        conn = {"rate": 1.20, "uptime": 14.5}  # $1.20/hr = $0.02/min
        session = {"episode": "ep1", "started": 1000.0, "submitted": 10, "completed": 1, "failed": 0, "skipped": 0}
        res1 = [{"episode": "ep1", "status": "done", "started": 1005.0, "renderSeconds": 120.0}]
        fin1 = worker_runtime.compute_financial_summary(conn, session, res1, now=1120.0)
        self.assertEqual(fin1["hourlyRate"], 1.2)
        self.assertEqual(fin1["minuteRate"], 0.02)
        self.assertEqual(fin1["rateBadge"], "Actual · Vast.ai")
        self.assertEqual(fin1["instanceUptimeMins"], 14.5)
        self.assertTrue(fin1["coldStartOnly"])
        self.assertEqual(fin1["avgShotSeconds"], 120.0)
        self.assertEqual(fin1["remainingShots"], 9)
        self.assertAlmostEqual(fin1["currentSessionCost"], 0.04, places=4)
        self.assertAlmostEqual(fin1["estimatedRemainingCost"], 9 * 2.0 * 0.02, places=4)
        self.assertAlmostEqual(fin1["estimatedTotalCost"], 0.04 + 0.36, places=4)

        # 2) After Shots #2 (60s) & #3 (60s), excludes Shot #1 cold start -> steady avg = 60s
        session2 = dict(session, completed=3)
        res3 = res1 + [
            {"episode": "ep1", "status": "done", "started": 1125.0, "renderSeconds": 60.0},
            {"episode": "ep1", "status": "done", "started": 1190.0, "renderSeconds": 60.0},
        ]
        fin3 = worker_runtime.compute_financial_summary(conn, session2, res3, now=1240.0)
        self.assertFalse(fin3["coldStartOnly"])
        self.assertEqual(fin3["avgShotSeconds"], 60.0)
        self.assertEqual(fin3["remainingShots"], 7)
        self.assertAlmostEqual(fin3["estimatedRemainingSeconds"], 420.0, places=1)
        self.assertAlmostEqual(fin3["estimatedRemainingCost"], 7 * 1.0 * 0.02, places=4)

        # 3) Manual SSH without Vast rate -> Unavailable, then with manualHourlyRate -> Manual
        fin_none = worker_runtime.compute_financial_summary({}, session2, res3, {}, now=1240.0)
        self.assertIsNone(fin_none["hourlyRate"])
        self.assertEqual(fin_none["rateBadge"], "Unavailable")
        fin_manual = worker_runtime.compute_financial_summary({}, session2, res3, {"manualHourlyRate": 0.60}, now=1240.0)
        self.assertEqual(fin_manual["hourlyRate"], 0.6)
        self.assertEqual(fin_manual["rateBadge"], "Manual")
        self.assertAlmostEqual(fin_manual["minuteRate"], 0.01, places=5)

    def test_connection_snapshot_exposes_vast_rate_and_uptime(self):
        import vast_connection
        with tempfile.TemporaryDirectory() as tmp:
            conn = vast_connection.Connection(Path(tmp) / "conn.json", lambda: [], lambda *a, **k: b"")
            conn.endpoint = {"id": 42, "gpu": "RTX 5090", "rate": 0.85, "uptime": 22.0}
            conn.state = "Ready"
            conn.url = "http://127.0.0.1:9999"
            snap = conn.snapshot()
            self.assertEqual(snap["rate"], 0.85)
            self.assertEqual(snap["uptime"], 22.0)


class Phase6DownloadsDeliveryAndEndToEndTests(unittest.TestCase):
    def test_downloads_export_respects_selected_take_all_takes_and_safe_folder_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "episodes"
            dl = root / "Downloads"
            db = se.new_project()
            ep = se.new_episode('Episode 1: <Storm>? "Cut"')
            db["episodes"] = [ep]
            s1_id = str(uuid.uuid4())
            ep["shots"] = [{"id": s1_id, "shot_code": "EP01S04", "selected_take_id": "t-one", "takes": []}]

            for ts, tid, tnum, payload in (("100", "t-one", 1, b"take1-mp4"), ("200", "t-two", 2, b"take2-mp4")):
                f = data / ep["id"] / s1_id / ts
                f.mkdir(parents=True)
                (f / "video.mp4").write_bytes(payload)
                (f / "job.json").write_text(json.dumps({
                    "shot": s1_id, "revision": "r1", "status": "done",
                    "take_id": tid, "take_number": tnum, "shot_code": "EP01S04",
                    "display_filename": f"EP01S04_T{tnum:02d}.mp4", "duration": 5.0, "seed": 100 + tnum,
                }), "utf-8")

            # Default export delivers selected take (T01)
            res = worker_runtime.export_episode_to_downloads(data, db, ep["id"], downloads_root=dl)
            self.assertTrue(res["ok"])
            self.assertEqual(res["exportedCount"], 1)
            dest = Path(res["destination"])
            self.assertTrue((dest / "EP01S04_T01.mp4").is_file())
            self.assertEqual((dest / "EP01S04_T01.mp4").read_bytes(), b"take1-mp4")
            manifest = json.loads((dest / "manifest.json").read_text("utf-8"))
            self.assertEqual(manifest["items"][0]["filename"], "EP01S04_T01.mp4")
            self.assertTrue(manifest["items"][0]["selected"])

            # all_takes=True delivers both T01 and T02 without removing anything
            res_all = worker_runtime.export_episode_to_downloads(data, db, ep["id"], downloads_root=dl, all_takes=True)
            self.assertEqual(res_all["exportedCount"], 2)
            self.assertTrue((dest / "EP01S04_T02.mp4").is_file())
            self.assertEqual((dest / "EP01S04_T02.mp4").read_bytes(), b"take2-mp4")

    def test_end_to_end_minimax_h3_worker_pipeline_without_vast_spend(self):
        import threading
        from types import SimpleNamespace
        import comfy_client
        import direct_session
        import workflow_profiles as wprof
        import worker_config

        pkg_path = Path(__file__).resolve().parent / "packages" / "MiniMax_H3.zip"
        raw_zip = pkg_path.read_bytes()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "episodes"
            (data / "production").mkdir(parents=True)
            dl = root / "Downloads"
            lib = root / "library"
            lib.mkdir()
            for fname, blob in (("tom.png", b"TOM-PNG"), ("emma.png", b"EMMA-PNG"), ("jack.png", b"JACK-PNG")):
                (lib / fname).write_bytes(blob)

            store = wprof.WorkflowStore(root / "profiles")
            wf = store.import_package(pkg_path.name, raw_zip)
            cfg = worker_config.WorkerConfigStore(data / "production" / "worker-settings.json", root / "overrides")
            cfg.update({
                "seedMode": "random",
                "resolutionPreset": "720p",
                "aspectRatio": "16:9",
                "autoExportDownloads": True,
                "continuousExecution": True,
            })
            logs = worker_runtime.WorkerLogStore(data / "production" / "worker.log")

            db = se.new_project()
            ep = se.new_episode("Episode 1: Pilot")
            db["episodes"] = [ep]
            s1 = {"id": str(uuid.uuid4()), "revision": "rev1", "scene_id": "sc1", "shot_code": "EP01S01",
                  "seconds": 5, "prompt": "Tom and Emma talk.", "change": "", "image": "tom.png", "refs": ["emma.png"],
                  "approved": True, "skipped": False, "takes": [], "selected_take_id": ""}
            s2 = {"id": str(uuid.uuid4()), "revision": "rev2", "scene_id": "sc1", "shot_code": "EP01S02",
                  "seconds": 5, "prompt": "Tom walks alone.", "change": "", "image": "tom.png", "refs": [],
                  "approved": True, "skipped": False, "takes": [], "selected_take_id": ""}
            ep["shots"] = [s1, s2]

            uploaded_names = set()
            upload_count = {"n": 0}
            submitted_graphs = []
            obj_info = {
                "ResolutionSelector": {
                    "input": {"required": {"aspect_ratio": [["9:16 (Portrait Widescreen)", "16:9 (Landscape Widescreen)"]], "megapixels": ["FLOAT"], "multiple": ["INT"]}}
                },
                "MinimaxH3LatentUpscaler3D": {"input": {"required": {}}},
            }

            def fake_upload(_url, _asset, name):
                if name not in uploaded_names:
                    uploaded_names.add(name)
                    upload_count["n"] += 1
                return name

            def fake_api(_url, path, obj=None):
                if path == "/object_info":
                    return {n["class_type"]: obj_info.get(n["class_type"], {}) for n in wf["graph"].values()}
                if path.startswith("/object_info/"):
                    kind = path.split("/object_info/", 1)[1]
                    return {kind: obj_info.get(kind, {})}
                if path == "/prompt":
                    submitted_graphs.append(obj["prompt"])
                    return {"prompt_id": f"ticket-{len(submitted_graphs)}"}
                if path == "/queue":
                    return {"queue_running": [], "queue_pending": []}
                if path == "/internal/logs/raw":
                    return {"entries": [{"t": "2026-11-25T10:00:00", "m": "Ready"}]}
                if path.startswith("/history/"):
                    t = path.rsplit("/", 1)[-1]
                    return {t: {"status": {"status_str": "success", "completed": True}, "outputs": {"141": {"gifs": [{"filename": "out.mp4"}]}}}}
                raise AssertionError(f"Unexpected path {path}")

            asset_map = {p.name: p for p in lib.iterdir()}
            ctx = {
                "DATA": data, "LOCK": threading.RLock(), "json_api": fake_api,
                "prompt": comfy_client.prompt,
                "generation_images": comfy_client.generation_images,
                "generation_voices": comfy_client.generation_voices,
                "prepare_shot": lambda u, f, e, s, w, r: comfy_client.prepare_shot(
                    u, f, e, s, w, r, asset_map=asset_map, upload_fn=fake_upload, json_api_fn=fake_api
                ),
                "download_output": lambda _u, f, _h, j: (f / "video.part").write_bytes(movie(j["playback_seconds"], True)),
                "take_quality": SimpleNamespace(check=lambda _p, _j: {"technical": "passed", "width": 1280, "height": 704, "frames": 124, "audioStreams": 1}),
                "worker_config": cfg,
                "worker_logs": logs,
                "export_downloads": lambda eid: worker_runtime.export_episode_to_downloads(data, db, eid, downloads_root=dl),
                "running": True,
            }
            session = direct_session.DirectSession(ctx)
            jobs = [[ep["id"], s1], [ep["id"], s2]]
            view = {"end": time.time() + 600, "episode": ep["id"], "submitted": 2, "phase": "Preparing ComfyUI", "completed": 0, "failed": 0}
            session.start(view, jobs, wf, None, "http://fixture", worker_settings=cfg.get())
            session.plan["sampleVerified"] = True  # Skip first-shot log gate (tested separately in test_workflow_manager)
            with patch.object(production.time, "sleep"):
                session.run("http://fixture", wf)

            self.assertEqual(view["phase"], "Finished")
            self.assertEqual(view["completed"], 2)
            # Deduplicated uploads: tom.png uploaded ONCE (reused in shot 2), emma.png ONCE, jack.png NEVER uploaded!
            self.assertEqual(upload_count["n"], 2)
            # Both graphs applied 16:9 aspect ratio, distinct random seeds, unique filename_prefix, and skipped unused refs!
            self.assertEqual(len(submitted_graphs), 2)
            self.assertEqual(submitted_graphs[0]["200"]["inputs"]["aspect_ratio"], "16:9 (Landscape Widescreen)")
            self.assertEqual(submitted_graphs[0]["141"]["inputs"]["filename_prefix"], f"AXDAR/{ep['id']}/EP01S01_T01")
            self.assertEqual(submitted_graphs[1]["141"]["inputs"]["filename_prefix"], f"AXDAR/{ep['id']}/EP01S02_T01")
            self.assertNotEqual(submitted_graphs[0]["63"]["inputs"]["noise_seed"], submitted_graphs[1]["63"]["inputs"]["noise_seed"])
            # Shot 1 had 2 ref images (Tom, Emma); Shot 2 had only 1 ref image (Tom)
            s1_refs = [k for k in submitted_graphs[0]["56"]["inputs"] if k.startswith("ref_images.")]
            s2_refs = [k for k in submitted_graphs[1]["56"]["inputs"] if k.startswith("ref_images.")]
            self.assertEqual(len(s1_refs), 2)
            self.assertEqual(len(s2_refs), 1)
            # Auto-export delivered both organized MP4s + manifest into Downloads
            dest = Path(view["exportDestination"])
            self.assertEqual(view["exportedCount"], 2)
            self.assertTrue((dest / "EP01S01_T01.mp4").is_file())
            self.assertTrue((dest / "EP01S02_T01.mp4").is_file())
            self.assertTrue((dest / "manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()
