"""Story persistence and production preparation for the local AXDAR project."""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import tempfile
import unicodedata
import uuid
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

SCHEMA_VERSION = 2
PHOTOS = {".jpg", ".jpeg", ".png", ".webp", ".avif"}
VOICES = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}
SHOT_TEXT_FIELDS = ("action", "camera", "movement", "lighting", "environment", "motion", "sound", "prompt",
                    "negative_prompt", "change", "workflow_version")
MAX_TEXT = 2_000_000


def uid():
    return str(uuid.uuid4())


def new_scene(title="Scene 1"):
    return {"id": uid(), "title": title}


def new_episode(title="New episode"):
    return {"id": uid(), "title": title, "story": "", "scenes": [], "shots": [], "story_imports": []}


def new_project(rules=()):
    return {"schema_version": SCHEMA_VERSION, "id": uid(), "episodes": [], "characters": [],
            "rules": deepcopy(list(rules)), "removedAssets": []}


def _version(db):
    version = db.get("schema_version", 1)
    if not isinstance(version, int) or version > SCHEMA_VERSION:
        raise ValueError("This project needs a newer version of AXDAR Studio.")
    return version


def _records(data_dir):
    root = Path(data_dir)
    return sorted(p.relative_to(root).parts[:3] for p in root.glob("*/*/*/job.json")
                  if p.parent.name.isdecimal())


def migration_preview(db, data_dir):
    required = _version(db) < SCHEMA_VERSION
    episodes = db.get("episodes", [])
    jobs = _records(data_dir)
    counts = {"episodes": len(episodes), "shots": sum(len(e["shots"]) for e in episodes),
              "scenes": sum(len(e.get("scenes", [])) or bool(e["shots"]) for e in episodes),
              "takes": len(jobs)}
    encoded = json.dumps([db, jobs], ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return {"required": required, "counts": counts, "token": sha256(encoded.encode()).hexdigest()}


def _take_id(project_id, episode, shot, version):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "/".join(("axdar-take", project_id, episode, shot, version))))


def sync_takes(db, data_dir):
    if _version(db) < SCHEMA_VERSION:
        return False
    shots = {(e["id"], s["id"]): s for e in db["episodes"] for s in e["shots"]}
    changed = False
    orphaned = db.get("archived_takes", [])
    for eid, sid, version in _records(data_dir):
        shot = shots.get((eid, sid))
        entries = shot.setdefault("takes", []) if shot is not None else orphaned
        take_id = _take_id(db["id"], eid, sid, version)
        if any(entry["id"] == take_id for entry in entries):
            continue
        entries.append({"id": take_id, "episode_id": eid, "shot_id": sid, "version": version,
                        "job": "/".join((eid, sid, version, "job.json"))})
        changed = True
    if orphaned:
        db["archived_takes"] = orphaned
    return changed


def _atomic_save(path, db):
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".story-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(db, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def migrate(path, data_dir, token):
    """Called under the application's project lock after an explicit confirmation."""
    path = Path(path)
    original = path.read_bytes()
    db = json.loads(original.decode("utf-8-sig"))
    result = migration_preview(db, data_dir)
    if not result["required"]:
        return {"project": db, "backup": None, "counts": result["counts"]}
    if token != result["token"]:
        raise ValueError("Project or take history changed. Review the migration again.")
    db.update(schema_version=SCHEMA_VERSION)
    db.setdefault("id", uid())
    db.setdefault("characters", [])
    for ep in db["episodes"]:
        ep.setdefault("story_imports", [])
        scenes = ep.setdefault("scenes", [])
        fallback = next(iter(scenes), None)
        if ep["shots"] and not fallback:
            fallback = new_scene()
            scenes.append(fallback)
        for shot in ep["shots"]:
            shot.setdefault("scene_id", fallback["id"])
            shot.setdefault("takes", [])
            shot.setdefault("action", shot.get("prompt", ""))
    sync_takes(db, data_dir)
    backup = path.with_name(f"{path.stem}.pre-story-{uid()}.json")
    with backup.open("xb") as output:
        output.write(original)
        output.flush()
        os.fsync(output.fileno())
    if path.read_bytes() != original:
        raise ValueError("Project or take history changed. Review the migration again.")
    _atomic_save(path, db)
    return {"project": db, "backup": str(backup), "counts": result["counts"]}


