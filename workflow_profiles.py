"""Immutable ComfyUI API imports, reviewed semantic mappings, and per-shot graphs."""
import copy
import hashlib
import json
import math
import re
import uuid
from pathlib import Path

from provision import MODEL_SUFFIXES, model_choice


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _link(value):
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and type(value[1]) is int


def _parse(raw):
    if isinstance(raw, (bytes, str)):
        raw = json.loads(raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw)
    if not isinstance(raw, dict):
        raise ValueError("Expected a ComfyUI API graph.")
    graph = raw.get("prompt", raw.get("graph", raw))
    if not isinstance(graph, dict) or not graph or "nodes" in graph:
        raise ValueError("This is a ComfyUI editor workflow. Export it in API format.")
    missing = [key for key, node in graph.items() if not isinstance(node, dict) or not node.get("class_type")]
    if missing:
        raise ValueError("API export is missing class_type for nodes " + ", ".join(missing) + ". Re-export with the required nodes installed.")
    for key, node in graph.items():
        if not isinstance(key, str) or not isinstance(node["class_type"], str) or not isinstance(node.get("inputs"), dict):
            raise ValueError(f"Invalid API node {key}.")
        for slot, value in node["inputs"].items():
            if _link(value) and (value[0] not in graph or value[1] < 0):
                raise ValueError(f"Node {key}.{slot} has a broken connection to {value[0]}.")
    return graph


def editor_graph(raw):
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Expected a ComfyUI JSON object.")
    graph = data.get("workflow", data.get("graph", data))
    if not isinstance(graph, dict) or "nodes" not in graph:
        return None
    nodes = graph["nodes"]
    if not isinstance(nodes, list) or not nodes or any(
        not isinstance(node, dict) or not isinstance(node.get("id"), (int, str))
        or not isinstance(node.get("type"), str) or not node["type"] for node in nodes
    ) or len({str(node["id"]) for node in nodes}) != len(nodes):
        raise ValueError("Malformed ComfyUI editor workflow nodes.")
    return {str(node["id"]): node["type"] for node in nodes}


def _upstream(graph, roots):
    seen, pending = set(), list(roots)
    while pending:
        node = pending.pop()
        if node not in seen:
            seen.add(node)
            pending.extend(v[0] for v in graph[node]["inputs"].values() if _link(v))
    return seen


def _title(node):
    return str(node.get("_meta", {}).get("title") or node["class_type"])


def _contract(info, kind):
    # Store only contracts relevant to this graph; a server can expose thousands of unrelated nodes.
    result = {}
    for group in ("required", "optional"):
        for key, spec in info.get(kind, {}).get("input", {}).get(group, {}).items():
            if not isinstance(spec, (list, tuple)) or not spec:
                continue
            options = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            typ = "COMBO" if isinstance(spec[0], list) else str(spec[0])
            result[key] = dict(type=typ, required=group == "required")
            result[key].update({k: options[k] for k in ("default","min","max","step","image_upload","audio_upload") if k in options})
            if isinstance(spec[0], list) or isinstance(options.get("options"), list):
                result[key]["options"] = spec[0] if isinstance(spec[0], list) else options["options"]
    return result


def _outputs(graph, info):
    used = {v[0] for node in graph.values() for v in node["inputs"].values() if _link(v)}
    items = []
    for ident, node in graph.items():
        kind, inputs = node["class_type"], node["inputs"]
        if inputs.get("save_output") is False:
            continue
        if info.get(kind, {}).get("output_node") is False or (ident in used and not info.get(kind, {}).get("output_node")):
            continue
        fmt = str(inputs.get("format", inputs.get("container", ""))).lower()
        if kind in ("SaveImage","PreviewImage","SaveAudio","SaveAudioMP3","SaveAudioOpus","SaveAnimatedWEBP","SaveAnimatedPNG"):
            medium = "unsupported"
        elif kind == "VHS_VideoCombine":
            medium = "video" if "mp4" in fmt else "unsupported"
        elif kind == "SaveVideo":
            medium = "video" if fmt in ("", "auto", "mp4", "video/mp4") else "unsupported"
        elif info.get(kind, {}).get("output_node") and "mp4" in fmt:
            medium = "video"
        elif any(m in fmt for m in ("webm", "webp", "png", "gif", "mov", "mkv", "wav", "mp3")):
            medium = "unsupported"
        else:
            medium = "review"
        items.append(dict(id=ident, label=f"{ident} · {_title(node)}", kind=medium))
    return items


def _one(items):
    return items[0] if len(items) == 1 else ""


