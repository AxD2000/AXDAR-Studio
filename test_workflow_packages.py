"""Comprehensive local tests for Workflow Packages (.zip), strict preflight, non-MiniMax portability, and readiness."""
import copy
import hashlib
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import production
import provision
import workflow_install_remote as remote
import workflow_manager
import workflow_packages as wpkg
import workflow_profiles as wprof

ROOT = Path(__file__).resolve().parent
LOCAL_COMFY_PORTABLE = Path(r"C:\Users\DIGITAL WORLD\Desktop\cai\ComfyUI_windows_portable")


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def make_generic_t2v_package(*, comfy_rev="b0b743566f65daafc423b4fea8a2fbda94b3384a", min_vram=8,
                             attention=None, model_payload=b"generic-ckpt-bytes"):
    """Build a structurally different, non-MiniMax workflow package (frames+fps, negative prompt, LoadImage, checkpoints)."""
    ckpt_sha = _sha256_bytes(model_payload)
    manifest = {
        "schemaVersion": 1,
        "id": "generic-t2v-alt",
        "name": "Generic Alt Video Pipeline",
        "version": "2.1.0",
        "description": "Non-MiniMax test package proving zero hardcoded model/node assumptions.",
        "comfyui": {"repo": "comfyanonymous/ComfyUI", "revision": comfy_rev},
        "runtime": {
            "python": {"min": "3.10", "max": "3.14"},
            "torch": {"min": "2.0.0", "max": "3.0.0"},
            "cuda": {"min": "11.8", "max": "13.9"},
            "minVramGb": min_vram,
            "computeCapabilities": ["8.0", "8.6", "8.9", "9.0", "12.0"],
            "attention": attention or {"backend": "none"},
        },
    }
    workflow = {
        "1": {"class_type": "AltCheckpointLoader", "inputs": {"ckpt_name": "alt_video_v2.safetensors"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "positive placeholder", "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry, artifacts", "clip": ["1", 1]}},
        "4": {"class_type": "LoadImage", "inputs": {"image": "sample.png"}},
        "5": {
            "class_type": "AltVideoSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "image": ["4", 0],
                "frame_count": 32,
                "cfg": 6.5,
            },
        },
        "6": {
            "class_type": "VHS_VideoCombine",
            "inputs": {"images": ["5", 0], "frame_rate": 16, "format": "video/h264-mp4", "save_output": True},
        },
    }
    nodes = [
        {
            "repo": "example-org/ComfyUI-AltVideoNodes",
            "revision": "1" * 40,
            "classes": ["AltCheckpointLoader", "AltVideoSampler"],
            "requirements": ["einops==0.8.0"],
        },
        {
            "repo": "Kosinkadink/ComfyUI-VideoHelperSuite",
            "revision": "2" * 40,
            "classes": ["VHS_VideoCombine"],
            "requirements": [],
        },
    ]
    models = [
        {
            "folder": "checkpoints",
            "repo": "example-org/AltVideo-Weights",
            "file": "weights/alt_video_v2.safetensors",
            "revision": "3" * 40,
            "sha256": ckpt_sha,
            "bytes": len(model_payload),
        }
    ]
    mapping = {
        "positive": "2.text",
        "negative": "3.text",
        "duration": "",
        "frames": "5.frame_count",
        "images": ["4.image"],
        "audio": [],
        "output": "6",
        "parameters": {"5.cfg": 6.5},
        "fps": 16,
        "minFrames": 16,
        "maxFrames": 160,
        "frameStep": 8,
        "frameBase": 16,
    }
    return manifest, workflow, nodes, models, mapping


def _make_raw_zip(files_dict):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files_dict.items():
            zf.writestr(name, content if isinstance(content, (bytes, str)) else json.dumps(content))
    return buf.getvalue()