def extract_story(filename, raw_bytes):
    ext = Path(filename).suffix.lower()
    if len(raw_bytes) > 16_000_000:
        raise ValueError("Story import exceeds 16 MB.")
    if ext in (".txt", ".md", ".fountain", ".text"):
        try:
            text = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as err:
            raise ValueError("Save the script as UTF-8 and import it again.") from err
    elif ext == ".docx":
        try:
            with ZipFile(BytesIO(raw_bytes)) as doc:
                info = doc.getinfo("word/document.xml")
                if info.file_size > MAX_TEXT * 8:
                    raise ValueError("DOCX text exceeds the import limit.")
                root = ElementTree.fromstring(doc.read(info))
            ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            paragraphs = []
            for paragraph in root.iter(ns+"p"):
                pieces = []
                for part in paragraph.iter():
                    if part.tag == ns+"t":
                        pieces.append(part.text or "")
                    elif part.tag == ns+"tab":
                        pieces.append("\t")
                    elif part.tag in (ns+"br", ns+"cr"):
                        pieces.append("\n")
                paragraphs.append("".join(pieces))
            text = "\n".join(paragraphs)
        except (BadZipFile, KeyError, ElementTree.ParseError) as err:
            raise ValueError("This file is not a readable DOCX document.") from err
    elif ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as err:
            raise ValueError("PDF import requires pypdf in the Studio Python environment.") from err
        try:
            reader = PdfReader(BytesIO(raw_bytes))
            if reader.is_encrypted:
                raise ValueError("Unlock the PDF and import it again.")
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as err:
            raise ValueError(f"Could not extract PDF text: {err}") from err
    else:
        raise ValueError("Choose a TXT, Markdown, Fountain, DOCX or text-based PDF file.")
    if not text.strip():
        raise ValueError("No extractable text found. Use a text-based document or paste the script.")
    if len(text) > MAX_TEXT:
        raise ValueError("Story text exceeds the import limit.")
    return text


_HEADING = re.compile(r"^\s*(?:#{1,6}\s*)?(?:(?:EP\s*\d+)\s*[·|:/–—-]\s*)?"
                      r"(?P<kind>scene|shot|sh|sc|(?:ال)?مشهد|(?:ال)?لقط[هة])\s*#?\s*(?P<number>\d+)"
                      r"(?=\s|$|[.|:：/–—-])(?:\s*[.|:：/–—-]\s*|\s+)?(?P<title>.*?)\s*$", re.I)
_SCREEN_HEADING = re.compile(r"^\s*(?:INT\.|EXT\.|INT\./EXT\.|I/E\.)\s+\S+", re.I)
_OFF = r"(?:O[.\s]?S\.?|O[.\s]?C\.?|V[.\s]?O\.?|off[\s-]?screen|off[\s-]?camera|voice[\s-]?over|خارج (?:الكادر|الشاشة|المشهد|الصورة)|من الخارج)"
_OFF_MARKER = re.compile(r"(?<!\w)"+_OFF+r"(?!\w)", re.I)
_TAG = re.compile(r"^\s*(?P<speaker>[\w][\w’'\- .\u064b-\u065f]{0,48}?)"
                  r"(?:\s*[\[(]\s*(?P<off>"+_OFF+r")\s*[\])])?\s*[:：]\s*(?P<text>\S.*)$", re.I)
_QUOTES = re.compile(r'["“«]([^"”»\n]+)["”»]')
_SAY = re.compile(r"\b(?:says?|asks?|whispers?|replies|shouts?|speaks?|قالت?|يقول|تقول|يسأل|تسأل|يهمس|تهمس)\b", re.I)
_SKIP_TAGS = {"camera", "sound", "lighting", "setting", "action", "narration", "shot", "scene", "description",
              "program", "framing", "environment", "motion", "dialogue", "speaker", "الصوت", "الإضاءة", "الكاميرا",
              "location", "locations", "style", "positive prompt", "negative prompt", "visuals", "visual", "cast",
              "subject definitions", "overall soundscape", "non diegetic music", "duration", "notes", "prompt",
              "sfx", "audio", "music", "continuity", "reference", "references", "character", "characters",
              "المدة", "المكان", "الوصف", "الحوار", "الشخصيات", "المراجع", "وصف المشهد"}
_FIELD_TAG = re.compile(r"^\s*(camera|framing|movement|lighting|environment|setting|motion|sound|camera framing|camera movement|sound requirements|الكاميرا|الإضاءة|البيئة|الحركة|الصوت)\s*[:：]\s*(.+)$", re.I)
_FIELDS = {"framing":"camera", "camera framing":"camera", "camera movement":"movement",
           "sound requirements":"sound", "setting":"environment", "الكاميرا":"camera", "الإضاءة":"lighting",
           "البيئة":"environment", "الحركة":"motion", "الصوت":"sound"}


def _fold(value):
    folded = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[\u064b-\u065f\u0670ـ]", "", folded).translate(str.maketrans("أإآى", "اااي"))


_PRODUCTION_LABELS = {_fold(v) for v in _SKIP_TAGS}


def _production_tag(label):
    return "_" in label or _fold(label).strip() in _PRODUCTION_LABELS


def _names(characters):
    result = {}
    for char in characters:
        for alias in [char["name"], *char.get("aliases", [])]:
            result.setdefault(_fold(alias).strip(), set()).add(char["id"])
    return result


def _matches(text, aliases):
    folded = _fold(text)
    if not aliases:
        return []
    pattern = r"(?<!\w)(?:" + "|".join(re.escape(n) for n in sorted(aliases, key=len, reverse=True) if n) + r")(?!\w)"
    return [(m.group(), m.start(), m.end()) for m in re.finditer(pattern, folded)]