def _fields(graph, branch, info, manual_media=True):
    fields, consumers = [], {}
    for ident in graph:
        if ident not in branch:
            continue
        for key, v in graph[ident]["inputs"].items():
            if _link(v):
                consumers.setdefault(v[0], []).append([ident,key])
    for ident, node in graph.items():
        if ident not in branch:
            continue
        kind, inputs = node["class_type"], node["inputs"]
        schema = _contract(info, kind)
        for key in dict.fromkeys([*inputs,*sorted(schema)]):
            value = inputs.get(key)
            if _link(value):
                continue
            spec = schema.get(key, {})
            typ = spec.get("type") or ("BOOLEAN" if type(value) is bool else "INT" if type(value) is int else "FLOAT" if type(value) is float else "STRING" if isinstance(value,str) else "UNKNOWN")
            if kind == "PrimitiveFloat" and key == "value":
                typ = "FLOAT"
            media = None
            if (kind == "LoadImage" and key == "image") or spec.get("image_upload") or typ == "IMAGE":
                media = "images"
            if (kind == "LoadAudio" and key == "audio") or spec.get("audio_upload") or typ == "AUDIO":
                media = "audio"
            if typ in ("STRING","COMBO") and key.lower() in ("image_path","image_file","reference_image"):
                media = "images"
            if typ in ("STRING","COMBO") and key.lower() in ("audio_path","audio_file","reference_audio","voice_path"):
                media = "audio"
            role = [media] if media else ["parameter"]
            if not media and typ == "STRING":
                role += ["positive","negative"]
                if manual_media and not spec.get("options"):
                    # A custom STRING socket may take a server-side file name. Only a human may bind it.
                    role += ["images","audio"]
            if not media and typ in ("INT","FLOAT"):
                role += ["duration","frames"]
            # Dynamic sockets and upload UI widgets are represented by the actual API keys.
            if typ in ("COMFY_AUTOGROW_V3","AUTOGROW","IMAGEUPLOAD","AUDIOUPLOAD") and key not in inputs:
                continue
            field = dict(id=ident+"."+key, node=ident, key=key, label=f"{ident} · {_title(node)} · {key}",
                         type=typ, roles=role, value=value, present=key in inputs,
                         required=spec.get("required", True), consumers=consumers.get(ident, []))
            field.update({k: spec[k] for k in ("min","max","step","default","options") if k in spec})
            if media:
                field["media"] = media
                field["socket"] = typ in ("IMAGE","AUDIO")
                if not field["socket"] and kind in ("LoadImage","LoadAudio"):
                    field["bypass"] = bool(field["consumers"]) and all(
                        _contract(info,graph[dst]["class_type"]).get(slot,{}).get("required") is False
                        for dst,slot in field["consumers"])
                else:
                    field["bypass"] = not field["required"]
            elif manual_media and "images" in role:
                field["bypass"] = not field["required"]
            fields.append(field)
    return fields


def _role_hints(field, graph):
    key = field["key"].lower()
    node = graph[field["node"]]
    label = _title(node).lower()
    dest = {slot.lower() for _, slot in field["consumers"]}
    hints = []
    if "positive" in field["roles"] and not field.get("options"):
        if key in ("negative","negative_prompt") or (key in ("text","value","prompt") and ("negative" in label or "negative" in dest)):
            hints.append("negative")
        elif key in ("positive","positive_prompt","prompt") or (key in ("text","value") and ("positive" in label or "prompt" in label or dest.intersection(("positive","prompt","positive_prompt")))):
            hints.append("positive")
    if "duration" in field["roles"]:
        if key in ("duration","seconds","duration_seconds","video_duration") or (key == "value" and "duration" in label and "frame" not in label):
            hints.append("duration")
        if key in ("num_frames","frame_count","frames","video_frames") or (key == "value" and dest.intersection(("num_frames","frame_count","frames"))):
            hints.append("frames")
    return hints


# ComfyUI's MiniMaxH3ReferenceToVideo contract: optional Autogrow groups and a 17k+5
# frame grid. These constraints apply only to this node, never to an unrelated workflow.
# https://github.com/Comfy-Org/ComfyUI/blob/master/comfy_extras/nodes_minimax_h3.py
_H3_GROUPS = {"images": ("ref_images","ref_image","LoadImage","image",9),
              "audio": ("ref_audios","ref_audio","LoadAudio","audio",3)}
_H3_EXPR = "max(5,round(a*24))+(5-(max(5,round(a*24))%17))%17"


