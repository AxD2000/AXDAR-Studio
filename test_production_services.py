import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import take_quality


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg binaries required for real decoding.")
class DecodeTests(unittest.TestCase):
    def test_decodes_real_video_and_checks_required_audio_resolution_and_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "video.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x90:r=24:d=2",
                            "-c:v", "libx264", "-y", str(file)], capture_output=True, check=True)
            job = {"settings": {"seconds": 2}, "resolution": [160, 90]}
            clean = take_quality.check(file, job)
            self.assertEqual((clean["technical"], clean["frames"], clean["width"], clean["height"]), ("passed", 48, 160, 90))
            self.assertEqual(take_quality.check(file, dict(job, audioRequired=True))["technical"], "failed")
            self.assertEqual(take_quality.check(file, dict(job, resolution=[320, 180]))["technical"], "failed")
            file.write_bytes(file.read_bytes()[:300])
            self.assertEqual(take_quality.check(file, job)["technical"], "failed")


if __name__ == "__main__":
    unittest.main()