def _speaker_from_cue(cue, aliases):
    names = _matches(cue, aliases)
    verb = _SAY.search(cue)
    if names and (verb or _OFF_MARKER.search(cue)):
        pivot = verb.start() if verb else len(cue)
        _,start,end = min(names, key=lambda row: min(abs(row[1]-pivot), abs(row[2]-pivot)))
        positions = [i for i,ch in enumerate(cue) for _ in _fold(ch)]
        if end <= len(positions):
            return cue[positions[start]:positions[end-1]+1]
    if verb:
        before, after = cue[:verb.start()].strip(" ,،:؛"), cue[verb.end():].strip(" ,،:؛")
        before = _OFF_MARKER.sub("",before).strip(" [](),،")
        after = _OFF_MARKER.sub("",after).strip(" [](),،")
        value = before.split()[-1:] if before else after.split()[:1]
        return " ".join(value)
    return ""


def _cue_line(line, aliases):
    label = line.strip()
    bare = label.rstrip(":：").strip()
    cue = re.sub(r"\s*[\[(]\s*" + _OFF + r"\s*[\])]\s*$", "", bare, flags=re.I).strip()
    if (_production_tag(cue) or not cue or len(cue.split()) > 4 or re.search(r"[.!?,،؟]",cue)):
        return ""
    if cue.isupper() or _fold(cue) in aliases or label.endswith((":", "：")):
        return cue
    return ""


def _dialogues(text, aliases):
    dialogue, visible_text = [], []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if _FIELD_TAG.match(line):
            continue
        tag = _TAG.match(line)
        if tag and _production_tag(tag["speaker"]) and _fold(tag["speaker"]) not in aliases:
            continue
        if tag:
            speaker = tag["speaker"].strip()
            speech_verb = _SAY.search(speaker)
            if speech_verb:
                speaker = _speaker_from_cue(speaker, aliases) or speaker
            dialogue.append({"speaker": speaker, "text": tag["text"], "offscreen": bool(tag["off"])})
            continue
        cue = _cue_line(line,aliases)
        if cue and index < len(lines) and lines[index].strip():
            speech = [lines[index]]
            index += 1
            while (index < len(lines) and lines[index].strip() and not _TAG.match(lines[index])
                   and not _cue_line(lines[index],aliases)):
                speech.append(lines[index])
                index += 1
            dialogue.append({"speaker":cue,"text":"\n".join(speech),"offscreen":bool(_OFF_MARKER.search(line))})
            continue
        quotes = list(_QUOTES.finditer(line))
        if quotes:
            end = 0
            for qi, quoted in enumerate(quotes):
                before = line[end:quoted.start()]
                after = line[quoted.end():quotes[qi+1].start() if qi+1 < len(quotes) else len(line)]
                cue = before if _SAY.search(before) or _OFF_MARKER.search(before) else after
                speaker = _speaker_from_cue(cue, aliases)
                off = bool(_OFF_MARKER.search(cue))
                dialogue.append({"speaker": speaker, "text": quoted.group(1), "offscreen": off})
                end = quoted.end()
            rest = _QUOTES.sub("", line)
            # Remove only the off-screen speaker's cue, keeping other named visual actions.
            for entry in dialogue[-len(quotes):]:
                if entry["offscreen"] and entry["speaker"]:
                    rest = re.sub(re.escape(entry["speaker"]), "", rest, flags=re.I)
            visible_text.append(rest)
        else:
            visible_text.append(line)
    return dialogue, "\n".join(visible_text)


def _unique(values):
    return list(dict.fromkeys(values))


def assignments(shot, characters):
    aliases = _names(characters)
    overrides = shot.get("overrides", {})
    override_aliases = {_fold(k): v for k,v in overrides.get("aliases", {}).items()}
    override_speakers = {_fold(k): v for k,v in overrides.get("speakers", {}).items()}
    valid = {c["id"] for c in characters}
    visible, speaking, blockers, ambiguity_choices = [], [], [], []
    text = shot.get("action", shot.get("prompt", ""))
    dialogue, narrative = _dialogues(text, aliases)
    if shot.get("dialogue_override") is not None:
        dialogue = deepcopy(shot["dialogue_override"])

    def resolve(label, speaker=False):
        key = _fold(label)
        chosen = (override_speakers.get(key) if speaker else None) or override_aliases.get(key)
        if chosen:
            if chosen in valid:
                return chosen
            blockers.append(f"Reassign '{label}': the selected character no longer exists.")
            return ""
        choices = aliases.get(key, set())
        if len(choices) == 1:
            return next(iter(choices))
        if len(choices) > 1:
            blockers.append(f"Choose a character ID for ambiguous {'speaker' if speaker else 'name'} '{label}'.")
            ambiguity_choices.append({"label":label, "kind":"speaker" if speaker else "name", "ids":sorted(choices)})
        elif speaker:
            blockers.append(f"Assign a character ID to speaker '{label or '(unattributed dialogue)'}'.")
            ambiguity_choices.append({"label":label,"kind":"speaker","ids":sorted(valid)})
        return ""

    for label, _, _ in _matches(narrative, aliases):
        if "visible_ids" not in overrides:
            resolved = resolve(label)
            if resolved:
                visible.append(resolved)
    for entry in dialogue:
        resolved = entry.get("character_id") or resolve(entry["speaker"], True)
        if resolved and resolved not in valid:
            blockers.append(f"Replace the unknown speaker ID '{resolved}'.")
        entry["character_id"] = resolved
        if resolved:
            speaking.append(resolved)
            if not entry["offscreen"]:
                visible.append(resolved)
    visible = _unique(overrides.get("visible_ids", visible))
    speaking = _unique(overrides.get("speaking_ids", speaking))
    for char_id in [*visible, *speaking]:
        if char_id not in valid:
            blockers.append(f"Replace the unknown character ID '{char_id}'.")
    return {"visible": visible, "speaking": speaking, "dialogue": dialogue, "blockers": _unique(blockers),
            "ambiguity_choices": ambiguity_choices}