def _h3_fields(graph, ident, fields, suggested, info, fps):
    node = graph[ident]
    inputs = node["inputs"]
    for role, (group, _key, loader_kind, loader_slot, _) in _H3_GROUPS.items():
        # Reuse only the existing loader nodes already authored in the exported graph.
        wired = []
        for slot, target in inputs.items():
            if not (slot.startswith(group + ".") and _link(target)):
                continue
            src_id = target[0]
            src_node = graph.get(src_id, {})
            if src_node.get("class_type") == loader_kind and loader_slot in src_node.get("inputs", {}):
                suffix = slot.rsplit("_", 1)[-1]
                order = (0, int(suffix), slot) if suffix.isdecimal() else (1, 0, slot)
                wired.append((order, slot, src_id, loader_slot))
        wired.sort(key=lambda item: item[0])
        loaders = [[slot, src_id, lslot] for _, slot, src_id, lslot in wired]
        used = {src_id for _, _, src_id, _ in wired}
        fields[:] = [f for f in fields if not (f["node"] in used and role in f["roles"] and all(dst==ident and slot.startswith(group+".") for dst,slot in f["consumers"]))]
        field_id = ident+"."+group
        fields.append(dict(id=field_id,node=ident,key=group,label=_title(node)+" · "+group,type="AUTOGROW",roles=[role],media=role,value=None,required=False,present=False,bypass=True,group=True,loaders=loaders))
        suggested[role] = [field_id]
    frame_spec = _contract(info,node["class_type"]).get("length", {})
    suggested.update(minFrames=frame_spec.get("min",5), maxFrames=frame_spec.get("max",3600),
                     frameStep=frame_spec.get("step",17), frameBase=frame_spec.get("min",5))
    link = inputs.get("length")
    duration = None
    if _link(link):
        expr = graph[link[0]]
        source = expr["inputs"].get("values.a")
        if (expr["class_type"] == "ComfyMathExpression" and
            re.sub(r"\s", "", str(expr["inputs"].get("expression", ""))) == _H3_EXPR and _link(source)):
            duration = next((f for f in fields if f["node"] == source[0] and f["key"] == "value" and "duration" in f["roles"]), None)
    if duration and fps==24:
        suggested.update(duration=duration["id"],frames="")
    else:
        frame_id = ident+".length"
        if not any(f["id"] == frame_id for f in fields):
            fields.append(dict(id=frame_id,node=ident,key="length",label=_title(node)+" · length",type="INT",roles=["frames"],value=None,required=True,present=True))
        suggested.update(duration="", frames=frame_id)


def _output_fps(graph, output, branch):
    def rate(value):
        if _link(value):
            value = graph[value[0]]["inputs"].get("value")
        return value if _number(value) and value>0 else None
    inputs = graph.get(output,{}).get("inputs",{})
    for key in ("frame_rate","fps"):
        if key in inputs:
            return rate(inputs[key])
    # A core SaveVideo takes a VIDEO from CreateVideo; the rate lives on that producer.
    known = {rate(n["inputs"].get("fps")) for k,n in graph.items() if k in branch and n["class_type"]=="CreateVideo"}
    return _one(sorted(v for v in known if v is not None))


