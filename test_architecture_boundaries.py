"""Enforces hard module isolation boundaries and Vast Linux remote bundle integrity."""
import ast
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import workflow_manager

ROOT = Path(__file__).resolve().parent

DEPARTMENTS = {
    "vast": {"vast_credentials", "vast_ssh_setup", "vast_connection", "vast_ssh_ops"},
    "workflow": {"workflow_profiles", "workflow_manager", "provision", "workflow_install_remote"},
    "story": {"story_engine", "studio_actions"},
    "editor": {"editor_media", "episode_editor", "freecut_bridge", "subtitle_align"},
    "plugins": {"plugin_manifest", "plugin_registry", "plugin_host"},
}

FORBIDDEN_IMPORTS = {
    "vast": DEPARTMENTS["workflow"] | DEPARTMENTS["story"] | DEPARTMENTS["editor"] | DEPARTMENTS["plugins"]
            | {"production", "vast_deploy", "vast_worker", "direct_session", "drive_archive", "production_services", "take_quality", "comfy_client", "studio", "app_context", "studio_routes"},
    "workflow": DEPARTMENTS["vast"] | DEPARTMENTS["story"] | DEPARTMENTS["editor"] | DEPARTMENTS["plugins"]
                | {"vast_deploy", "vast_worker", "direct_session", "drive_archive", "production_services", "take_quality", "studio", "app_context", "studio_routes"},
    "story": DEPARTMENTS["vast"] | DEPARTMENTS["editor"] | DEPARTMENTS["plugins"]
             | {"workflow_manager", "workflow_install_remote", "production", "vast_deploy", "vast_worker", "direct_session", "drive_archive", "studio", "app_context", "studio_routes"},
    "editor": DEPARTMENTS["vast"] | DEPARTMENTS["workflow"] | DEPARTMENTS["plugins"]
              | {"vast_deploy", "vast_worker", "direct_session", "drive_archive", "studio", "app_context", "studio_routes"},
    "plugins": DEPARTMENTS["vast"] | DEPARTMENTS["workflow"] | DEPARTMENTS["story"] | DEPARTMENTS["editor"]
               | {"production", "vast_deploy", "vast_worker", "direct_session", "drive_archive", "comfy_client", "studio", "app_context", "studio_routes"},
}


def module_imports(path):
    tree = ast.parse(path.read_text("utf-8"), filename=path.name)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_department_import_isolation(self):
        violations = []
        for dept, modules in DEPARTMENTS.items():
            forbidden = FORBIDDEN_IMPORTS[dept]
            for mod in sorted(modules):
                imports = module_imports(ROOT / f"{mod}.py")
                bad = sorted(imports & forbidden)
                if bad:
                    violations.append(f"{dept}/{mod}.py imports forbidden modules: {bad}")
        self.assertEqual(violations, [], "\n".join(violations))

    def test_vast_has_no_circular_import_between_connection_and_ssh_setup(self):
        self.assertNotIn("vast_connection", module_imports(ROOT / "vast_ssh_setup.py"))

    def test_no_globals_injection_or_studio_runpy_in_production_code(self):
        studio_src = (ROOT / "studio.pyw").read_text("utf-8")
        self.assertNotIn("globals()", studio_src)
        worker_src = (ROOT / "vast_worker.py").read_text("utf-8")
        self.assertNotIn("runpy.run_path", worker_src)
        self.assertNotIn("studio.pyw", worker_src)

    def test_isolated_four_file_vast_installer_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in workflow_manager.FILES:
                shutil.copy2(ROOT / name, Path(tmp) / name)
            code = (
                f"import sys; sys.path.insert(0, {tmp!r}); "
                "import workflow_install_remote, provision; "
                "assert len(provision.model_sources()) > 0; "
                "print('BUNDLE_OK')"
            )
            result = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("BUNDLE_OK", result.stdout)

    def test_isolated_remote_worker_deploy_bundle_without_studio(self):
        import vast_deploy
        with tempfile.TemporaryDirectory() as tmp:
            for name in vast_deploy.SOURCES:
                shutil.copy2(ROOT / name, Path(tmp) / name)
            self.assertFalse((Path(tmp) / "studio.pyw").exists())
            code = (
                f"import sys; sys.path.insert(0, {tmp!r}); "
                "import vast_worker, worker, comfy_client, production, workflow_profiles; "
                "print('WORKER_BUNDLE_OK')"
            )
            result = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("WORKER_BUNDLE_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