def _estimate(text, dialogue):
    word_count = len(re.findall(r"\w+", text))
    speech = sum(len(re.findall(r"\w+", d["text"])) for d in dialogue)
    events = max(1, len(re.findall(r"[.!?؟؛]|\n", text)))
    pauses = len(re.findall(r"\b(?:pauses?|waits?|silence|يتوقف|ينتظر|صمت)\b|…|\.{3}", text, re.I))
    return max(2, math.ceil(speech/2.45 + max(0, word_count-speech)/4.5 + min(events, 20)*.75 + pauses*1.3))


def _shot(body, scene_id, characters, heading=""):
    shot = {"id": uid(), "revision": uid(), "scene_id": scene_id, "sourceHeading": heading, "action": body,
            "prompt": body, "negative_prompt": "", "camera": "", "movement": "", "lighting": "",
            "environment": "", "motion": "", "sound": "", "workflow_version": "", "change": "",
            "image": "", "refs": [], "approved": False, "takes": [], "overrides": {},
            "manual_images": [], "manual_audio": [], "story_managed": True}
    for line in body.splitlines():
        tagged = _FIELD_TAG.match(line)
        if tagged:
            key = tagged.group(1).casefold()
            shot[_FIELDS.get(key, key)] = tagged.group(2)
    matched = assignments(shot, characters)
    shot.update(visible_ids=matched["visible"], speaking_ids=matched["speaking"], dialogue=matched["dialogue"],
                dialog=bool(matched["dialogue"]), seconds=_estimate(body, matched["dialogue"]))
    return shot


def _units(body, aliases):
    """Propose cuts at supplied boundaries without adding story events."""
    pieces, metadata = [], []
    lines = body.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line.strip():
            continue
        if _FIELD_TAG.match(line) or ((tag := _TAG.match(line)) and _production_tag(tag["speaker"])):
            if pieces:
                pieces[-1] += "\n" + line
            else:
                metadata.append(line)
            continue
        if _cue_line(line,aliases) and index < len(lines) and lines[index].strip():
            bundle = [line, lines[index]]
            index += 1
            while (index < len(lines) and lines[index].strip() and not _TAG.match(lines[index])
                   and not _cue_line(lines[index],aliases)):
                bundle.append(lines[index])
                index += 1
            pieces.append("\n".join(bundle))
        elif _TAG.match(line) or _QUOTES.search(line) or _FIELD_TAG.match(line):
            pieces.append(line)
        else:
            pieces.extend(re.split(r"(?<=[.!?؟])\s+(?=\S)", line))
    if metadata:
        if pieces:
            pieces[0] = "\n".join(metadata+[pieces[0]])
        else:
            pieces.append("\n".join(metadata))
    return [part.strip() for part in pieces if part.strip()]


def parse_story(text, characters=()):
    if not isinstance(text, str) or not text.strip() or len(text)>MAX_TEXT:
        raise ValueError("Enter a story of up to 2,000,000 characters.")
    scenes, scene, heading, body = [], None, "", []
    def ensure_scene():
        nonlocal scene
        if scene is None:
            scene = {**new_scene(), "shots": []}
            scenes.append(scene)
        return scene
    def flush():
        nonlocal body
        value = "\n".join(body).strip()
        if heading and not value:
            raise ValueError(f"Missing shot text under '{heading}'.")
        if value:
            target = ensure_scene()
            for piece in ([value] if heading else _units(value,_names(characters))):
                target["shots"].append(_shot(piece, target["id"], characters, heading))
        body = []
    for line in text.splitlines():
        marker = _HEADING.match(line)
        if not marker and not _SCREEN_HEADING.match(line):
            body.append(line)
            continue
        flush()
        label = re.sub(r"^\s*#{1,6}\s*", "", line).strip()
        if not marker or _fold(marker["kind"]) in {"scene","sc","مشهد","المشهد"}:
            scene = {**new_scene(label), "shots": []}
            scenes.append(scene)
            heading = ""
        else:
            ensure_scene()
            heading = label
    flush()
    if not any(sc["shots"] for sc in scenes):
        raise ValueError("Add narrative text beneath a scene or shot heading.")
    return {"original": text, "scenes": scenes}