def inspect_workflow(name, raw_or_dict, object_info=None, mapping=None):
    graph = _parse(raw_or_dict)
    info = object_info or {}
    override = mapping or {}
    if not isinstance(info, dict) or not isinstance(override, dict):
        raise ValueError("Workflow metadata and mapping must be objects.")
    outputs = _outputs(graph,info)
    confirmed = [item["id"] for item in outputs if item["kind"] == "video"]
    output = override.get("output", _one(confirmed))
    if not isinstance(output,str):
        raise ValueError("The output mapping must be a node ID.")
    branch = _upstream(graph, [output]) if isinstance(output,str) and output in graph else set(graph)
    h3 = _one([k for k in branch if graph[k]["class_type"]=="MiniMaxH3ReferenceToVideo"])
    fields = _fields(graph,branch,info,manual_media=not h3)
    suggested = dict(positive="",negative="",duration="",frames="",images=[],audio=[],output=output,parameters={})
    issues = []
    for role in ("positive","negative","duration","frames"):
        candidates = [f["id"] for f in fields if role in _role_hints(f,graph)]
        suggested[role] = _one(candidates)
        if len(candidates)>1 and role not in override:
            issues.append(f"Choose the {role} input among {', '.join(candidates)}.")
    for role in ("images","audio"):
        candidates = [f["id"] for f in fields if f.get("media")==role and not f.get("socket")]
        suggested[role] = candidates if len(candidates) <= 1 else []
    fps = _output_fps(graph,output,branch)
    if fps:
        suggested["fps"] = fps
    if h3:
        _h3_fields(graph,h3,fields,suggested,info,fps)
    reviewed = dict(suggested, **override)
    # Null UI values represent an unspecified constraint, not a numeric bound.
    reviewed = {k:v for k,v in reviewed.items() if v is not None or k in suggested}
    by_id = {f["id"]: f for f in fields}
    output_item = next((item for item in outputs if item["id"]==output), {})
    if not output_item or output_item["kind"]=="unsupported":
        issues.append("Select an output node that saves an MP4 video.")
    if not reviewed["positive"]:
        issues.append("Map the positive prompt input.")
    if sum(bool(reviewed.get(k)) for k in ("duration","frames","fixedSeconds"))!=1:
        issues.append("Map one duration or frame-count control, or confirm the fixed duration in seconds.")
    for role in ("positive","negative","duration","frames"):
        value = reviewed.get(role)
        if value and (not isinstance(value,str) or role not in by_id.get(value,{}).get("roles",())):
            issues.append(f"Invalid {role} mapping.")
    if reviewed.get("positive") and reviewed["positive"]==reviewed.get("negative"):
        issues.append("Positive and negative prompts need separate fields.")
    for role in ("images","audio"):
        values = reviewed.get(role)
        if not isinstance(values,list) or any(not isinstance(v,str) or role not in by_id.get(v,{}).get("roles",()) for v in values):
            issues.append(f"Invalid {role} input list.")
            continue
        if len(set(values))!=len(values):
            issues.append(f"Duplicate {role} input.")
        for fid in values:
            if by_id.get(fid,{}).get("socket"):
                issues.append(f"Connect a Load{'Image' if role=='images' else 'Audio'} node to {fid} in ComfyUI and re-export; AXDAR does not add nodes.")
        available = [f["id"] for f in fields if f.get("media")==role and not f.get("socket")]
        required = [fid for fid in available if not by_id[fid].get("bypass")]
        if any(fid not in values for fid in required) or (len(available)>1 and role not in override and not h3):
            issues.append(f"Map and order the {role} inputs: "+", ".join(available))
    for key in ("fps","fixedSeconds","minSeconds","maxSeconds","minFrames","maxFrames","frameStep","frameBase"):
        val = reviewed.get(key)
        if val is not None and (not _number(val) or val < 0 or (val==0 and key not in ("frameBase","minFrames","minSeconds"))):
            issues.append(f"Invalid {key}.")
        elif val is not None and key in ("minFrames","maxFrames","frameStep","frameBase") and int(val)!=val:
            issues.append(f"{key} must be an integer.")
    if reviewed.get("frames") and not reviewed.get("fps"):
        issues.append("Enter the video frame rate (fps).")
    num = by_id.get(reviewed.get("frames")) if isinstance(reviewed.get("frames"),str) else None
    if num and "step" in num and _number(num["step"]) and num["step"]>0:
        step,base = reviewed.get("frameStep",num["step"]),reviewed.get("frameBase",num.get("min",0))
        if _number(step) and _number(base) and (step%num["step"] or (base-num.get("min",0))%num["step"]):
            issues.append("The frame grid must honor the node's frame-count step and base.")
    if h3 and any(reviewed.get(k)!=v for k,v in (("frameStep",17),("frameBase",5))):
        issues.append("MiniMax H3 requires a 17n+5 frame grid.")
    if _number(fps) and reviewed.get("fps")!=fps:
        issues.append(f"The selected video output specifies {fps:g} fps.")
    if not isinstance(reviewed.get("parameters"),dict):
        issues.append("Runtime parameters must be an object keyed by input ID.")
    else:
        chosen = [v for k in ("positive","negative","duration","frames") if isinstance((v:=reviewed.get(k)),str) and v]
        for role in ("images","audio"):
            if isinstance(reviewed.get(role),list):
                chosen.extend(v for v in reviewed[role] if isinstance(v,str))
        bound = set(chosen)
        if len(chosen)!=len(bound):
            issues.append("Each input must have only one semantic role.")
        for k,val in reviewed["parameters"].items():
            if k not in by_id or "parameter" not in by_id[k]["roles"] or k in bound:
                issues.append(f"Invalid runtime parameter {k}.")
            elif (err:=_value_error(by_id[k],val)):
                issues.append(err)
        for f in fields:
            if f.get("present") or not f["required"] or f["id"] in bound or "parameter" not in f["roles"]:
                continue
            if f["id"] not in reviewed["parameters"] and f.get("default") is None:
                issues.append(f"Supply required input {f['id']} ({f['type']}).")
    wf = dict(name=str(name), graph=graph, output=output, adapter="minimax-h3" if h3 else "api", mapping=reviewed,
              fields=fields, outputs=outputs, issues=issues)
    if h3:
        wf["reference"] = h3
        wf["modelFrames"] = {k:suggested[k] for k in ("minFrames","maxFrames","frameStep","frameBase")}
        wf["loader"] = next((v[0] for k,v in graph[h3]["inputs"].items() if k.startswith("ref_images.") and _link(v)), "")
    for k in ("positive","duration"):
        if isinstance(reviewed.get(k),str) and reviewed[k]:
            wf["prompt" if k=="positive" else "duration"] = by_id.get(reviewed[k],{}).get("node")
    if not issues:
        constraints = limits(wf)
        for minimum,maximum in (("min_frames","max_frames"),("min_seconds","max_seconds")):
            lo,hi = constraints[minimum],constraints[maximum]
            if (lo is not None and hi is not None and lo>hi) or (hi is not None and hi<=0):
                issues.append(f"Workflow has incompatible {minimum} and {maximum}.")
    wf["version"] = _digest(_json([graph,reviewed,fields,outputs,issues]))
    return wf


