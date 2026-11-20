from pathlib import Path
import json
import os
import shutil
import tempfile
import unittest

# Imports from repository
from plugin_registry import Registry
from plugin_host import handle

class TestDemoEcho(unittest.TestCase):
    def setUp(self):
        # Create temp directory and copy only the demo.echo fixture
        self.tmp_dir = tempfile.TemporaryDirectory()
        src = os.path.join(Path(__file__).parent, "plugins", "catalog", "demo.echo")
        dst = os.path.join(self.tmp_dir.name, "plugins", "catalog", "demo.echo")
        shutil.copytree(src, dst)
        self.registry = Registry(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def call(self, op, **kw):
        return handle(dict(op=op, id="demo.echo", **kw), self.registry)

    def test_install_and_digest(self):
        # Start with empty installed list
        res = self.call("install", version="1.0.0")
        self.assertTrue(res.get("ok"))
        installed = res.get("data", {}).get("installed", [])
        self.assertEqual(len(installed), 1)
        real_digest = installed[0]["digest"]
        self.assertTrue(isinstance(real_digest, str) and len(real_digest) > 0)
        self.real_digest_v1 = real_digest

    def test_enable_and_invoke_v1(self):
        # Install v1
        res = self.call("install", version="1.0.0")
        real_digest = res.get("data", {}).get("installed", [])[0]["digest"]
        # Reject enable without approval
        res = self.call("enable", version="1.0.0", approved=False, digest=real_digest)
        self.assertFalse(res.get("ok"))
        # Reject enable with wrong digest
        res = self.call("enable", version="1.0.0", approved=True, digest="bad-digest")
        self.assertFalse(res.get("ok"))
        # Enable properly
        res = self.call("enable", version="1.0.0", approved=True, digest=real_digest)
        self.assertTrue(res.get("ok"))
        # Invoke action
        res = self.call("invoke", action="echo", text="abc")
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("data"), {"text": "v1: abc"})

    def test_disable_and_invoke_rejection(self):
        # Install and enable v1
        res = self.call("install", version="1.0.0")
        digest = res.get("data", {}).get("installed", [])[0]["digest"]
        self.call("enable", version="1.0.0", approved=True, digest=digest)
        # Disable
        res = self.call("disable")
        self.assertTrue(res.get("ok"))
        # Invoke should be rejected
        res = self.call("invoke", action="echo", text="abc")
        self.assertFalse(res.get("ok"))

    def test_enable_v2_and_rollback(self):
        # Install v1 and enable
        res = self.call("install", version="1.0.0")
        digest_v1 = res.get("data", {}).get("installed", [])[0]["digest"]
        self.call("enable", version="1.0.0", approved=True, digest=digest_v1)
        # Install v2
        res = self.call("install", version="2.0.0")
        digest_v2 = next(p["digest"] for p in res["data"]["installed"] if p["version"] == "2.0.0")
        # Enable v2
        res = self.call("enable", version="2.0.0", approved=True, digest=digest_v2)
        self.assertTrue(res.get("ok"))
        # Invoke v2
        res = self.call("invoke", action="echo", text="abc")
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("data"), {"text": "v2: abc"})
        # Rollback to v1
        res = self.call("rollback")
        self.assertTrue(res.get("ok"))
        # Invoke again should use v1
        res = self.call("invoke", action="echo", text="abc")
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("data"), {"text": "v1: abc"})

    def test_remove_and_project_sentinel(self):
        # Install and enable
        res = self.call("install", version="1.0.0")
        digest = res.get("data", {}).get("installed", [])[0]["digest"]
        self.call("enable", version="1.0.0", approved=True, digest=digest)
        # Disable
        self.call("disable")
        # Remove
        sentinel = self.registry.base / "episodes" / "existing.mp4"
        sentinel.parent.mkdir()
        sentinel.write_bytes(b"existing media")
        res = self.call("remove", version="1.0.0")
        self.assertTrue(res.get("ok"))
        self.assertEqual(sentinel.read_bytes(), b"existing media")
        self.assertEqual(self.call("list")["data"]["installed"], [])

    def test_unknown_action(self):
        entry = self.call("install", version="1.0.0")["data"]["installed"][0]
        self.assertTrue(self.call("enable", version=entry["version"], approved=True, digest=entry["digest"])["ok"])
        self.assertFalse(self.call("invoke", action="missing", text="abc")["ok"])

    def test_catalog_tampering(self):
        entry = self.call("install", version="1.0.0")["data"]["installed"][0]
        script = self.registry.catalog / "demo.echo" / "1.0.0" / "action.py"
        script.write_bytes(b"raise RuntimeError('unexpected replacement')")
        self.assertFalse(self.call("install", version="1.0.0")["ok"])
        self.assertTrue(self.call("enable", version=entry["version"], approved=True, digest=entry["digest"])["ok"])
        self.assertEqual(self.call("invoke", action="echo", text="original")["data"]["text"], "v1: original")

    def test_unsupported_profile(self):
        path = self.registry.catalog / "demo.echo" / "1.0.0" / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["actions"][0]["execution"] = "gpu"
        path.write_text(json.dumps(manifest))
        self.assertFalse(self.call("install", version="1.0.0")["ok"])

    def test_rollback_never_enabled(self):
        self.call("install", version="1.0.0")
        installed = self.call("install", version="2.0.0")["data"]["installed"]
        digest = next(entry["digest"] for entry in installed if entry["version"] == "2.0.0")
        self.assertTrue(self.call("enable", version="2.0.0", digest=digest, approved=True)["ok"])
        res = self.call("rollback")
        self.assertFalse(res.get("ok"))
        self.assertEqual(self.call("invoke", action="echo", text="abc")["data"]["text"], "v2: abc")

if __name__ == "__main__":
    unittest.main()