def _clean_strings(value, label):
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a list of names.")
    return _unique(item.strip() for item in value if item.strip())


def apply_story(db, episode_id, proposal, assets=()):
    episode = next((e for e in db["episodes"] if e["id"] == episode_id), None)
    if not episode:
        raise ValueError("Select an existing episode.")
    if not isinstance(proposal,dict):
        raise ValueError("Review a valid story proposal.")
    source = proposal.get("original")
    if not isinstance(source, str) or not source.strip() or len(source)>MAX_TEXT:
        raise ValueError("The original story is missing or too large.")
    proposed = proposal.get("scenes")
    if not isinstance(proposed, list) or not proposed:
        raise ValueError("Review a scene and shot breakdown before importing.")
    # Validate the entire proposal before appending a single entity.
    scene_rows, shots = [], []
    known = {row["id"] for e in db["episodes"] for row in [e, *e.get("scenes", []), *e["shots"]]}
    def checked_id(record):
        if not isinstance(record,dict) or not isinstance(record.get("id"),str):
            raise ValueError("The breakdown contains an invalid scene or shot ID.")
        entity_id = record["id"]
        try:
            if str(uuid.UUID(entity_id)) != entity_id or entity_id in known:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as err:
            raise ValueError("The breakdown contains a duplicate or invalid ID. Preview the import again.") from err
        known.add(entity_id)
        return entity_id
    for row in proposed:
        scene_id = checked_id(row)
        title = row.get("title","")
        if not isinstance(title,str) or not title.strip() or not isinstance(row.get("shots"), list):
            raise ValueError("Each proposed scene needs a title and a shot list.")
        scene_rows.append({"id": scene_id, "title": title.strip()})
        for candidate in row["shots"]:
            shot_id = checked_id(candidate)
            action = candidate.get("action", candidate.get("prompt", ""))
            if not isinstance(action, str) or not action.strip():
                raise ValueError("Enter narrative action for each proposed shot.")
            heading = candidate.get("sourceHeading","")
            if not isinstance(heading,str) or candidate.get("scene_id",scene_id) != scene_id:
                raise ValueError("A proposed shot must have a valid title and belong to its scene.")
            if candidate.get("approved",False) is not False or candidate.get("takes",[]) != []:
                raise ValueError("New shots must be reviewed before recording approvals or takes.")
            if candidate.get("image","") or candidate.get("refs",[]) != []:
                raise ValueError("Choose explicit shot references from the available asset library.")
            shot = _shot(action, scene_id, db.get("characters", []), heading)
            # The preview can be edited by the user. Treat it as input again at commit time.
            keys = (*SHOT_TEXT_FIELDS, "seconds", "manual_images","manual_audio","workflowParameters","overrides",
                    "dialogue_override")
            changes = {k:candidate[k] for k in keys if k in candidate}
            staging = {"id":episode_id, "scenes":scene_rows, "shots":[shot]}
            update_shot(db,staging,shot["id"],changes,assets)
            shot["id"] = shot_id
            shots.append(shot)
    if not shots:
        raise ValueError("The breakdown must contain at least one shot.")
    episode.setdefault("scenes", []).extend(scene_rows)
    episode["shots"].extend(shots)
    episode.setdefault("story_imports", []).append({"id": uid(), "original": source,
                                                   "filename": str(proposal.get("filename") or ""),
                                                   "scene_ids":[s["id"] for s in scene_rows]})
    if not episode.get("story"):
        episode["story"] = source
    return episode


def _cast_snapshot(shot, registry):
    matched = assignments(shot, registry)
    cast = set(matched["visible"] + matched["speaking"])
    return matched, [row for row in registry if row["id"] in cast]


def save_character(db, character, assets):
    registry = db.setdefault("characters", [])
    name = character.get("name", "")
    description = character.get("description", "")
    if not isinstance(name, str) or not name.strip() or not isinstance(description, str):
        raise ValueError("Enter a display name and a text description.")
    asset_set = set(assets)
    aliases = _clean_strings(character.get("aliases", []), "Aliases")
    images = _clean_strings(character.get("images", []), "Character images")
    voices = _clean_strings(character.get("voices", []), "Character voices")
    for ref, allowed in [(n, PHOTOS) for n in images] + [(n, VOICES) for n in voices]:
        if ref not in asset_set or Path(ref).suffix.lower() not in allowed:
            raise ValueError(f"Choose a matching library reference for '{ref}'.")
    cid = character.get("id") or uid()
    if not isinstance(cid,str):
        raise ValueError("Invalid character ID.")
    try:
        if str(uuid.UUID(cid)) != cid:
            raise ValueError
    except ValueError as err:
        raise ValueError("Invalid character ID.") from err
    saved = {"id": cid, "name": name.strip(), "description": description.strip(), "aliases": aliases,
             "images": images, "voices": voices}
    index = next((i for i, c in enumerate(registry) if c["id"]==cid),len(registry))
    newer = list(registry)
    newer[index:index+1] = [saved]
    # Preserve approvals that the edited character doesn't affect.
    for ep in db["episodes"]:
        for shot in ep["shots"]:
            if shot.get("approved") and _cast_snapshot(shot, registry) != _cast_snapshot(shot, newer):
                shot.update(approved=False, revision=uid())
    db["characters"] = newer
    return saved


