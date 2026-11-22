import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from freecut_bridge import FreecutBridge, dialogue
from production import atomic


class FreecutBridgeTests(unittest.TestCase):
    def test_frozen_dialogue_is_kept_verbatim_apart_from_markup(self):
        shot = {"prompt": "TOM: <d>[English]The new draft.</d>"}
        take = {"settings": {"prompt": "TOM: <d>[English]It's the original—exactly.</d>"}}
        self.assertEqual(dialogue(shot, take, []), ("en", ["It's the original—exactly."]))

    def test_saved_move_and_trim_shift_and_clip_source_subtitles(self):
        with tempfile.TemporaryDirectory() as temp:
            bridge = FreecutBridge(SimpleNamespace(data=Path(temp)))
            episode = str(uuid.uuid4())
            folder = bridge.workspace / "projects" / episode
            folder.mkdir(parents=True)
            clip = dict(type="video", durationInFrames=48, sourceFps=24, sourceStart=24, speed=1,
                        transcriptCaptions={"enabled": True, "cues": [
                            {"startSeconds": 0.5, "endSeconds": 1.5, "text": "Original dialogue."},
                            {"startSeconds": 4, "endSeconds": 5, "text": "Outside the trim."}]})
            clip["from"] = 72
            atomic(folder / "project.json", {"metadata": {"fps": 24}, "timeline": {"items": [clip]}})
            self.assertEqual(bridge.subtitles(episode), "1\n00:00:03,000 --> 00:00:03,500\nOriginal dialogue.\n")
