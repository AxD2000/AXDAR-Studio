"""Story and workflow operations shared by the HTTP handlers and production snapshots."""
import hashlib
import json
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import story_engine as story
import workflow_profiles as profiles


def find_id(items, key, label):
    item = next((item for item in items if item["id"] == key), None)
    if item is None:
        raise ValueError(label + " was not found. Refresh the project.")
    return item


def url_for_comfy(value):
    value = str(value or "").strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Enter the HTTP or HTTPS address of your ComfyUI server.")
    return value


def fingerprint(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def production_prompt(wf, text, voices):
    parts = [text]
    prefix = str((wf.get("mapping") or {}).get("promptPrefix") or "").strip()
    if prefix:
        parts.insert(0, prefix)
    if wf.get("reference") and wf.get("graph", {}).get(wf["reference"], {}).get("class_type") == "MiniMaxH3ReferenceToVideo":
        parts.extend(f"<Audio {i}> is the voice reference for {Path(name).stem}." for i, name in enumerate(voices, 1))
    return "\n".join(filter(None, parts))


class StudioActions:
    def __init__(self, ctx):
        self.ctx = ctx
        self.store = profiles.WorkflowStore(ctx["BASE"] / "workflow-profiles")

    def bootstrap_workflow(self):
        current = self.store.active()
        if current:
            return current
        if self.store.list():
            return None
        path = self.ctx["WORKFLOW_STATE"]
        if path.is_file():
            raw = path.read_bytes()
            name = json.loads(raw.decode("utf-8-sig")).get("name", path.name)
        else:
            path = self.ctx["BASE"] / "workflow.json"
            if not path.is_file():
                return None
            raw, name = path.read_bytes(), path.name
        return self.store.import_workflow(name, raw)

    def commit(self, db):
        db["_edit"] = uuid.uuid4().hex
        self.ctx["save"](self.ctx["DB"], db)
        return db

    def check(self, db, request):
        if db.get("schema_version") != story.SCHEMA_VERSION:
            raise ValueError("Review the migration and create a backup before editing this project.")
        if request.get("edit") is not None and request["edit"] != db.get("_edit"):
            raise ValueError("Another edit was saved. Reload the project before applying this change.")

    def general_save(self, incoming):
        db = self.ctx["project"]()
        self.check(db, {"edit":incoming.get("_edit")})
        if incoming.get("_edit") != db.get("_edit") or incoming.get("id") != db["id"]:
            raise ValueError("Project changed. Reload before saving.")
        rules = incoming.get("rules", [])
        if not isinstance(rules, list) or not all(isinstance(r, dict) and isinstance(r.get("text"), str) and isinstance(r.get("scope"), str) for r in rules):
            raise ValueError("Invalid continuity rules.")
        changed = db["rules"] != rules
        db["rules"] = rules
        titles = {item["id"]:item["title"] for item in incoming["episodes"]}
        for ep in db["episodes"]:
            if ep["id"] in titles:
                ep["title"] = str(titles[ep["id"]])
            if changed:
                for shot in ep["shots"]:
                    if shot.get("approved"):
                        shot.update(approved=False, revision=str(uuid.uuid4()))
        return {"edit":self.commit(db)["_edit"]}

    def workflow(self, route, obj, raw=None, filename=None):
        if route == "list":
            active = self.ctx["active_workflow"]
            entries = [profiles.public_profile(self.store.get(row["version"])) for row in self.store.list()]
            return {"profiles":entries, "active":active.get("version") if active else None}
        if route == "import":
            wf = self.store.import_workflow(filename or "workflow.json", raw)
        elif route == "attach":
            wf = self.store.attach(obj["version"], raw)
        elif route == "select":
            wf = self.store.select(obj["version"])
        elif route == "map":
            wf = self.store.remap(obj["version"], obj["mapping"], select=False)
        elif route == "inspect":
            wf = self.store.get(obj["version"])
            metadata = obj.get("metadata")
            if metadata is None:
                url = url_for_comfy(obj.get("url") or self.ctx["controller"].connection.snapshot().get("url"))
                metadata = self.ctx["json_api"](url, "/object_info")
            if not isinstance(metadata, dict) or not metadata:
                raise ValueError("Supply ComfyUI /object_info JSON.")
            wf = self.store.inspect_metadata(obj["version"], metadata)
        else:
            raise ValueError("Unknown workflow operation.")
        active = self.store.active()
        self.ctx["active_workflow"] = active
        self.ctx["workflow_error"] = "" if active else "Map and select a compatible workflow."
        return {"profile":profiles.public_profile(wf), "active":active["version"] if active else None}

    def preview(self, db, ep, shot, assets, graph=True):
        wf = self.ctx["active_workflow"]
        limits = profiles.limits(wf) if wf else {}
        view = story.preview_shot(db, ep, shot, assets, limits)
        issues = list(view["blockers"])
        if not wf:
            issues.append("Select a mapped workflow in Production.")
        else:
            images, voices = view["images"], view["audio"]
            view["positive_prompt"] = production_prompt(wf, view["positive_prompt"], voices)
            view["workflow_version"] = wf["version"]
            view["frames"] = profiles.timing(wf, view["seconds"])["frames"]
            runtime = dict(shot, seconds=view["seconds"], negativePrompt=view["negative_prompt"])
            issues.extend(wf.get("issues", []))
            issues.extend(profiles.validate_shot(wf, runtime, images, voices))
            if graph and not issues:
                try:
                    view["graph"] = profiles.runtime_graph(wf, runtime, view["positive_prompt"], images, voices,
                                                           lambda name:name, "AXDAR/" + ep["id"] + "/" + shot["id"] + "/preview")
                except ValueError as exc:
                    issues.append(str(exc))
        view["blockers"] = list(dict.fromkeys(issues))
        view["ready"] = not view["blockers"]
        return view

    def approved_jobs(self, db, ep):
        self.check(db, {})
        assets = self.ctx["active_assets"](db)
        jobs, skipped = [], {}
        for shot in ep["shots"]:
            if not shot.get("approved"):
                continue
            view = self.preview(db, ep, shot, assets, graph=False)
            if shot.get("workflow_version") != view.get("workflow_version"):
                view["blockers"].append("Review and approve this shot for the selected workflow version.")
            if shot.get("approval_inputs") and shot["approval_inputs"] != self.approval_key(view, shot):
                view["blockers"].append("Production inputs changed. Review and approve this shot again.")
            if view["blockers"]:
                skipped[shot["id"]] = view["blockers"]
                continue
            images, voices = view["images"], view["audio"]
            frozen = dict(shot, image=images[0] if images else "", refs=images[1:] + voices,
                          prompt=view["positive_prompt"], frozenPrompt=view["positive_prompt"], seconds=view["seconds"],
                          negativePrompt=view["negative_prompt"], workflow_version=view["workflow_version"])
            frozen.pop("takes", None)
            jobs.append([ep["id"], frozen, db["rules"]])
        return jobs, skipped

    @staticmethod
    def approval_key(view, shot):
        keys = ("positive_prompt","negative_prompt","images","audio","seconds","workflow_version")
        return fingerprint([[view.get(key) for key in keys], shot.get("workflowParameters", {})])

    def act(self, path, obj, raw=None, filename=None):
        db = self.ctx["project"]()
        if path == "/migrate":
            result = story.migrate(self.ctx["DB"], self.ctx["DATA"], obj["token"])
            project = result["project"]
            if result.get("backup") and self.ctx.get("active_workflow"):
                version = self.ctx["active_workflow"]["version"]
                for ep in project["episodes"]:
                    for shot in ep["shots"]:
                        if shot.get("approved") and not shot.get("workflow_version"):
                            shot["workflow_version"] = version
            if not project.get("_edit") or result.get("backup"):
                self.commit(project)
            if result.get("backup"):
                result["backup"] = str(result["backup"])
            return result
        self.check(db, obj)
        if path == "/story/parse":
            text = story.extract_story(filename, raw) if raw is not None else obj.get("text", "")
            return dict(story.parse_story(text, db["characters"]), filename=filename or "")
        assets = self.ctx["active_assets"](db)
        wf = self.ctx["active_workflow"]
        limits = profiles.limits(wf) if wf else {}
        ep = find_id(db["episodes"], obj["episode"], "Episode") if "episode" in obj else None
        result = {}
        if path == "/project/episode":
            db["episodes"].append(story.new_episode(str(obj["title"])))
        elif path == "/project/scene":
            if obj.get("id"):
                find_id(ep["scenes"], obj["id"], "Scene")["title"] = str(obj["title"])
            else:
                ep["scenes"].append(story.new_scene(str(obj["title"])))
        elif path == "/characters/save":
            story.save_character(db, obj["character"], assets)
        elif path == "/story/commit":
            story.apply_story(db, ep["id"], obj["proposal"], assets)
        elif path == "/shots/new":
            scene = find_id(ep["scenes"], obj["scene"], "Scene")
            parsed = story.parse_story(obj.get("text") or "New shot", db["characters"])
            shots = [s for unit in parsed["scenes"] for s in unit["shots"]]
            if len(shots) != 1:
                raise ValueError("Enter one shot, or import a story with a breakdown.")
            shot = shots[0]
            shot["scene_id"] = scene["id"]
            ep["shots"].append(shot)
        elif path == "/shots/preview":
            shot = find_id(ep["shots"], obj["shot"], "Shot")
            return self.preview(db, ep, shot, assets)
        elif path == "/shots/update":
            story.update_shot(db, ep, obj["shot"], obj["changes"], assets, limits)
        elif path == "/shots/remove":
            shot = find_id(ep["shots"], obj["shot"], "Shot")
            if shot.get("takes") or any((self.ctx["DATA"]/ep["id"]/shot["id"]).glob("*/job.json")):
                raise ValueError("This shot has production history and must be kept.")
            ep["shots"].remove(shot)
        elif path == "/shots/split":
            story.split_shot(db, ep, obj["shot"], assets, limits)
        elif path == "/shots/approve":
            selected = [find_id(ep["shots"], obj["shot"], "Shot")] if obj.get("shot") else ep["shots"]
            approved, blocked = [], {}
            for shot in selected:
                view = self.preview(db, ep, {**shot, "approved":False}, assets, graph=False)
                if not view["ready"]:
                    blocked[shot["id"]] = view["blockers"]
                    shot["approved"] = False
                    continue
                key = self.approval_key(view, shot)
                if shot.get("approval_inputs") != key or not shot.get("approved"):
                    shot["revision"] = str(uuid.uuid4())
                shot.update(approved=True, workflow_version=wf["version"], approval_inputs=key)
                shot.update(visible_ids=view["visible"], speaking_ids=view["speaking"], dialogue=view["dialogue"])
                if shot.get("story_managed"):
                    shot.update(image=next(iter(view["images"]),""), refs=view["images"][1:] + view["audio"], dialog=bool(view["dialogue"]))
                approved.append(shot["id"])
            result.update(approved=approved, blocked=blocked)
        else:
            raise ValueError("Unknown story operation.")
        result["project"] = self.commit(db)
        return result