def _value_error(field, value):
    typ = field["type"]
    if typ in ("INT","FLOAT"):
        if not _number(value) or (typ == "INT" and int(value)!=value):
            return f"{field['id']} needs a {typ} value."
        if ("min" in field and value<field["min"]) or ("max" in field and value>field["max"]):
            return f"{field['id']} is outside the node's allowed range."
    elif typ == "BOOLEAN" and not isinstance(value,bool):
        return f"{field['id']} needs a boolean."
    elif typ == "STRING" and not isinstance(value,str):
        return f"{field['id']} needs text."
    elif typ not in ("INT","FLOAT","BOOLEAN","STRING","COMBO"):
        return f"{field['id']} requires a connected {typ} node; fix the original API graph."
    if "options" in field and value not in field["options"]:
        return f"{field['id']} needs one of its declared choices."
    return ""


def _bound(a,b,fn):
    values = [v for v in (a,b) if _number(v)]
    return fn(values) if values else None


def limits(wf):
    m = wf["mapping"]
    fields = {f["id"]:f for f in wf["fields"]}
    duration = fields.get(m.get("duration"),{})
    frames = fields.get(m.get("frames"),{})
    fmin = _bound(frames.get("min"),m.get("minFrames"),max)
    fmax = _bound(frames.get("max"),m.get("maxFrames"),min)
    fps = m.get("fps")
    step = m.get("frameStep",frames.get("step",1))
    base = m.get("frameBase",frames.get("min",0))
    if wf["adapter"]=="minimax-h3":
        fmin = _bound(wf["modelFrames"]["minFrames"],fmin,max)
        fmax = _bound(wf["modelFrames"]["maxFrames"],fmax,min)
        step,base = 17,5
    lo = _bound(duration.get("min"),m.get("minSeconds"),max)
    hi = _bound(duration.get("max"),m.get("maxSeconds"),min)
    if _number(fps) and fps>0 and _number(step) and step>0:
        if fmin is not None:
            lo = _bound(lo,max(0,base+math.ceil((fmin-base)/step)*step)/fps,max)
        if fmax is not None:
            hi = _bound(hi,(base+math.floor((fmax-base)/step)*step)/fps,min)
    fixed = m.get("fixedSeconds")
    lo = _bound(lo,fixed,max)
    hi = _bound(hi,fixed,min)
    refs = {}
    for role in ("images","audio"):
        chosen = [fields[v] for v in m.get(role,[]) if v in fields]
        cap = sum(_H3_GROUPS[role][-1] if f.get("group") else (0 if f.get("socket") else 1) for f in chosen)
        refs["max_"+role] = cap
        refs["min_"+role] = sum(not f.get("bypass") for f in chosen)
    return dict(version=wf.get("version"),min_seconds=lo,max_seconds=hi,min_frames=fmin,max_frames=fmax,
                fps=fps,frame_step=step,frame_base=base,negative_prompt=bool(m.get("negative")),
                requires_image=bool(refs["min_images"]),
                trained_frame_range=[124,362] if wf["adapter"]=="minimax-h3" else None,**refs)


def timing(wf, seconds):
    result = dict(seconds=seconds, frames=None, playback_seconds=seconds, split=[], issues=[])
    if not _number(seconds) or seconds<=0:
        result["issues"].append("Set a positive, finite shot duration.")
        return result
    if wf["issues"]:
        result["issues"].extend(wf["issues"])
        return result
    constraints = limits(wf)
    lo,hi = constraints["min_seconds"],constraints["max_seconds"]
    if hi is not None and seconds>hi+1e-8:
        # Use equal whole-second pieces where possible. Never round above the supported maximum.
        ceiling = math.floor(hi) or hi
        count = math.ceil(seconds/ceiling)
        if count<=1000:
            whole = float(seconds).is_integer() and count<=seconds
            base,remainder = divmod(int(seconds),count) if whole else (0,0)
            pieces = [base+(i<remainder) if whole else seconds/count for i in range(count)]
            if all((lo is None or s>=lo) and s<=hi for s in pieces):
                result["split"] = pieces
        result["issues"].append(f"Shot exceeds this workflow's {hi:g} s maximum. Review a split before production.")
    elif lo is not None and seconds+1e-8<lo:
        result["issues"].append(f"Shot is below this workflow's {lo:g} s minimum.")
    duration_field = next((f for f in wf["fields"] if f["id"]==wf["mapping"].get("duration")), {})
    if duration_field.get("type")=="INT" and not float(seconds).is_integer():
        result["issues"].append("The mapped duration control needs whole seconds.")
    fps = constraints["fps"]
    if _number(fps) and fps>0:
        base,step = constraints["frame_base"],constraints["frame_step"]
        # Match the H3 workflow's upstream frame calculator; other graphs use the smallest admissible frame count.
        count = round(seconds*fps) if wf["adapter"]=="minimax-h3" and wf["mapping"].get("duration") else math.ceil(seconds*fps-1e-9)
        result["frames"] = int(max(1,base+math.ceil((count-base)/step)*step))
        if wf["mapping"].get("frames") or wf["adapter"]=="minimax-h3" and wf["mapping"].get("duration"):
            result["playback_seconds"] = result["frames"]/fps
    return result