def _duration(seconds, limits):
    def number(key, default):
        value = limits.get(key)
        return float(value) if value is not None else default
    floor = number("min_seconds", 0)
    ceiling = number("max_seconds", math.inf)
    fps = number("fps", 0)
    frames = None
    blockers = []
    if fps > 0:
        step = max(1, int(number("frame_step", 1)))
        first = int(number("frame_base", number("min_frames", 0)))
        low_frames = int(number("min_frames", 1))
        high_frames = number("max_frames", math.inf)
        # The durations stored by AXDAR describe playback time, frames / fps.
        floor = max(floor, low_frames/fps)
        if math.isfinite(high_frames):
            last = first + math.floor((high_frames-first)/step)*step
            ceiling = min(ceiling, last/fps)
        requested = seconds*fps
        frames = first + round((requested-first)/step)*step
        if frames < low_frames:
            frames = first + math.ceil((low_frames-first)/step)*step
        if frames > high_frames:
            frames = first + math.floor((high_frames-first)/step)*step
    if seconds < floor - 1e-8:
        blockers.append(f"Increase duration to at least {floor:g} seconds for this workflow.")
    if seconds > ceiling + 1e-8:
        blockers.append(f"Split the shot or reduce its duration to {ceiling:g} seconds or less for this workflow.")
    return frames, ceiling, blockers


