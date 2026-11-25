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
    "workflow": {"workflow_profiles", "workflow_manager", "provision", "workflow_install_remote", "workflow_packages"},
    "story": {"story_engine", "studio_actions"},
    "plugins": {"plugin_manifest", "plugin_registry", "plugin_host"},
}

FORBIDDEN_IMPORTS = {
    "vast": DEPARTMENTS["workflow"] | DEPARTMENTS["story"] | DEPARTMENTS["plugins"]
            | {"production", "direct_session", "take_quality", "editor_media", "comfy_client", "studio", "app_context", "studio_routes"},
    "workflow": DEPARTMENTS["vast"] | DEPARTMENTS["story"] | DEPARTMENTS["plugins"]
                | {"direct_session", "take_quality", "editor_media", "studio", "app_context", "studio_routes"},
    "story": DEPARTMENTS["vast"] | DEPARTMENTS["plugins"]
             | {"workflow_manager", "workflow_install_remote", "production", "direct_session", "editor_media", "studio", "app_context", "studio_routes"},
    "plugins": DEPARTMENTS["vast"] | DEPARTMENTS["workflow"] | DEPARTMENTS["story"]
               | {"production", "direct_session", "comfy_client", "editor_media", "studio", "app_context", "studio_routes"},
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

    def test_no_globals_injection_in_studio(self):
        studio_src = (ROOT / "studio.pyw").read_text("utf-8")
        self.assertNotIn("globals()", studio_src)

    def test_isolated_four_file_vast_installer_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in workflow_manager.FILES:
                shutil.copy2(ROOT / name, Path(tmp) / name)
            self.assertFalse((Path(tmp) / "studio.pyw").exists())
            self.assertFalse((Path(tmp) / "model-lock.json").exists())
            code = (
                f"import sys; sys.path.insert(0, {tmp!r}); "
                "import workflow_install_remote, provision, workflow_packages, production; "
                "assert workflow_packages.PACKAGE_SCHEMA_VERSION == 1; "
                "print('BUNDLE_OK')"
            )
            result = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("BUNDLE_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