def validate_shot(wf, shot, images, voices):
    if wf.get("issues"):
        return list(wf["issues"])
    issues = timing(wf,shot.get("seconds"))["issues"]
    constraints = limits(wf)
    for role,items in (("images",images),("audio",voices)):
        needed = constraints["min_"+role]
        allowed = constraints["max_"+role]
        if len(items)<needed or len(items)>allowed:
            issues.append(f"Workflow accepts {needed}–{allowed} {role} references; this shot has {len(items)}.")
    parameters = shot.get("workflowParameters", {})
    if not isinstance(parameters,dict):
        return issues+["Shot runtime parameters must be an object."]
    fields = {f["id"]:f for f in wf["fields"]}
    bound = {wf["mapping"].get(k) for k in ("positive","negative","duration","frames")}
    bound.update(wf["mapping"]["images"]+wf["mapping"]["audio"])
    for key,value in parameters.items():
        if key not in fields or key in bound or "parameter" not in fields[key]["roles"]:
            issues.append("Unknown or reserved runtime parameter "+key+".")
        elif (err:=_value_error(fields[key],value)):
            issues.append(err)
    return issues


def _validate_runtime_contracts(graph, keys, model_info):
    """Read-only verification against ComfyUI /object_info. Never mutates exported nodes."""
    contracts = {}
    for ident in keys:
        node = graph[ident]
        kind = node["class_type"]
        inputs = node["inputs"]
        for key, value in inputs.items():
            if _link(value):
                continue
            needs_check = (isinstance(value, str) and value.lower().endswith(MODEL_SUFFIXES)) or kind == "MinimaxH3LatentUpscaler3D"
            if not needs_check:
                continue
            if kind not in contracts:
                contracts[kind] = _contract(model_info(kind), kind)
            spec = contracts[kind].get(key)
            if not spec:
                continue
            options = spec.get("options")
            if options is not None:
                choices = [o["key"] if isinstance(o, dict) and "key" in o else o for o in options]
                if value not in choices:
                    raise ValueError(
                        f"Node {ident}.{key} ({value!r}) does not match ComfyUI's registered choices {choices}. "
                        "Fix and re-export the workflow in ComfyUI; AXDAR does not modify exported workflows."
                    )
            typ = spec.get("type")
            if (typ == "BOOLEAN" and not isinstance(value, bool)) or (typ in ("INT", "FLOAT") and not _number(value)):
                raise ValueError(
                    f"Node {ident}.{key} ({value!r}) has an incompatible type for {typ}. "
                    "Fix and re-export the workflow in ComfyUI; AXDAR does not modify exported workflows."
                )


def verify_strict_integrity(original_graph, runtime_graph, allowed_edits, allowed_added_loaders=()):
    """Guarantee zero deleted nodes, zero unauthorized additions, and zero unauthorized input mutations."""
    removed = sorted(set(original_graph) - set(runtime_graph))
    added = sorted(set(runtime_graph) - set(original_graph))
    extra = [nid for nid in added if nid not in allowed_added_loaders]
    if removed or extra:
        raise ValueError(
            f"Strict workflow integrity violation: nodes changed (added={extra}, removed={removed}). "
            "AXDAR must not add or remove workflow nodes."
        )
    for nid in added:
        if runtime_graph[nid].get("class_type") not in ("LoadImage", "LoadAudio"):
            raise ValueError(f"Strict workflow integrity violation: unexpected added node {nid}.")
    for ident, orig_node in original_graph.items():
        run_node = runtime_graph[ident]
        if run_node.get("class_type") != orig_node.get("class_type"):
            raise ValueError(f"Strict workflow integrity violation: node {ident} changed class_type.")
        orig_inputs = orig_node.get("inputs", {})
        run_inputs = run_node.get("inputs", {})
        for key in set(orig_inputs) | set(run_inputs):
            if (ident, key) in allowed_edits:
                continue
            if (key in orig_inputs) != (key in run_inputs) or orig_inputs.get(key) != run_inputs.get(key):
                raise ValueError(
                    f"Strict workflow integrity violation: unauthorized edit to {ident}.{key}. "
                    "AXDAR only injects mapped shot inputs."
                )