def _split_proposal(shot, ceiling, characters):
    if not math.isfinite(ceiling) or ceiling <= 0 or shot["seconds"] <= ceiling:
        return []
    seconds = shot["seconds"]
    bound = math.floor(ceiling) if float(seconds).is_integer() and ceiling >= 1 else ceiling
    count = math.ceil(seconds/bound)
    source = shot.get("action") or shot.get("prompt", "")
    words = list(re.finditer(r"\S+", source))
    if not words or count > 1000:
        return []
    # Prefer natural boundaries but never invent filler for a prolonged pause or a held image.
    if len(words) < count:
        return []
    cuts = [0]
    for i in range(1, count):
        ideal = round(len(words)*i/count)
        left = max(cuts[-1]+1, ideal-max(1, len(words)//(count*4)))
        right = min(len(words)-(count-i), ideal+max(1, len(words)//(count*4)))
        candidates = [k for k in range(left, right+1)
                      if re.search(r"[.!?؟؛،,:;…]$", words[k-1].group()) or
                      "\n" in source[words[k-1].end():words[k].start()]]
        cuts.append(min(candidates, key=lambda k: abs(k-ideal)) if candidates else
                    min(max(ideal, cuts[-1]+1), len(words)-(count-i)))
    cuts.append(len(words))
    segments = []
    # Carry speaker context into later fragments of a tagged dialogue line.
    char_aliases = _names(characters)
    for i, (a,b) in enumerate(zip(cuts,cuts[1:])):
        start, end = words[a].start(), words[b-1].end()
        fragment = source[start:end]
        seg = _shot(fragment, shot["scene_id"], characters, f"{shot.get('sourceHeading') or 'Shot'} · {i+1}")
        for field in ("camera","movement","lighting","environment","motion","sound","negative_prompt","workflow_version",
                      "manual_images","manual_audio","overrides","workflowParameters"):
            if field in shot:
                seg[field] = deepcopy(shot[field])
        # Preserve edited production prompts separately where they differ from the narrative.
        if shot.get("prompt") and shot["prompt"] != source:
            seg["prompt"] = shot["prompt"] + "\n" + fragment
        if not shot.get("story_managed"):
            seg["manual_images"] = _unique([n for n in [shot.get("image"), *shot.get("refs", [])]
                                             if n and Path(n).suffix.lower() in PHOTOS])
            seg["manual_audio"] = [n for n in shot.get("refs", []) if Path(n).suffix.lower() in VOICES]
        if start:
            original_line = source[source.rfind("\n", 0, start)+1:source.find("\n", start)
                                   if "\n" in source[start:] else len(source)]
            tag = _TAG.match(original_line)
            if tag and not _production_tag(tag["speaker"]) and not _TAG.match(fragment.split("\n")[0]):
                seg["dialogue_context"] = {"speaker": tag["speaker"].strip(), "offscreen": bool(tag["off"])}
            elif not seg["dialogue"] and (quoted := next((q for q in _QUOTES.finditer(source) if q.start()<start<q.end()), None)):
                cue = source[source.rfind("\n",0,quoted.start())+1:quoted.start()]
                seg["dialogue_context"] = {"speaker": _speaker_from_cue(cue,char_aliases), "offscreen": bool(_OFF_MARKER.search(cue))}
        if float(seconds).is_integer() and count <= seconds:
            whole, remainder = divmod(int(seconds), count)
            seg["seconds"] = whole + (i < remainder)
        else:
            seg["seconds"] = seconds/count
        segments.append(seg)
    return segments


def preview_shot(db, episode, shot, assets, limits=None):
    limits = limits or {}
    asset_set = set(assets)
    registry = db.get("characters", [])
    cast = assignments(shot, registry)
    blockers = list(cast["blockers"])
    if shot.get("dialogue_context"):
        # A split inside a dialogue line retains its original attribution without changing source wording.
        context = shot["dialogue_context"]
        contextual = dict(shot, action=f"{context['speaker']}"+(" (O.S.)" if context.get("offscreen") else "")+
                          ": "+shot.get("action", "").split("\n")[0])
        context_cast = assignments(contextual, registry)
        cast["visible"] = _unique(context_cast["visible"]+cast["visible"])
        cast["speaking"] = _unique(context_cast["speaking"]+cast["speaking"])
        cast["dialogue"] = context_cast["dialogue"]+cast["dialogue"]
        blockers.extend(context_cast["blockers"])
    by_id = {c["id"]: c for c in registry}
    if shot.get("story_managed"):
        images = list(shot.get("manual_images", []))
        audio = list(shot.get("manual_audio", []))
    else:
        selected = [n for n in [shot.get("image"), *shot.get("refs", [])] if n]
        images = [n for n in selected if Path(n).suffix.lower() in PHOTOS]
        audio = [n for n in selected if Path(n).suffix.lower() in VOICES]
    descriptions = []
    for cid in _unique(cast["visible"]+cast["speaking"]):
        char = by_id.get(cid)
        if char is None:
            continue
        assigned = [n for n in char["images"] if n in asset_set]
        if not assigned:
            blockers.append(f"Assign an available visual reference to '{char['name']}' in the character registry.")
        elif cid in cast["visible"]:
            images.append(assigned[0])
        if cid in cast["speaking"] and char["voices"]:
            voices = [n for n in char["voices"] if n in asset_set]
            if voices:
                audio.append(voices[0])
            else:
                blockers.append(f"Restore or replace the voice reference for '{char['name']}'.")
        if char.get("description") and cid in cast["visible"]:
            descriptions.append(f"{char['name']}: {char['description']}")
    images, audio = _unique(images), _unique(audio)
    for ref in images+audio:
        if ref not in asset_set:
            blockers.append(f"Restore or replace missing library reference '{ref}'.")
    if limits.get("requires_image") and not images:
        blockers.append("Choose an image reference for this shot in this workflow.")
    for group, key, items in (("image","max_images",images), ("audio","max_audio",audio)):
        capacity = limits.get(key)
        if capacity is not None and len(items)>capacity:
            blockers.append(f"The workflow accepts {capacity} {group} reference(s). Reduce the shot references or select another workflow.")
    positive = str(shot.get("prompt", "")).strip()
    if not positive:
        blockers.append("Enter a positive prompt for this shot.")
    details = [positive]
    for field in ("action","camera","movement","lighting","environment","motion","sound"):
        value = str(shot.get(field, "")).strip()
        if value and value != positive and value not in positive:
            details.append(f"{field.capitalize()}: {value}")
    details.extend(descriptions)
    for row in cast["dialogue"]:
        if row["text"] not in "\n".join(details):
            details.append(f"{row['speaker']}: {row['text']}")
    rules = [r["text"] for r in db.get("rules", []) if r.get("text") and
             (r["scope"]=="all" or r["scope"]=="dialog" and (cast["dialogue"] or shot.get("dialog"))
              or r["scope"] in images+audio)]
    details.extend(rules)
    if shot.get("change"):
        details.append(shot["change"])
    seconds = shot.get("seconds", 0)
    frames, split = None, []
    if not isinstance(seconds, (int,float)) or isinstance(seconds,bool) or not math.isfinite(seconds) or seconds<=0:
        blockers.append("Enter a positive duration in seconds.")
    else:
        frames, maximum, time_blockers = _duration(seconds, limits)
        blockers.extend(time_blockers)
        if seconds>maximum:
            split = _split_proposal(shot, maximum, registry)
    workflow_version = limits.get("version") or shot.get("workflow_version","")
    if limits.get("version") and shot.get("approved") and shot.get("workflow_version") not in ("",None,limits["version"]):
        blockers.append("Review this shot and approve it for the selected workflow version.")
    negative = shot.get("negative_prompt","")
    warnings = []
    if negative and limits.get("negative_prompt") is False:
        warnings.append("This workflow has no negative prompt mapping.")
    blockers = _unique(blockers)
    return {**cast, "ready": not blockers, "blockers": blockers, "warnings": warnings,
            "ambiguities":[b for b in blockers if "ambiguous" in b or "speaker '" in b],
            "positive_prompt": "\n".join(_unique(x for x in details if x)), "negative_prompt": negative,
            "images": images, "audio": audio, "seconds": seconds, "frames": frames,
            "split_proposal": split, "workflow_version": workflow_version}


def _find_shot(episode, shot_id):
    shot = next((s for s in episode["shots"] if s["id"]==shot_id), None)
    if shot is None:
        raise ValueError("Select an existing shot.")
    return shot


def update_shot(db, episode, shot_id, changes, assets, limits=None):
    shot = _find_shot(episode, shot_id)
    draft = deepcopy(shot)
    char_ids = {c["id"] for c in db.get("characters", [])}
    for key, value in changes.items():
        if key in (*SHOT_TEXT_FIELDS, "sourceHeading"):
            if not isinstance(value, str):
                raise ValueError(f"{key} must be text.")
            draft[key] = value
        elif key == "scene_id":
            if not any(s["id"] == value for s in episode.get("scenes",[])):
                raise ValueError("Choose a scene in this episode.")
            draft[key] = value
        elif key=="seconds":
            if type(value) not in (int,float) or not math.isfinite(value) or value<=0:
                raise ValueError("Enter a positive duration in seconds.")
            draft[key] = value
        elif key in ("manual_images","manual_audio"):
            items = _clean_strings(value,key)
            ext = PHOTOS if key=="manual_images" else VOICES
            if any(item not in assets or Path(item).suffix.lower() not in ext for item in items):
                raise ValueError("Choose a matching reference from the asset library.")
            if not draft.get("story_managed"):
                old = [n for n in [draft.get("image"),*draft.get("refs",[])] if n]
                draft["manual_images"] = [n for n in old if Path(n).suffix.lower() in PHOTOS]
                draft["manual_audio"] = [n for n in old if Path(n).suffix.lower() in VOICES]
            draft.update(story_managed=True)
            draft[key] = items
        elif key=="overrides":
            if not isinstance(value,dict) or set(value)-{"visible_ids","speaking_ids","speakers","aliases"}:
                raise ValueError("Invalid character overrides.")
            for k, v in value.items():
                if k.endswith("_ids"):
                    ids = _clean_strings(v,k)
                elif isinstance(v,dict) and all(isinstance(label,str) for label in v):
                    ids = v.values()
                else:
                    raise ValueError("Invalid name or speaker override.")
                if any(not isinstance(cid,str) or cid not in char_ids for cid in ids):
                    raise ValueError("Choose an existing character ID for each override.")
            draft[key] = deepcopy(value)
        elif key=="workflowParameters":
            if not isinstance(value,dict):
                raise ValueError("Workflow parameters must be an object.")
            draft[key] = deepcopy(value)
        elif key=="dialogue_override":
            if value is not None and (not isinstance(value,list) or any(
                    not isinstance(row,dict) or not isinstance(row.get("speaker"),str) or
                    not isinstance(row.get("text"),str) or not isinstance(row.get("offscreen"),bool) or
                    (row.get("character_id") and row["character_id"] not in char_ids) for row in value)):
                raise ValueError("Each dialogue line needs speaker, text and an off-screen flag.")
            draft[key] = deepcopy(value)
        elif key=="approved" and value is False:
            draft[key] = False
        else:
            raise ValueError(f"Unsupported shot edit '{key}'.")
    if draft == shot:
        return shot
    draft.update(approved=False, revision=uid())
    preview = preview_shot(db, episode, draft, assets, limits)
    draft.update(visible_ids=preview["visible"], speaking_ids=preview["speaking"], dialogue=preview["dialogue"])
    if draft.get("story_managed"):
        draft["dialog"] = bool(preview["dialogue"])
    shot.update(draft)
    return shot


def approve_shots(db, episode, ids, assets, limits=None):
    selected = None if ids is None else set(ids)
    if selected is not None and selected - {s["id"] for s in episode["shots"]}:
        raise ValueError("Select shots in this episode.")
    approved, blocked = [], {}
    for shot in episode["shots"]:
        if selected is not None and shot["id"] not in selected:
            continue
        candidate = {**shot, "approved": False}
        preview = preview_shot(db, episode, candidate, assets, limits)
        if not preview["ready"]:
            blocked[shot["id"]] = preview["blockers"]
            shot["approved"] = False
            continue
        workflow_version = preview["workflow_version"]
        if shot.get("workflow_version") != workflow_version:
            shot["revision"] = uid()
        shot.update(approved=True, workflow_version=workflow_version,
                    visible_ids=preview["visible"], speaking_ids=preview["speaking"], dialogue=preview["dialogue"])
        if shot.get("story_managed"):
            shot.update(image=next(iter(preview["images"]),""), refs=preview["images"][1:] + preview["audio"],
                        dialog=bool(preview["dialogue"]))
        approved.append(shot["id"])
    return {"approved":approved, "blocked":blocked}


def split_shot(db, episode, shot_id, assets, limits=None):
    shot = _find_shot(episode,shot_id)
    pieces = preview_shot(db, episode, shot, assets, limits)["split_proposal"]
    if not pieces:
        raise ValueError("No source-based split is available. Edit the duration or add narrative cut points.")
    index = episode["shots"].index(shot)
    pieces[0].update(id=shot["id"],takes=deepcopy(shot.get("takes",[])))
    episode["shots"][index:index+1] = pieces
    return pieces