class WorkflowPackageValidationTests(unittest.TestCase):
    def test_all_five_files_are_mandatory(self):
        manifest, wf, nodes, models, mapping = make_generic_t2v_package()
        base = {
            "manifest.json": manifest,
            "workflow.json": wf,
            "nodes.json": nodes,
            "model-lock.json": models,
            "mapping.json": mapping,
        }
        for missing_name in list(base):
            subset = {k: v for k, v in base.items() if k != missing_name}
            with self.assertRaisesRegex(ValueError, missing_name):
                wpkg.parse_package(_make_raw_zip(subset), "bad.zip")

    def test_rejects_standalone_json_models_txt_shell_scripts_and_traversal(self):
        with self.assertRaisesRegex(ValueError, "Standalone JSON"):
            wpkg.parse_package(b'{"1":{"class_type":"X","inputs":{}}}', "standalone.json")

        manifest, wf, nodes, models, mapping = make_generic_t2v_package()
        base = {
            "manifest.json": manifest,
            "workflow.json": wf,
            "nodes.json": nodes,
            "model-lock.json": models,
            "mapping.json": mapping,
        }
        with self.assertRaisesRegex(ValueError, "models.txt"):
            wpkg.parse_package(_make_raw_zip({**base, "models.txt": "checkpoints a/b main f.safetensors"}), "bad.zip")
        with self.assertRaisesRegex(ValueError, "Shell scripts"):
            wpkg.parse_package(_make_raw_zip({**base, "setup.sh": "#!/bin/bash\necho hi"}), "bad.zip")
        with self.assertRaisesRegex(ValueError, "Unsafe path"):
            wpkg.parse_package(_make_raw_zip({**base, "../escape.json": "{}"}), "bad.zip")

    def test_enforces_pinned_revisions_pinned_requirements_and_model_lock_coverage(self):
        manifest, wf, nodes, models, mapping = make_generic_t2v_package()
        # Unpinned ComfyUI revision
        bad_m = json.loads(json.dumps(manifest))
        bad_m["comfyui"]["revision"] = "main"
        with self.assertRaisesRegex(ValueError, "40-character"):
            wpkg.build_package_bytes(bad_m, wf, nodes, models, mapping)

        # Unpinned custom node revision
        bad_n = json.loads(json.dumps(nodes))
        bad_n[0]["revision"] = "latest"
        with self.assertRaisesRegex(ValueError, "40-character"):
            wpkg.build_package_bytes(manifest, wf, bad_n, models, mapping)

        # Unpinned Python requirement
        bad_n2 = json.loads(json.dumps(nodes))
        bad_n2[0]["requirements"] = ["einops>=0.7.0"]
        with self.assertRaisesRegex(ValueError, "unpinned"):
            wpkg.build_package_bytes(manifest, wf, bad_n2, models, mapping)

        # Protected GPU package override
        bad_n3 = json.loads(json.dumps(nodes))
        bad_n3[0]["requirements"] = ["torch==2.5.0"]
        with self.assertRaisesRegex(ValueError, "GPU runtime"):
            wpkg.build_package_bytes(manifest, wf, bad_n3, models, mapping)

        # Unlisted model referenced in workflow.json
        with self.assertRaisesRegex(ValueError, "not locked in model-lock.json"):
            wpkg.build_package_bytes(manifest, wf, nodes, [], mapping)

        # Windows backslash in workflow.json model path must be rejected for Linux compatibility
        bad_wf = json.loads(json.dumps(wf))
        bad_wf["1"]["inputs"]["ckpt_name"] = "sub\\alt_video_v2.safetensors"
        with self.assertRaisesRegex(ValueError, "Windows backslash"):
            wpkg.build_package_bytes(manifest, bad_wf, nodes, models, mapping)

        # SageAttention without explicit boolean allowSdpaFallback
        bad_att = json.loads(json.dumps(manifest))
        bad_att["runtime"]["attention"] = {"backend": "sageattention", "revision": "a" * 40}
        with self.assertRaisesRegex(ValueError, "allowSdpaFallback"):
            wpkg.build_package_bytes(bad_att, wf, nodes, models, mapping)

    def test_minimax_h3_reference_package_validates_and_builds_runtime_graph(self):
        pkg_path = ROOT / "packages" / "MiniMax_H3.zip"
        self.assertTrue(pkg_path.is_file(), "packages/MiniMax_H3.zip must exist")
        raw = pkg_path.read_bytes()
        parsed = wpkg.parse_package(raw, pkg_path.name)
        self.assertEqual(parsed["manifest"]["id"], "minimax-h3-singularity")
        self.assertEqual(len(parsed["nodes"]), 5)
        self.assertEqual(len(parsed["models"]), 8)
        with tempfile.TemporaryDirectory() as tmp:
            store = wprof.WorkflowStore(Path(tmp))
            wf = store.import_package(pkg_path.name, raw)
            self.assertTrue(wf["packaged"])
            self.assertEqual(wf["issues"], [])
            # Packaged workflows forbid dynamic remapping
            with self.assertRaisesRegex(ValueError, "authoritative mapping.json"):
                store.remap(wf["version"], wf["mapping"])
            shot = {"id": "s1", "seconds": 5, "prompt": "Hero walks in"}
            graph = wprof.runtime_graph(wf, shot, "Hero walks in", ["hero.png"], ["hero.mp3"], lambda n: "up_" + n, "AXDAR/test")
            self.assertEqual(graph["84"]["inputs"]["value"], "Hero walks in")
            self.assertEqual(graph["74"]["inputs"]["value"], 5)
            self.assertEqual(graph["141"]["class_type"], "VHS_VideoCombine")

    def test_second_non_minimax_package_runs_end_to_end_without_minimax_assumptions(self):
        payload = b"alt-checkpoint-payload-0123456789"
        manifest, wf_graph, nodes, models, mapping = make_generic_t2v_package(model_payload=payload)
        raw_zip = wpkg.build_package_bytes(manifest, wf_graph, nodes, models, mapping)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = wprof.WorkflowStore(root / "profiles")
            wf = store.import_package("Generic_Alt.zip", raw_zip)
            self.assertTrue(wf["packaged"])
            self.assertEqual(wf["adapter"], "generic-t2v-alt")
            self.assertEqual(wf["issues"], [])
            # Verify runtime_graph binds positive, negative, frames (2s @ 16fps = 32), LoadImage
            shot = {"id": "s2", "seconds": 2, "negativePrompt": "low quality"}
            runtime = wprof.runtime_graph(wf, shot, "A sunny meadow", ["meadow.png"], [], lambda n: "up_" + n, "AXDAR/alt")
            self.assertEqual(runtime["2"]["inputs"]["text"], "A sunny meadow")
            self.assertEqual(runtime["3"]["inputs"]["text"], "low quality")
            self.assertEqual(runtime["4"]["inputs"]["image"], "up_meadow.png")
            self.assertEqual(runtime["5"]["inputs"]["frame_count"], 32)
            self.assertEqual(runtime["5"]["inputs"]["cfg"], 6.5)

            # Verify remote package installer executes strictly from this package (NOT global MiniMax tables)
            comfy_root = root / "ComfyUI"
            (comfy_root / "main.py").parent.mkdir(parents=True)
            (comfy_root / "main.py").write_text("# comfy")
            (comfy_root / "models" / "checkpoints").mkdir(parents=True)
            run_dir = root / "install_run"
            run_dir.mkdir()

            installed_node_calls = []
            downloaded_model_calls = []

            def fake_install_nodes(r, specs):
                installed_node_calls.extend(s["repo"] for s in specs)
                for s in specs:
                    d = r / "custom_nodes" / s["repo"].split("/")[1]
                    d.mkdir(parents=True, exist_ok=True)
                return [s["repo"] for s in specs]

            def fake_install_models(r, specs, progress=None):
                for s in specs:
                    downloaded_model_calls.append(s["file"])
                    dest = r / "models" / s["folder"] / Path(s["file"]).name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(payload)
                return True

            fake_env = {
                "comfyuiRoot": str(comfy_root),
                "comfyuiRevision": manifest["comfyui"]["revision"],
                "pythonVersion": "3.12.4",
                "torchVersion": "2.6.0+cu124",
                "cudaVersion": "12.4",
                "gpuName": "Mock RTX 4090",
                "gpuArch": "8.9",
                "vramGb": 24.0,
                "diskFreeBytes": 500 * 1024**3,
                "environmentFingerprint": "a" * 64,
            }

            def fake_git_head(p):
                p = Path(p)
                if p == comfy_root:
                    return manifest["comfyui"]["revision"]
                for s in nodes:
                    if p.name == s["repo"].split("/")[1] and p.is_dir():
                        return s["revision"]
                return None

            after_restart = {"restarted": False}

            def fake_api(url, timeout=10):
                class Resp:
                    def __init__(self, obj):
                        self.data = json.dumps(obj).encode()
                    def __enter__(self):
                        return io.BytesIO(self.data)
                    def __exit__(self, *a):
                        pass

                if url.endswith("/queue"):
                    return Resp({"queue_running": [], "queue_pending": []})
                if after_restart["restarted"]:
                    return Resp({
                        "AltCheckpointLoader": {"input": {"required": {"ckpt_name": [["alt_video_v2.safetensors"]]}}},
                        "CLIPTextEncode": {"input": {"required": {"text": ["STRING"]}}},
                        "LoadImage": {"input": {"required": {"image": [["sample.png"]]}}},
                        "AltVideoSampler": {"input": {"required": {"frame_count": ["INT"]}}},
                        "VHS_VideoCombine": {"input": {"required": {"frame_rate": ["INT"]}}},
                    })
                return Resp({
                    "CLIPTextEncode": {"input": {"required": {"text": ["STRING"]}}},
                    "LoadImage": {"input": {"required": {"image": [["sample.png"]]}}},
                })

            def fake_subproc(cmd, **kw):
                if cmd[:2] == ["supervisorctl", "restart"]:
                    after_restart["restarted"] = True
                    return SimpleNamespace(returncode=0)
                raise AssertionError(f"Unexpected subprocess: {cmd}")

            spec = {
                "packaged": True,
                "version": wf["version"],
                "manifest": manifest,
                "nodes": nodes,
                "models": models,
                "graph": wf_graph,
                "mode": "install",
                "port": 8188,
                "timeout": 60,
            }
            with patch.object(remote, "root_path", return_value=comfy_root), \
                 patch.object(remote, "urlopen", side_effect=fake_api), \
                 patch.object(remote, "probe_environment", return_value=fake_env), \
                 patch.object(provision, "git_head", side_effect=fake_git_head), \
                 patch.object(provision, "install_package_nodes", side_effect=fake_install_nodes), \
                 patch.object(provision, "install_package_models", side_effect=fake_install_models), \
                 patch.object(remote.subprocess, "run", side_effect=fake_subproc):
                code = remote.execute(run_dir, spec)
            self.assertEqual(code, 0)
            self.assertEqual(installed_node_calls, ["example-org/ComfyUI-AltVideoNodes", "Kosinkadink/ComfyUI-VideoHelperSuite"])
            self.assertEqual(downloaded_model_calls, ["weights/alt_video_v2.safetensors"])
            stage_state = production.read(run_dir / "stage.json")
            self.assertTrue(stage_state["report"]["compatible"])
            self.assertEqual(stage_state["report"]["attention"]["status"], "Not required")