def runtime_graph(wf, shot, positive_prompt, images, voices, upload, prefix, *, model_info=None):
    """Strict slot-only binding: preserve all authored nodes and unmapped settings verbatim."""
    errors = validate_shot(wf,shot,images,voices)
    if not str(positive_prompt or "").strip():
        errors.append("The shot needs a positive prompt.")
    if errors:
        raise ValueError(" ".join(errors))
    original = wf["graph"]
    graph = copy.deepcopy(original)
    fields = {f["id"]:f for f in wf["fields"]}
    m = wf["mapping"]
    output = m["output"]
    allowed_edits = set()
    allowed_added_loaders = set()
    next_id = max((int(i) for i in graph if i.isdigit()), default=0) + 1

    def set_field(field_id, value):
        f = fields[field_id]
        graph[f["node"]]["inputs"][f["key"]] = value
        allowed_edits.add((f["node"], f["key"]))

    def media_loader(kind, slot, name):
        nonlocal next_id
        key = str(next_id)
        next_id += 1
        graph[key] = dict(class_type=kind, inputs={slot: upload(name)})
        allowed_added_loaders.add(key)
        return [key, 0]

    for key,value in {**m.get("parameters",{}),**shot.get("workflowParameters",{})}.items():
        set_field(key,value)
    set_field(m["positive"],positive_prompt)
    if m.get("negative"):
        set_field(m["negative"],shot.get("negativePrompt",shot.get("negative_prompt","")))
    if m.get("duration"):
        value = shot["seconds"]
        set_field(m["duration"],int(value) if fields[m["duration"]]["type"]=="INT" else value)
    if m.get("frames"):
        set_field(m["frames"],timing(wf,shot["seconds"])["frames"])
    if model_info:
        _validate_runtime_contracts(graph, _upstream(graph, [output]), model_info)
    for role,assets in (("images",list(images)),("audio",list(voices))):
        group,stem,kind,slot,cap = _H3_GROUPS[role]
        selected = [fields[key] for key in m[role]]
        # An explicitly deselected optional source must never leak its sample asset into a shot.
        selected.extend(f for f in fields.values() if f.get("media")==role and not f.get("socket") and f["id"] not in m[role])
        for index,field in enumerate(selected):
            inputs = graph[field["node"]]["inputs"]
            must_remain = sum(not f.get("bypass") for f in selected[index+1:])
            count = len(assets)-must_remain if field["id"] in m[role] else 0
            if field.get("group"):
                loaders = field.get("loaders", [])
                take = min(max(0, count), cap)
                for key in list(inputs):
                    if key.startswith(group + "."):
                        del inputs[key]
                        allowed_edits.add((field["node"], key))
                for i in range(take):
                    name = assets.pop(0)
                    slot_key = f"{group}.{stem}_{i}"
                    if i < len(loaders):
                        _, loader_id, loader_key = loaders[i]
                        graph[loader_id]["inputs"][loader_key] = upload(name)
                        allowed_edits.add((loader_id, loader_key))
                        inputs[slot_key] = [loader_id, 0]
                    else:
                        inputs[slot_key] = media_loader(kind, slot, name)
                    allowed_edits.add((field["node"], slot_key))
            elif count>0:
                if field.get("socket"):
                    raise ValueError(
                        f"Mapped socket {field['id']} has no existing loader node. "
                        "Wire it in ComfyUI and re-export; AXDAR does not add nodes."
                    )
                name = assets.pop(0)
                set_field(field["id"], upload(name))
            elif field.get("socket") or not field.get("consumers") or graph[field["node"]]["class_type"] not in ("LoadImage","LoadAudio"):
                if field["key"] in inputs:
                    inputs.pop(field["key"], None)
                    allowed_edits.add((field["node"], field["key"]))
            else:
                for dest,key in field["consumers"]:
                    if key in graph[dest]["inputs"]:
                        graph[dest]["inputs"].pop(key, None)
                        allowed_edits.add((dest, key))
    verify_strict_integrity(original, graph, allowed_edits, allowed_added_loaders)
    return graph


def public_profile(wf):
    return {key:wf[key] for key in ("version","name","adapter","mapping","fields","outputs","issues",
                                    "format","nodeCount","executable")}


def runtime_requirements(ctx, spec, wf):
    required = {}
    for _eid, shot, rules in spec["jobs"]:
        prompt_text = shot.get("frozenPrompt") or ctx["prompt"](shot, rules)
        runtime = runtime_graph(
            wf, shot, prompt_text,
            ctx["generation_images"](shot),
            ctx["generation_voices"](shot),
            str, "AXDAR/check",
        )
        for node in runtime.values():
            key = json.dumps(node, sort_keys=True)
            if key not in required:
                required[key] = node
    return {"graph": required}


