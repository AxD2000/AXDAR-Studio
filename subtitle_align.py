"""Run stable-ts in the subtitle Python environment."""
import json
import math
import os
import re
import sys
from pathlib import Path


def align(entries):
    import stable_whisper
    import torch

    torch.set_num_threads(min(4, os.cpu_count() or 1))

    models = {}
    results = []
    for entry in entries:
        language, lines = entry["language"], entry["lines"]
        try:
            if language not in models:
                model = os.environ.get("AXDAR_SUBTITLE_MODEL") or ("tiny.en" if language == "en" else "tiny")
                models_dir = Path(__file__).resolve().parent / "tools" / "freecut" / "models"
                models[language] = stable_whisper.load_model(model, device="cpu", download_root=str(models_dir))
            aligned = models[language].align(
                entry["wav"], "\n".join(lines), language=language, original_split=True,
                regroup=False, failure_threshold=0.15, verbose=None,
            )
            segments = aligned.segments if aligned else []
            if len(segments) != len(lines):
                raise ValueError("stable-ts could not place each original dialogue line.")
            cues = []
            prior_end = 0
            for original, segment in zip(lines, segments):
                normalize = lambda value: re.sub(r"\s+", " ", value).strip()
                if normalize(original) != normalize(segment.text):
                    raise ValueError("stable-ts returned different dialogue text.")
                start, end = segment.start, segment.end
                valid_words = [word for word in segment.words or []
                               if word.end > word.start and (word.probability or 0) >= 0.12]
                if (not math.isfinite(start) or not math.isfinite(end)
                        or not 0 <= start < end <= entry["duration"] + 0.12
                        or start < prior_end - 0.04 or not segment.words
                        or len(valid_words) / len(segment.words) < 0.75):
                    raise ValueError("Check this clip's dialogue timing against its audio.")
                cues.append({"startSeconds": start, "endSeconds": min(end, entry["duration"]), "text": original})
                prior_end = end
            results.append({"media": entry["media"], "cues": cues})
        except Exception as exc:
            results.append({"media": entry["media"], "error": str(exc)[:250]})
    return results


if __name__ == "__main__":
    manifest, output = map(Path, sys.argv[1:])
    output.write_text(json.dumps(align(json.loads(manifest.read_text("utf-8"))), ensure_ascii=False), "utf-8")