class CompatibilityAndReadinessTests(unittest.TestCase):
    def test_incompatible_environment_blocks_installation_before_mutation(self):
        manifest, wf_graph, nodes, models, _mapping = make_generic_t2v_package(min_vram=48)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            comfy_root = root / "ComfyUI"
            (comfy_root / "main.py").parent.mkdir(parents=True)
            (comfy_root / "main.py").write_text("# comfy")
            # Also plant a conflicting custom node revision on disk
            conflict_node = comfy_root / "custom_nodes" / "ComfyUI-AltVideoNodes"
            conflict_node.mkdir(parents=True)
            # And plant a corrupt model file at the exact target path
            bad_model = comfy_root / "models" / "checkpoints" / "alt_video_v2.safetensors"
            bad_model.parent.mkdir(parents=True)
            bad_model.write_bytes(b"wrong-size")

            wrong_env = {
                "comfyuiRoot": str(comfy_root),
                "comfyuiRevision": "f" * 40,  # Wrong ComfyUI commit!
                "pythonVersion": "3.9.18",   # Too old!
                "torchVersion": "1.13.1",    # Too old!
                "cudaVersion": "11.0",       # Too old!
                "gpuName": "Small GPU",
                "gpuArch": "7.5",            # Unsupported compute capability!
                "vramGb": 12.0,              # Insufficient VRAM!
                "diskFreeBytes": 100 * 1024**3,
                "environmentFingerprint": "e" * 64,
            }
            with patch.object(provision, "git_head", side_effect=lambda p: "9" * 40 if Path(p) == conflict_node else "f" * 40):
                compat = remote.check_package_compatibility(wrong_env, manifest, comfy_root, nodes, models)
            self.assertFalse(compat["compatible"])
            joined = "\n".join(compat["incompatibilities"])
            for expected in ("ComfyUI revision", "Python version", "PyTorch version", "CUDA version",
                             "Insufficient GPU VRAM", "compute capability", "Conflicting custom node", "Conflicting model file"):
                self.assertIn(expected, joined)

            # Execute must return exit code 3 (incompatible) WITHOUT calling install_package_nodes or install_package_models
            run_dir = root / "run"
            run_dir.mkdir()
            spec = {"packaged": True, "version": "a" * 64, "manifest": manifest, "nodes": nodes,
                    "models": models, "graph": wf_graph, "mode": "install", "port": 8188, "timeout": 60}

            class Resp:
                def __enter__(self):
                    return io.BytesIO(b"{}")
                def __exit__(self, *a):
                    pass

            with patch.object(remote, "root_path", return_value=comfy_root), \
                 patch.object(remote, "urlopen", return_value=Resp()), \
                 patch.object(remote, "probe_environment", return_value=wrong_env), \
                 patch.object(provision, "git_head", side_effect=lambda p: "9" * 40 if Path(p) == conflict_node else "f" * 40), \
                 patch.object(provision, "install_package_nodes") as inst_nodes, \
                 patch.object(provision, "install_package_models") as inst_models:
                code = remote.execute(run_dir, spec)
            self.assertEqual(code, 3)
            inst_nodes.assert_not_called()
            inst_models.assert_not_called()

    def test_sdpa_fallback_is_forbidden_unless_manifest_explicitly_authorizes_it(self):
        for allow in (False, True):
            payload = b"sage-test-model"
            manifest, wf_graph, nodes, models, _mapping = make_generic_t2v_package(
                attention={"backend": "sageattention", "revision": "e" * 40, "allowSdpaFallback": allow},
                model_payload=payload,
            )
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                comfy_root = root / "ComfyUI"
                (comfy_root / "main.py").parent.mkdir(parents=True)
                (comfy_root / "main.py").write_text("# comfy")
                for s in nodes:
                    (comfy_root / "custom_nodes" / s["repo"].split("/")[1]).mkdir(parents=True)
                model_file = comfy_root / "models" / "checkpoints" / "alt_video_v2.safetensors"
                model_file.parent.mkdir(parents=True)
                model_file.write_bytes(payload)
                run_dir = root / "run"
                run_dir.mkdir()
                ok_env = {
                    "comfyuiRoot": str(comfy_root),
                    "comfyuiRevision": manifest["comfyui"]["revision"],
                    "pythonVersion": "3.12.4",
                    "torchVersion": "2.6.0+cu124",
                    "cudaVersion": "12.4",
                    "gpuName": "Mock GPU",
                    "gpuArch": "8.9",
                    "vramGb": 24.0,
                    "diskFreeBytes": 500 * 1024**3,
                    "environmentFingerprint": "b" * 64,
                }
                obj_info = {
                    "AltCheckpointLoader": {"input": {"required": {"ckpt_name": [["alt_video_v2.safetensors"]]}}},
                    "CLIPTextEncode": {"input": {"required": {"text": ["STRING"]}}},
                    "LoadImage": {"input": {"required": {"image": [["sample.png"]]}}},
                    "AltVideoSampler": {"input": {"required": {"frame_count": ["INT"]}}},
                    "VHS_VideoCombine": {"input": {"required": {"frame_rate": ["INT"]}}},
                }

                def fake_api(url, timeout=10):
                    class Resp:
                        def __init__(self, obj):
                            self.d = json.dumps(obj).encode()
                        def __enter__(self):
                            return io.BytesIO(self.d)
                        def __exit__(self, *a):
                            pass
                    return Resp({"queue_running": [], "queue_pending": []} if url.endswith("/queue") else obj_info)

                def fake_git(p):
                    p = Path(p)
                    if p == comfy_root:
                        return manifest["comfyui"]["revision"]
                    for s in nodes:
                        if p.name == s["repo"].split("/")[1]:
                            return s["revision"]
                    return None

                spec = {"packaged": True, "version": "c" * 64, "manifest": manifest, "nodes": nodes,
                        "models": models, "graph": wf_graph, "mode": "install", "port": 8188, "timeout": 60}
                with patch.object(remote, "root_path", return_value=comfy_root), \
                     patch.object(remote, "urlopen", side_effect=fake_api), \
                     patch.object(remote, "probe_environment", return_value=ok_env), \
                     patch.object(provision, "git_head", side_effect=fake_git), \
                     patch.object(provision, "install_package_nodes", return_value=[]), \
                     patch.object(provision, "sage_ready", return_value=False), \
                     patch.object(provision, "install_sage", side_effect=subprocess.CalledProcessError(1, ["ninja"])):
                    code = remote.execute(run_dir, spec)
                if allow:
                    self.assertEqual(code, 0)
                    stage_state = production.read(run_dir / "stage.json")
                    self.assertIn("authorized by manifest", stage_state["report"]["attention"]["status"])
                else:
                    self.assertEqual(code, 1)

    def test_installation_does_not_grant_production_readiness_and_tracks_exact_env_tuple(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn = Mock()
            conn.target.return_value = {"id": 77, "host": "h", "port": 22}
            conn.snapshot.return_value = {"instance": 77}
            ctx = {"BASE": ROOT, "DATA": root, "LOCK": threading.RLock(), "connection": conn, "running": False}
            mgr = workflow_manager.WorkflowManager(ctx)
            v1 = "1" * 64
            v2 = "2" * 64
            report_env_a = {"environmentFingerprint": "a" * 64, "compatible": True, "incompatibilities": [], "environment": {"gpuName": "A"}}
            report_env_b = {"environmentFingerprint": "b" * 64, "compatible": True, "incompatibilities": [], "environment": {"gpuName": "B"}}

            # 1) Installation completes -> installationStatus=='installed', productionReadiness MUST remain 'not_verified'
            entry = mgr.record_environment_verification(v1, "77", "installed", report_env_a)
            self.assertEqual(entry["installationStatus"], "installed")
            self.assertEqual(entry["productionReadiness"], "not_verified")
            self.assertFalse(mgr.is_production_ready(v1, "77", "a" * 64))

            # 2) Real verified video generation marks ONLY (v1, 77, env_a) as production ready
            mgr.record_production_readiness(
                v1, "77",
                {"takeId": "t1", "shotId": "s1", "sha256": "d" * 64, "duration": 2.0, "width": 512, "height": 512, "frames": 32, "technical": "passed"},
                passed=True, env_fingerprint="a" * 64,
            )
            self.assertTrue(mgr.is_production_ready(v1, "77", "a" * 64))
            # Different package version, different instance, or different environment fingerprint MUST NOT be ready
            self.assertFalse(mgr.is_production_ready(v2, "77", "a" * 64))
            self.assertFalse(mgr.is_production_ready(v1, "88", "a" * 64))
            self.assertFalse(mgr.is_production_ready(v1, "77", "b" * 64))

            # 3) Environment changes on same instance -> readiness is NOT inherited from old fingerprint
            mgr.record_environment_verification(v1, "77", "installed", report_env_b)
            self.assertFalse(mgr.is_production_ready(v1, "77"))

    def test_incompatible_package_blocks_workflow_switching(self):
        from studio_actions import StudioActions
        manifest, wf_graph, nodes, models, mapping = make_generic_t2v_package()
        raw_zip = wpkg.build_package_bytes(manifest, wf_graph, nodes, models, mapping)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn = Mock()
            conn.target.return_value = {"id": 55, "host": "h", "port": 22}
            conn.snapshot.return_value = {"instance": 55}
            ctx = {"BASE": root, "DATA": root / "episodes", "LOCK": threading.RLock(), "connection": conn, "running": False, "active_workflow": None, "workflow_error": ""}
            (root / "episodes" / "production").mkdir(parents=True)
            mgr = workflow_manager.WorkflowManager(ctx)
            ctx["workflow_manager"] = mgr
            actions = StudioActions(ctx)
            wf = actions.store.import_package("Alt.zip", raw_zip, select=False)
            mgr.record_environment_verification(
                wf["version"], "55", "incompatible",
                {"compatible": False, "incompatibilities": ["Insufficient GPU VRAM"], "environmentFingerprint": "c" * 64},
            )
            with self.assertRaisesRegex(ValueError, "Cannot switch to incompatible workflow package"):
                actions.workflow("select", {"version": wf["version"]})

    def test_comfyui_mismatch_reports_cleanly_and_aligns_only_when_explicitly_confirmed(self):
        payload = b"align-test-model"
        manifest, wf_graph, nodes, models, _mapping = make_generic_t2v_package(model_payload=payload)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            comfy_root = root / "ComfyUI"
            (comfy_root / "main.py").parent.mkdir(parents=True)
            (comfy_root / "main.py").write_text("# comfy")
            for s in nodes:
                (comfy_root / "custom_nodes" / s["repo"].split("/")[1]).mkdir(parents=True)
            model_file = comfy_root / "models" / "checkpoints" / "alt_video_v2.safetensors"
            model_file.parent.mkdir(parents=True)
            model_file.write_bytes(payload)
            run_dir = root / "run"
            run_dir.mkdir()

            state = {"comfy_rev": "f" * 40, "restarted": False}

            def make_env(*_a):
                return {
                    "comfyuiRoot": str(comfy_root),
                    "comfyuiRevision": state["comfy_rev"],
                    "comfyuiGitClean": True,
                    "pythonVersion": "3.12.4",
                    "torchVersion": "2.10.0+cu130",
                    "cudaVersion": "13.0",
                    "gpuName": "RTX 5090",
                    "gpuArch": "12.0",
                    "vramGb": 32.0,
                    "diskFreeBytes": 500 * 1024**3,
                    "environmentFingerprint": ("e" if state["comfy_rev"] == "f" * 40 else "a") * 64,
                }

            obj_info = {
                "AltCheckpointLoader": {"input": {"required": {"ckpt_name": [["alt_video_v2.safetensors"]]}}},
                "CLIPTextEncode": {"input": {"required": {"text": ["STRING"]}}},
                "LoadImage": {"input": {"required": {"image": [["sample.png"]]}}},
                "AltVideoSampler": {"input": {"required": {"frame_count": ["INT"]}}},
                "VHS_VideoCombine": {"input": {"required": {"frame_rate": ["INT"]}}},
            }

            def fake_api(url, timeout=10):
                class Resp:
                    def __init__(self, obj):
                        self.d = json.dumps(obj).encode()
                    def __enter__(self):
                        return io.BytesIO(self.d)
                    def __exit__(self, *a):
                        pass
                return Resp({"queue_running": [], "queue_pending": []} if url.endswith("/queue") else obj_info)

            def fake_git_head(p):
                p = Path(p)
                if p == comfy_root:
                    return state["comfy_rev"]
                for s in nodes:
                    if p.name == s["repo"].split("/")[1]:
                        return s["revision"]
                return None

            def fake_align(r, repo, expected):
                self.assertEqual(expected, manifest["comfyui"]["revision"])
                state["comfy_rev"] = expected
                return True

            spec = {"packaged": True, "version": "c" * 64, "manifest": manifest, "nodes": nodes,
                    "models": models, "graph": wf_graph, "mode": "install", "port": 8188, "timeout": 60}

            # 1) Without alignComfyui=True, mismatch MUST block with exit code 3 and actionable hint (never silent)
            with patch.object(remote, "root_path", return_value=comfy_root), \
                 patch.object(remote, "urlopen", side_effect=fake_api), \
                 patch.object(remote, "probe_environment", side_effect=make_env), \
                 patch.object(provision, "git_head", side_effect=fake_git_head), \
                 patch.object(provision, "align_comfyui_revision") as align_mock:
                code = remote.execute(run_dir, spec)
            self.assertEqual(code, 3)
            align_mock.assert_not_called()
            rep = production.read(run_dir / "stage.json")["report"]
            self.assertTrue(rep["comfyuiMismatch"]["canAutoAlign"])
            self.assertIn("no Jupyter Terminal needed", rep["incompatibilities"][0])

            # 2) With explicit alignComfyui=True, aligns ComfyUI, restarts, re-probes, and succeeds
            with patch.object(remote, "root_path", return_value=comfy_root), \
                 patch.object(remote, "urlopen", side_effect=fake_api), \
                 patch.object(remote, "probe_environment", side_effect=make_env), \
                 patch.object(provision, "git_head", side_effect=fake_git_head), \
                 patch.object(provision, "align_comfyui_revision", side_effect=fake_align) as align_mock, \
                 patch.object(provision, "install_package_nodes", return_value=[]), \
                 patch.object(remote.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as restart_mock:
                code = remote.execute(run_dir, dict(spec, alignComfyui=True))
            self.assertEqual(code, 0)
            align_mock.assert_called_once()
            restart_mock.assert_called_once()

    def test_pre_production_rechecks_live_environment_fingerprint_and_blocks_drift(self):
        manifest, wf_graph, nodes, models, mapping = make_generic_t2v_package()
        raw_zip = wpkg.build_package_bytes(manifest, wf_graph, nodes, models, mapping)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = wprof.WorkflowStore(root / "profiles")
            wf = store.import_package("Alt.zip", raw_zip)
            target = {"id": 91, "host": "h", "port": 22, "comfy_port": 8188}
            conn = Mock()
            conn.target.return_value = target
            conn.snapshot.return_value = {"instance": 91}
            ssh_ops = Mock()
            ctx = {"BASE": ROOT, "DATA": root, "LOCK": threading.RLock(), "connection": conn, "running": False}
            mgr = workflow_manager.WorkflowManager(ctx, ssh_ops=ssh_ops)

            # 1) Never installed -> pre-production check MUST block
            with self.assertRaisesRegex(ValueError, "Install and verify"):
                mgr.verify_live_environment(wf["version"], target, wf)

            # 2) Installed with fingerprint F1 -> live check returns matching F1 -> passes
            fp1 = "1" * 64
            mgr.record_environment_verification(
                wf["version"], "91", "installed",
                {"compatible": True, "incompatibilities": [], "environmentFingerprint": fp1, "environment": {"gpuName": "RTX 5090"}},
            )
            live_ok = {
                "compatible": True, "incompatibilities": [], "environmentFingerprint": fp1,
                "nodes": [], "nodeRepos": [{"repo": n["repo"], "status": "Installed"} for n in nodes],
                "models": [{"name": "alt_video_v2.safetensors", "status": "Installed"}],
            }
            ssh_ops.ssh.side_effect = [b"", json.dumps(live_ok).encode()]
            self.assertEqual(mgr.verify_live_environment(wf["version"], target, wf), fp1)

            # 3) Live environment fingerprint drifted to F2 -> MUST block production before any prompt
            live_drift = dict(live_ok, environmentFingerprint="2" * 64)
            ssh_ops.ssh.side_effect = [b"", json.dumps(live_drift).encode()]
            with self.assertRaisesRegex(ValueError, "fingerprint changed"):
                mgr.verify_live_environment(wf["version"], target, wf)

    def test_remote_package_zip_unpack_and_structured_metrics(self):
        payload = b"zip-unpack-model-bytes"
        manifest, wf_graph, nodes, models, mapping = make_generic_t2v_package(model_payload=payload)
        raw_zip = wpkg.build_package_bytes(manifest, wf_graph, nodes, models, mapping)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "remote_run"
            run_dir.mkdir()
            (run_dir / "package.zip").write_bytes(raw_zip)
            unpacked = remote._unpack_remote_package(run_dir)
            self.assertEqual(unpacked["manifest"]["id"], "generic-t2v-alt")
            for req_file in wpkg.REQUIRED_FILES:
                self.assertTrue((run_dir / "package" / req_file).is_file())

    def test_direct_session_skip_and_regenerate_controls(self):
        import direct_session
        import uuid
        eid = str(uuid.uuid4())
        s1 = str(uuid.uuid4())
        s2 = str(uuid.uuid4())
        ticket1 = str(uuid.uuid4())
        ticket2 = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "production").mkdir()
            jobs = [
                [eid, {"id": s1, "revision": "r1", "seconds": 2, "prompt": "One"}],
                [eid, {"id": s2, "revision": "r2", "seconds": 2, "prompt": "Two"}],
            ]
            session = None
            prompt_calls = []

            def api(url, path, obj=None):
                if path == "/object_info":
                    return {"Output": {}}
                if path == "/prompt":
                    prompt_calls.append(obj["prompt"]["id"])
                    if len(prompt_calls) == 1:
                        session.control({"action": "skip", "episode": eid})
                        return {"prompt_id": ticket1}
                    return {"prompt_id": ticket2}
                if path == f"/api/jobs/{ticket1}/cancel":
                    return {"cancelled": True}
                if path == f"/history/{ticket2}":
                    return {ticket2: {"status": {"status_str": "success", "completed": True}, "outputs": {"141": {}}}}
                raise AssertionError(f"Unexpected API path: {path}")

            from test_production import movie
            ctx = {
                "DATA": root, "LOCK": threading.RLock(), "json_api": api,
                "prepare_shot": lambda _u, _f, _e, s, _w, _r: {"id": s["id"], "141": {"inputs": {}}},
                "download_output": lambda _u, f, _h, _j: (f / "video.part").write_bytes(movie(2, False)),
                "prompt": lambda s: s["prompt"],
                "take_quality": SimpleNamespace(check=lambda _p, _j: {"technical": "passed"}),
                "running": True,
            }
            wf = {"version": "a" * 64, "name": "Fix", "output": "141", "graph": {"141": {"class_type": "Output", "inputs": {}}}}
            view = {"end": 9999999999.0, "episode": eid, "submitted": 2, "phase": "Preparing ComfyUI", "completed": 0, "failed": 0}
            session = direct_session.DirectSession(ctx)
            session.start(view, jobs, wf, None, "http://fixture")
            with patch.object(direct_session, "runtime_requirements", return_value={"graph": wf["graph"]}), \
                 patch.object(wprof, "timing", return_value={"playback_seconds": 2}), \
                 patch.object(production.time, "sleep"):
                session.run("http://fixture", wf)
                self.assertEqual(prompt_calls, [s1, s2])
                self.assertEqual(view["skipped"], 1)
                self.assertEqual(view["completed"], 1)
                self.assertEqual(view["phase"], "Finished")

                # Now test force_new=True (regenerate) creates a second take for already-completed s2
                takes_before = list((root / eid / s2).glob("*/job.json"))
                self.assertEqual(len(takes_before), 1)
                res = production.render(ctx, "http://fixture", 9999999999.0, eid, jobs[1][1], wf, 0, lambda: None, retry_failed=True, force_new=True)
                self.assertEqual(res, "done")
                takes_after = list((root / eid / s2).glob("*/job.json"))
                self.assertEqual(len(takes_after), 2)

    def test_unified_workflow_ui_structure_and_scroll_preservation(self):
        wm_js = (ROOT / "workflow-manager-ui.js").read_text("utf-8")
        wf_js = (ROOT / "workflow-ui.js").read_text("utf-8")
        wk_js = (ROOT / "worker-ui.js").read_text("utf-8")
        self.assertNotIn("data-wm-tab", wm_js)
        self.assertNotIn("dependencyView", wm_js)
        self.assertIn("updateTerminalLogPreservingScroll", wm_js)
        self.assertNotIn('data-wm="start"', wm_js)
        self.assertNotIn('data-wm="stop"', wm_js)
        self.assertNotIn('wmProdPanel', wm_js)
        self.assertIn("wmVideoZoom", wk_js)
        self.assertIn('data-worker="start"', wk_js)
        self.assertIn('data-worker="stop"', wk_js)
        self.assertIn('data-worker="pause"', wk_js)
        self.assertIn('data-worker="resume"', wk_js)
        self.assertIn('data-worker="skip"', wk_js)
        self.assertIn('data-worker="regenerate"', wk_js)
        self.assertIn("wmDropZone", wf_js)
        self.assertIn("queueAutoDeploy", wf_js)

    def test_bootstrap_zip_binary_import_integrity_and_execution(self):
        # Build a realistic bootstrap installer ZIP with binary non-UTF-8 content (like PNG + compressed bytes)
        workflow_api = {
            "119": {"class_type": "VAELoader", "inputs": {"vae_name": "video_vae.safetensors"}},
            "120": {"class_type": "VAELoader", "inputs": {"vae_name": "audio_vae.safetensors"}},
            "128": {"class_type": "CLIPLoader", "inputs": {"clip_name": "clip.safetensors", "type": "minimax"}},
            "131": {
                "class_type": "ComfyMathExpression",
                "inputs": {
                    "expression": "max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17",
                    "values.a": ["132", 0],
                },
            },
            "132": {"class_type": "PrimitiveFloat", "inputs": {"value": 5}},
            "142": {
                "class_type": "DenoMiniMaxH3ReferenceImageLoader",
                "inputs": {"image_paths": "", "disabled_image_paths": "", "audio_sources": "[]"},
            },
            "141": {
                "class_type": "DenoMiniMaxH3ReferenceToVideo",
                "inputs": {
                    "prompt": "",
                    "length": ["131", 1],
                    "clip": ["128", 0],
                    "vae": ["119", 0],
                    "audio_vae": ["120", 0],
                    "ref_images": ["142", 0],
                },
            },
            "144": {
                "class_type": "VHS_VideoCombine",
                "inputs": {"frame_rate": 24, "format": "video/h264-mp4", "filename_prefix": "out", "images": ["141", 0]},
            },
        }
        manifest = {
            "schema_version": 1,
            "package_id": "minimax-speed-x6-v10",
            "version": "0.1.0-rc1",
            "entrypoint": "install.sh",
            "workflow_api": "workflow_api.json",
            "model_count": 2,
            "inputs": {
                "prompt": {"node": "141", "field": "prompt"},
                "image_paths": {"node": "142", "field": "image_paths"},
                "duration": {"node": "132", "field": "value"},
            },
            "output_node": "144",
            "install_event_prefix": "AXDAR_EVENT ",
        }
        nodes_lock = [{"name": "comfyui-deno-custom-nodes", "url": "https://github.com/Deno2026/comfyui-deno-custom-nodes.git", "ref": "v0.7.91"}]
        models_lock = [
            {"repo": "Comfy-Org/MiniMax-H3", "revision": "4b53a35", "remote": "vae/video_vae.safetensors", "local": "vae/video_vae.safetensors"},
            {"repo": "Comfy-Org/MiniMax-H3", "revision": "4b53a35", "remote": "vae/audio_vae.safetensors", "local": "vae/audio_vae.safetensors"},
        ]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
            zf.writestr("workflow_api.json", json.dumps(workflow_api))
            zf.writestr("nodes.lock.json", json.dumps(nodes_lock))
            zf.writestr("models.lock.json", json.dumps(models_lock))
            zf.writestr("install.sh", "#!/usr/bin/env bash\necho ok\n")
            zf.writestr("settings.json", json.dumps({"hf_token": "hf_PASTE_YOUR_READ_TOKEN_HERE"}))
            # Include raw binary PNG header bytes that are guaranteed invalid UTF-8
            zf.writestr("test_assets/reference.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xeb\xff\xfe")
        raw_zip = buf.getvalue()
        # Confirm raw ZIP itself cannot be decoded as UTF-8
        with self.assertRaises(UnicodeDecodeError):
            raw_zip.decode("utf-8")

        # 1) Parse + store import + runtime_graph binding must succeed strictly from binary ZIP
        with tempfile.TemporaryDirectory() as tmp:
            store = wprof.WorkflowStore(Path(tmp) / "profiles")
            wf = store.import_package("AXDAR_MiniMax_SpeedX6_v10_RC1.zip", raw_zip)
            self.assertTrue(wf["packaged"])
            self.assertEqual(wf["packageKind"], "bootstrap")
            self.assertEqual(wf["entrypoint"], "install.sh")
            self.assertEqual(wf["mapping"]["positive"], "141.prompt")
            self.assertEqual(wf["mapping"]["images"], ["142.image_paths"])
            self.assertEqual(wf["mapping"]["duration"], "132.value")
            self.assertEqual(wf["mapping"]["output"], "144")
            reloaded = store.get(wf["version"])
            self.assertEqual(reloaded["packageKind"], "bootstrap")
            pub = wprof.public_profile(reloaded)
            self.assertEqual(pub["totalModelBytes"], 0)
            g = wprof.runtime_graph(
                reloaded, {"seconds": 5, "workflowParameters": {}},
                "A tabby cat sitting by a window", ["cat.png"], [], lambda x: f"uploaded/{x}", "AXDAR/test",
            )
            self.assertEqual(g["141"]["inputs"]["prompt"], "A tabby cat sitting by a window")
            self.assertEqual(g["142"]["inputs"]["image_paths"], "uploaded/cat.png")
            self.assertEqual(g["132"]["inputs"]["value"], 5)

        # 2) Corrupted ZIP must fail CRC integrity check cleanly
        corrupted = bytearray(raw_zip)
        corrupted[-5] ^= 0xFF
        with self.assertRaises(ValueError):
            wpkg.parse_package(bytes(corrupted), "corrupted.zip")

        # 3) HF token redaction never leaks tokens or Bearer headers
        secret = "hf_SuperSecretTokenValue1234567890"
        self.assertNotIn(secret, remote.redact(f"Using {secret} and Authorization: Bearer {secret}", secret))
        self.assertIn("[REDACTED]", remote.redact(f"Using {secret}", secret))
        self.assertIn("hf_transfer", remote.redact("pip install hf_transfer"))

        # 4) Remote bootstrap extraction + install.sh execution + AXDAR_EVENT metrics parsing
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ComfyUI"
            root.mkdir()
            (root / "main.py").write_text("", encoding="utf-8")
            (root / "custom_nodes").mkdir(parents=True)
            (root / "models" / "vae").mkdir(parents=True)
            folder = Path(tmp) / "install-run"
            folder.mkdir()
            (folder / "package.zip").write_bytes(raw_zip)
            stages, metrics, reports = {}, {}, []

            def fake_stream(cmd, cwd, env, secret_tokens, event_prefix, on_event):
                self.assertEqual(cmd[0], "bash")
                self.assertTrue(Path(cmd[1]).name == "install.sh")
                self.assertTrue((Path(cwd) / "manifest.json").is_file())
                # Simulate installer creating custom node + models and emitting AXDAR_EVENT lines
                (root / "custom_nodes" / "comfyui-deno-custom-nodes").mkdir(exist_ok=True)
                on_event({"stage": "node_installed", "name": "comfyui-deno-custom-nodes", "commit": "abc"})
                (root / "models" / "vae" / "video_vae.safetensors").write_bytes(b"v")
                on_event({
                    "stage": "models", "file": "vae/video_vae.safetensors", "completed": 1, "total_files": 2,
                    "downloaded_bytes": 500, "total_bytes": 1000, "bytes_per_second": 52428800, "status": "downloading",
                })
                (root / "models" / "vae" / "audio_vae.safetensors").write_bytes(b"a")
                on_event({
                    "stage": "models", "file": "vae/audio_vae.safetensors", "completed": 2, "total_files": 2,
                    "downloaded_bytes": 1000, "total_bytes": 1000, "bytes_per_second": 0, "status": "verified",
                })
                return 0

            info_obj = {n["class_type"]: {"input": {"required": {}}} for n in workflow_api.values()}
            fake_env = {
                "comfyuiRoot": str(root), "comfyuiRevision": "a" * 40, "comfyuiGitClean": True,
                "pythonVersion": "3.12.0", "torchVersion": "2.10.0+cu130", "cudaVersion": "13.0",
                "gpuName": "NVIDIA GeForce RTX 5090", "gpuArch": "12.0", "vramGb": 32.0,
                "diskFreeBytes": 200 * 10**9, "environmentFingerprint": "f" * 64,
            }
            config = {
                "packaged": True, "packageKind": "bootstrap", "mode": "install", "port": 8188,
                "manifest": manifest, "graph": workflow_api, "nodes": nodes_lock, "models": models_lock,
            }
            with patch.object(remote, "root_path", return_value=root), \
                 patch.object(remote, "probe_environment", return_value=dict(fake_env)), \
                 patch.object(remote, "_supervisor_has_comfyui", return_value=False), \
                 patch.object(remote, "_stream_bootstrap_command", side_effect=fake_stream), \
                 patch.object(remote.workflow_comfy_manager.ComfyManagerClient, "version", return_value=None):
                rc = remote._execute_bootstrap(
                    folder, config, lambda _p: info_obj, lambda: None,
                    lambda name, st, msg="", c=None, t=None: stages.__setitem__(name, st),
                    reports.append,
                    metrics_fn=lambda **kw: metrics.update(kw),
                )
            self.assertEqual(rc, 0)
            self.assertTrue((folder / "package" / "install.sh").is_file())
            self.assertEqual(metrics["filesTotal"], 3)
            self.assertEqual(metrics["filesCompleted"], 3)
            self.assertEqual(stages[remote.PHASES[6]], "Completed")
            self.assertTrue(reports[-1]["environmentFingerprint"])

    def test_post_install_comfy_manager_sync_install_update_protect_and_restart(self):
        import workflow_comfy_manager as wcm

        graph = {
            "1": {"class_type": "DenoMiniMaxH3ReferenceToVideo", "inputs": {}},
            "2": {"class_type": "PathchSageAttentionKJ", "inputs": {}},
            "3": {"class_type": "MissingCustomNodeX", "inputs": {}},
            "4": {"class_type": "SolAttnMiniMax", "inputs": {}},
            "5": {"class_type": "BadGpuDepNode", "inputs": {}},
        }
        node_specs = [
            {"name": "comfyui-deno-custom-nodes", "repo": "Deno2026/comfyui-deno-custom-nodes", "revision": "v0.7.91"},
            {"name": "ComfyUI-KJNodes", "repo": "kijai/ComfyUI-KJNodes", "revision": ""},
        ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ComfyUI"
            (root / "custom_nodes" / "sol_attn_minimax_pr117").mkdir(parents=True)
            (root / "models" / "vae").mkdir(parents=True)
            model_file = root / "models" / "vae" / "kept.safetensors"
            model_file.write_bytes(b"MODEL_DATA_PRESERVED")

            events = []
            installed_packs = {
                "comfyui-deno-custom-nodes": {"ver": "0.7.91", "enabled": True},
                "comfyui-kjnodes": {"ver": "1.4.0", "enabled": True},
                "sol_attn_minimax_pr117": {"ver": "unknown", "enabled": True},
                "bad-gpu-pack": {"ver": "1.0.0", "enabled": True},
                "unrelated-pack": {"ver": "1.0.0", "enabled": True},
            }
            packs = {
                "comfyui-deno-custom-nodes": {
                    "id": "comfyui-deno-custom-nodes", "title": "Deno Nodes",
                    "repository": "https://github.com/Deno2026/comfyui-deno-custom-nodes",
                    "state": "installed", "version": "0.8.0", "active_version": "0.7.91", "updatable": True,
                },
                "comfyui-kjnodes": {
                    "id": "comfyui-kjnodes", "title": "ComfyUI-KJNodes",
                    "repository": "https://github.com/kijai/ComfyUI-KJNodes",
                    "state": "installed", "version": "1.5.0", "active_version": "1.4.0", "updatable": True,
                },
                "missing-pack-x": {
                    "id": "missing-pack-x", "title": "Missing Pack X",
                    "repository": "https://github.com/example/missing-pack-x",
                    "state": "not-installed", "version": "2.0.0", "files": ["https://github.com/example/missing-pack-x"],
                },
                "sol_attn_minimax_pr117": {
                    "id": "sol_attn_minimax_pr117", "title": "sol_attn_minimax_pr117",
                    "repository": "", "state": "installed", "version": "1.1.0", "active_version": "1.0.0", "updatable": True,
                },
                "bad-gpu-pack": {
                    "id": "bad-gpu-pack", "title": "Bad GPU Pack",
                    "repository": "https://github.com/example/bad-gpu-pack",
                    "state": "installed", "version": "2.0.0", "active_version": "1.0.0", "updatable": True,
                    "pip": ["torch==2.3.0"],
                },
                "unrelated-pack": {
                    "id": "unrelated-pack", "title": "Unrelated Pack",
                    "repository": "https://github.com/example/unrelated",
                    "state": "installed", "version": "9.0.0", "active_version": "1.0.0", "updatable": True,
                },
            }
            mappings = {
                "comfyui-deno-custom-nodes": [["DenoMiniMaxH3ReferenceToVideo"], {}],
                "comfyui-kjnodes": [["PathchSageAttentionKJ"], {}],
                "missing-pack-x": [["MissingCustomNodeX"], {}],
                "sol_attn_minimax_pr117": [["SolAttnMiniMax"], {}],
                "bad-gpu-pack": [["BadGpuDepNode"], {}],
                "unrelated-pack": [["SomeUnrelatedNode"], {}],
            }

            state_box = {"queue_polls": 0, "pending_op": None, "restarted": False}

            def fake_request(method, path, payload, timeout, expect_json):
                events.append((method, path, payload))
                if path == "/manager/version":
                    return 200, "V3.32"
                if path.startswith("/customnode/getmappings"):
                    return 200, mappings
                if path.startswith("/customnode/getlist"):
                    return 200, {"channel": "default", "node_packs": copy.deepcopy(packs)}
                if path == "/customnode/installed":
                    return 200, dict(installed_packs)
                if path == "/customnode/import_fail_info":
                    return 400, ""
                if path == "/manager/queue/install":
                    self.assertNotEqual(payload.get("id"), "unrelated-pack")
                    state_box["pending_op"] = ("install", payload["id"])
                    return 200, ""
                if path == "/manager/queue/update":
                    self.assertEqual(payload["id"], "comfyui-kjnodes")
                    state_box["pending_op"] = ("update", payload["id"])
                    return 200, ""
                if path == "/manager/queue/start":
                    state_box["queue_polls"] = 1
                    return 200, ""
                if path == "/manager/queue/status":
                    if state_box["queue_polls"] > 0:
                        state_box["queue_polls"] -= 1
                        return 200, {"total_count": 1, "done_count": 0, "in_progress_count": 1, "is_processing": True}
                    if state_box["pending_op"]:
                        op, pid = state_box["pending_op"]
                        state_box["pending_op"] = None
                        if op == "install":
                            packs[pid]["state"] = "installed"
                            installed_packs[pid] = {"ver": packs[pid]["version"], "enabled": True}
                        elif op == "update":
                            packs[pid]["active_version"] = packs[pid]["version"]
                            packs[pid]["updatable"] = False
                            installed_packs[pid]["ver"] = packs[pid]["version"]
                    return 200, {"total_count": 1, "done_count": 1, "in_progress_count": 0, "is_processing": False}
                raise AssertionError(f"Unexpected ComfyUI-Manager endpoint: {method} {path}")

            def fake_api(path):
                if path == "/object_info":
                    base = {
                        "DenoMiniMaxH3ReferenceToVideo": {},
                        "PathchSageAttentionKJ": {},
                        "SolAttnMiniMax": {},
                        "BadGpuDepNode": {},
                    }
                    if state_box["restarted"] and packs["missing-pack-x"]["state"] == "installed":
                        base["MissingCustomNodeX"] = {}
                    return base
                raise AssertionError(f"Unexpected ComfyUI API path: {path}")

            def fake_restart():
                # Restart MUST NEVER run while queue is still processing
                self.assertEqual(state_box["queue_polls"], 0)
                self.assertIsNone(state_box["pending_op"])
                events.append(("RESTART", "comfyui", None))
                state_box["restarted"] = True

            client = wcm.ComfyManagerClient("http://127.0.0.1:8188", request_fn=fake_request)
            stages = {}
            sync = wcm.run_post_install_sync(
                root=root, graph=graph, api=fake_api, port=8188,
                manifest={}, node_specs=node_specs,
                stage=lambda name, st, msg="", c=None, t=None: stages.__setitem__(name, (st, msg)),
                restart_fn=fake_restart, client=client, poll_interval=0.0,
            )

            self.assertEqual(sync["finalStatus"], "passed")
            self.assertEqual(sync["installedNodepacks"], ["Missing Pack X"])
            self.assertEqual(sync["updatedNodepacks"], ["ComfyUI-KJNodes"])
            skipped_names = {s["name"] for s in sync["skippedProtected"]}
            self.assertIn("sol_attn_minimax_pr117", skipped_names)
            self.assertIn("Deno Nodes", skipped_names)
            self.assertIn("Bad GPU Pack", skipped_names)
            self.assertTrue(sync["restarted"])
            self.assertEqual(sync["remainingMissingClasses"], [])
            # Verify forbidden endpoints were NEVER called
            called_paths = [p for _, p, _ in events]
            self.assertNotIn("/manager/queue/update_all", called_paths)
            self.assertNotIn("/manager/queue/uninstall", called_paths)
            self.assertNotIn("/manager/queue/disable", called_paths)
            self.assertNotIn("/manager/queue/reinstall", called_paths)
            # Restart occurred strictly AFTER install and update finished
            restart_idx = called_paths.index("comfyui")
            self.assertGreater(restart_idx, called_paths.index("/manager/queue/install"))
            self.assertGreater(restart_idx, called_paths.index("/manager/queue/update"))
            # Downloaded model preserved untouched
            self.assertEqual(model_file.read_bytes(), b"MODEL_DATA_PRESERVED")

            # Idempotency check: repeated execution performs ZERO installs, ZERO updates, ZERO restarts
            events.clear()
            sync2 = wcm.run_post_install_sync(
                root=root, graph=graph, api=fake_api, port=8188,
                manifest={}, node_specs=node_specs,
                restart_fn=lambda: (_ for _ in ()).throw(AssertionError("Redundant restart on idempotent run")),
                client=client, poll_interval=0.0,
            )
            self.assertEqual(sync2["finalStatus"], "passed")
            self.assertEqual(sync2["installedNodepacks"], [])
            self.assertEqual(sync2["updatedNodepacks"], [])
            self.assertFalse(sync2["restarted"])

    def test_post_install_manager_failure_preserves_models_and_supports_independent_retry(self):
        import workflow_comfy_manager as wcm

        graph = {"1": {"class_type": "BrokenMissingNode", "inputs": {}}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ComfyUI"
            root.mkdir()
            (root / "main.py").write_text("", encoding="utf-8")
            (root / "models" / "vae").mkdir(parents=True)
            model_file = root / "models" / "vae" / "existing.safetensors"
            model_file.write_bytes(b"KEEP_ME")

            def failing_request(method, path, payload, timeout, expect_json):
                if path == "/manager/version":
                    return 200, "V3.32"
                if path.startswith("/customnode/getmappings"):
                    return 200, {"broken-pack": [["BrokenMissingNode"], {}]}
                if path.startswith("/customnode/getlist"):
                    return 200, {"channel": "default", "node_packs": {
                        "broken-pack": {"id": "broken-pack", "title": "Broken Pack", "state": "not-installed", "version": "1.0.0"}
                    }}
                if path == "/customnode/installed":
                    return 200, {}
                if path == "/manager/queue/status":
                    return 200, {"total_count": 0, "done_count": 0, "in_progress_count": 0, "is_processing": False}
                if path == "/manager/queue/install":
                    return 500, "git clone failed: network error"
                if path == "/customnode/import_fail_info":
                    return 400, ""
                raise AssertionError(f"Unexpected endpoint: {path}")

            client = wcm.ComfyManagerClient("http://127.0.0.1:8188", request_fn=failing_request)
            sync = wcm.run_post_install_sync(
                root=root, graph=graph, api=lambda _p: {}, port=8188,
                restart_fn=lambda: (_ for _ in ()).throw(AssertionError("Must not restart on failed install")),
                client=client, poll_interval=0.0,
            )
            self.assertEqual(sync["finalStatus"], "failed")
            self.assertFalse(sync["restarted"])
            self.assertEqual(len(sync["errors"]), 1)
            self.assertEqual(sync["errors"][0]["nodepack"], "Broken Pack")
            self.assertIn("git clone failed", sync["errors"][0]["error"])
            # Models preserved, no package reinstall triggered
            self.assertEqual(model_file.read_bytes(), b"KEEP_ME")

        # Verify WorkflowManager ships workflow_comfy_manager.py and exposes independent sync-nodes mode
        self.assertIn("workflow_comfy_manager.py", workflow_manager.FILES)
        wm_js = (ROOT / "workflow-manager-ui.js").read_text("utf-8")
        self.assertIn("managerSyncHtml", wm_js)
        self.assertIn('data-wm="sync-nodes"', wm_js)
        self.assertIn("ComfyUI-Manager sync", wm_js)

        # Verify post-install sync does NOT run when package install.sh itself fails
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ComfyUI"
            root.mkdir()
            (root / "main.py").write_text("", encoding="utf-8")
            (root / "custom_nodes").mkdir()
            (root / "models").mkdir()
            folder = Path(tmp) / "failed-run"
            folder.mkdir()
            buf = io.BytesIO()
            wf_api = {"1": {"class_type": "VHS_VideoCombine", "inputs": {"format": "video/h264-mp4", "frame_rate": 24, "prompt": ""}}}
            manifest = {
                "schema_version": 1, "package_id": "fail-pkg", "version": "1.0.0",
                "entrypoint": "install.sh", "workflow_api": "workflow_api.json",
                "inputs": {"prompt": {"node": "1", "field": "prompt"}},
                "fixedSeconds": 5, "output_node": "1",
            }
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("manifest.json", json.dumps(manifest))
                zf.writestr("workflow_api.json", json.dumps(wf_api))
                zf.writestr("install.sh", "#!/usr/bin/env bash\nexit 1\n")
            (folder / "package.zip").write_bytes(buf.getvalue())
            fake_env = {
                "comfyuiRoot": str(root), "comfyuiRevision": "a" * 40, "comfyuiGitClean": True,
                "pythonVersion": "3.12.0", "torchVersion": "2.10.0+cu130", "cudaVersion": "13.0",
                "gpuName": "NVIDIA", "gpuArch": "12.0", "vramGb": 32.0,
                "diskFreeBytes": 200 * 10**9, "environmentFingerprint": "f" * 64,
            }
            config = {"packaged": True, "packageKind": "bootstrap", "mode": "install", "port": 8188,
                      "manifest": manifest, "graph": wf_api, "nodes": [], "models": []}
            with patch.object(remote, "root_path", return_value=root), \
                 patch.object(remote, "probe_environment", return_value=dict(fake_env)), \
                 patch.object(remote, "_supervisor_has_comfyui", return_value=False), \
                 patch.object(remote, "_stream_bootstrap_command", return_value=1), \
                 patch.object(remote, "_run_post_install_manager_sync") as sync_mock:
                rc = remote._execute_bootstrap(
                    folder, config, lambda _p: {}, lambda: None,
                    lambda *_a, **_k: None, lambda *_a, **_k: None,
                )
            self.assertEqual(rc, 1)
            sync_mock.assert_not_called()


@unittest.skipUnless(
    (LOCAL_COMFY_PORTABLE / "ComfyUI" / "main.py").is_file() and (LOCAL_COMFY_PORTABLE / "python_embeded" / "python.exe").is_file(),
    "Local ComfyUI_windows_portable installation required for real host preflight validation.",
)
class LocalComfyUIPreflightTests(unittest.TestCase):
    def test_real_local_comfyui_environment_probe_and_package_compatibility(self):
        comfy_root = LOCAL_COMFY_PORTABLE / "ComfyUI"
        comfy_py = str(LOCAL_COMFY_PORTABLE / "python_embeded" / "python.exe")
        with patch.dict(os.environ, {"COMFY_PYTHON": comfy_py}):
            env = remote.probe_environment(comfy_root)
        self.assertRegex(env["comfyuiRevision"] or "", r"^[0-9a-f]{40}$")
        self.assertTrue(env["pythonVersion"])
        self.assertTrue(env["torchVersion"])
        self.assertGreater(env["diskFreeBytes"], 0)

        # 1) MiniMax_H3.zip requires >=24 GB VRAM; on local RTX 3060 (12 GB) preflight must detect VRAM incompatibility
        minimax = wpkg.parse_package((ROOT / "packages" / "MiniMax_H3.zip").read_bytes(), "MiniMax_H3.zip")
        mm_compat = remote.check_package_compatibility(env, minimax["manifest"], comfy_root, minimax["nodes"], minimax["models"])
        if env["vramGb"] < 24:
            self.assertFalse(mm_compat["compatible"])
            self.assertTrue(any("VRAM" in msg for msg in mm_compat["incompatibilities"]))

        # 2) Lightweight generic package matching local ComfyUI rev & <=12 GB VRAM must pass compatibility preflight
        manifest, _wf, nodes, models, _mapping = make_generic_t2v_package(
            comfy_rev=env["comfyuiRevision"],
            min_vram=min(8, env["vramGb"]),
        )
        alt_compat = remote.check_package_compatibility(env, manifest, comfy_root, nodes, models)
        self.assertTrue(alt_compat["compatible"], alt_compat["incompatibilities"])


if __name__ == "__main__":
    unittest.main()