class WorkflowStore:
    """Content-addressed original files and immutable mapping revisions; selection is an atomic pointer."""
    def __init__(self, directory):
        self.directory = Path(directory)

    @staticmethod
    def _write(path, data):
        path.parent.mkdir(parents=True,exist_ok=True)
        temp = path.with_name(uuid.uuid4().hex+".tmp")
        try:
            temp.write_bytes(data)
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)

    def _persist(self, name, raw, info, mapping, select, executable=None):
        editor = editor_graph(raw.decode("utf-8-sig"))
        if editor is not None and executable is None:
            version = _digest(_json([_digest(raw), "editor"]))
            wf = dict(version=version, name=name, adapter="UI workflow", mapping={}, fields=[],
                      outputs=[], issues=["Connect ComfyUI to prepare this original workflow before production."],
                      graph={})
        else:
            wf = inspect_workflow(name,executable or raw,info,mapping)
            wf["version"] = _digest(_json([_digest(raw),wf["version"]]))
        wf.update(format="ComfyUI UI JSON" if editor is not None else "ComfyUI API JSON",
                  nodeCount=len(editor if editor is not None else wf["graph"]),
                  executable=bool(wf["graph"]))
        source = _digest(raw)
        # Two differently formatted imports retain separate originals even if the graphs are equal.
        source_path = self.directory/"originals"/(source+".json")
        if not source_path.exists():
            self._write(source_path,raw)
        path = self.directory/"versions"/(wf["version"]+".json")
        if not path.exists():
            classes = {n["class_type"] for n in wf["graph"].values()}
            meta = {k:v for k,v in (info or {}).items() if k in classes}
            rec = dict(name=name,source=source,mapping=mapping,metadata=meta)
            if executable is not None:
                rec["executable"] = _digest(executable)
                self._write(self.directory/"executable"/(rec["executable"]+".json"),executable)
            self._write(path,_json(rec))
        if select and not wf["issues"]:
            self._write(self.directory/"active.json",_json(wf["version"]))
        return wf

    def import_workflow(self,name,raw_bytes,object_info=None,select=True):
        if not isinstance(raw_bytes,bytes):
            raise ValueError("Import the original workflow bytes.")
        return self._persist(name,raw_bytes,object_info,None,select)

    def _record(self,version):
        if not isinstance(version,str) or not re.fullmatch(r"[0-9a-f]{64}",version):
            raise ValueError("Unknown workflow version.")
        try:
            return json.loads((self.directory/"versions"/(version+".json")).read_bytes())
        except FileNotFoundError as exc:
            raise ValueError("Unknown workflow version.") from exc

    def original(self,version):
        record = self._record(version)
        data = (self.directory/"originals"/(record["source"]+".json")).read_bytes()
        if _digest(data)!=record["source"]:
            raise ValueError("The saved workflow original failed its checksum.")
        return data

    def get(self,version):
        rec = self._record(version)
        raw = self.original(version)
        editor = editor_graph(raw.decode("utf-8-sig"))
        executable = self._executable(rec)
        if editor is not None and executable is None:
            return dict(version=version,name=rec["name"],adapter="UI workflow",mapping={},fields=[],outputs=[],
                        issues=["Connect ComfyUI to prepare this original workflow before production."],
                        graph={},format="ComfyUI UI JSON",nodeCount=len(editor),executable=False)
        wf = inspect_workflow(rec["name"],executable or raw,rec["metadata"],rec["mapping"])
        wf.update(version=version,format="ComfyUI UI JSON" if editor is not None else "ComfyUI API JSON",
                  nodeCount=len(editor if editor is not None else wf["graph"]),executable=True)
        return wf

    def _executable(self, rec):
        if "executable" in rec:
            data = (self.directory/"executable"/(rec["executable"]+".json")).read_bytes()
            if _digest(data)!=rec["executable"]:
                raise ValueError("The attached executable graph failed its checksum.")
            return data
        return None

    def attach(self, version, raw):
        rec = self._record(version)
        original = self.original(version)
        editor = editor_graph(original.decode("utf-8-sig"))
        if not editor:
            raise ValueError("Select the original ComfyUI UI profile first.")
        graph = _parse(raw)
        shared = {key for key in editor if key in graph}
        if not shared or any(editor[key] != graph[key]["class_type"] for key in shared):
            raise ValueError("The API export does not match identifiable nodes in this UI workflow.")
        outputs = {item["id"] for item in _outputs(graph,{})}
        if not outputs & shared:
            raise ValueError("The API export does not include a video output from this UI workflow.")
        wf = self.get(version)
        return self._persist(rec["name"],original,{},wf["mapping"] or None,False,raw)

    def active(self):
        pointer = self.directory/"active.json"
        return self.get(json.loads(pointer.read_bytes())) if pointer.exists() else None

    def list(self):
        items = []
        for p in sorted((self.directory/"versions").glob("*.json"),key=lambda x:x.stat().st_mtime_ns,reverse=True):
            wf = self.get(p.stem)
            items.append(dict(name=wf["name"],version=wf["version"],ready=not wf["issues"]))
        return items

    def remap(self,version,mapping,object_info=None,select=True):
        rec = self._record(version)
        if not self.get(version)["executable"]:
            raise ValueError("Prepare the original workflow on the connected ComfyUI instance first.")
        return self._persist(rec["name"], self.original(version), rec["metadata"] if object_info is None else object_info,
                             mapping,select,self._executable(rec))

    def inspect_metadata(self,version,metadata):
        rec = self._record(version)
        if not self.get(version)["executable"]:
            raise ValueError("Prepare the original workflow on the connected ComfyUI instance first.")
        return self._persist(rec["name"],self.original(version),metadata,rec["mapping"],False,self._executable(rec))

    def select(self,version):
        wf = self.get(version)
        if wf["issues"]:
            raise ValueError("Review workflow mapping first. "+" ".join(wf["issues"]))
        self._write(self.directory/"active.json",_json(version))
        return wf
