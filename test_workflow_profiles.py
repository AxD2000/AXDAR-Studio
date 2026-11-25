import copy
import json
import tempfile
import unittest
from pathlib import Path

import workflow_profiles as profiles


ROOT = Path(__file__).resolve().parent


def node(kind, **inputs):
    return {"class_type":kind,"inputs":inputs}


def generic_graph():
    graph = {"t":node("MyText", positive_prompt="keep me", negative_prompt="negative"),
             "pic":node("LoadImage", image="unrelated.png"),
             "voice":node("LoadAudio", audio="unrelated.wav"),
             "gen":node("MyVideo", conditioning=["t",0], frame_count=49, portrait=["pic",0], speech=["voice",0]),
             "out":node("StudioMovieSink", video=["gen",0], format="mp4", fps=24,filename_prefix="original")}
    metadata = {"MyText":{"input":{"required":{"positive_prompt":["STRING"],"negative_prompt":["STRING"]}}},
                "MyVideo":{"input":{"required":{"conditioning":["CONDITIONING"],"portrait":["IMAGE"],
                                                 "frame_count":["INT",{"min":1,"max":97,"step":8}]},
                                    "optional":{"speech":["AUDIO"]}}},
                "StudioMovieSink":{"output_node":True}}
    return graph,metadata


class WorkflowProfiles(unittest.TestCase):
    @unittest.skipUnless((ROOT/"selected-workflow.json").exists(), "Supply the user H3 workflow for this check.")
    def test_real_studio_original_and_optional_h3_groups(self):
        raw = (ROOT/"selected-workflow.json").read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            store = profiles.WorkflowStore(tmp)
            wf = store.import_workflow("Studio H3",raw)
            self.assertEqual(wf["issues"],[])
            self.assertEqual(wf["mapping"]["positive"],"84.value")
            self.assertEqual(wf["mapping"]["duration"],"74.value")
            self.assertEqual(wf["output"],"141")
            copied = copy.deepcopy(wf["graph"])
            shot = {"seconds":5}
            uploaded = []
            def upload(name):
                uploaded.append(name)
                return "staged/"+name
            g = profiles.runtime_graph(wf,shot,"new words",["left.png","right.png"],["line.wav"],upload,"AXDAR/take")
            inputs = g["56"]["inputs"]
            for i,name in enumerate(["left.png","right.png"]):
                self.assertEqual(g[inputs[f"ref_images.ref_image_{i}"][0]]["inputs"]["image"],"staged/"+name)
            self.assertEqual(g[inputs["ref_audios.ref_audio_0"][0]]["inputs"]["audio"],"staged/line.wav")
            self.assertEqual(g["74"]["inputs"]["value"],5)
            self.assertEqual(g["84"]["inputs"]["value"],"new words")
            self.assertEqual(g["141"]["inputs"].get("filename_prefix"),wf["graph"]["141"]["inputs"].get("filename_prefix"))
            self.assertEqual(uploaded,["left.png","right.png","line.wav"])
            self.assertTrue(set(wf["graph"].keys()).issubset(set(g.keys())))
            self.assertEqual(profiles.timing(wf,5)["frames"],124)
            self.assertEqual(profiles.limits(wf)["trained_frame_range"],[124,362])
            no_refs = profiles.runtime_graph(wf,shot,"quiet",[],[],upload,"p")
            self.assertFalse(any(k.startswith(("ref_images.","ref_audios.")) for k in no_refs["56"]["inputs"]))
            self.assertEqual(set(no_refs.keys()),set(wf["graph"].keys()))
            self.assertEqual(copied,wf["graph"])
            self.assertEqual(store.original(wf["version"]),raw)
            self.assertEqual(store.active()["version"],wf["version"])
            too_long = profiles.timing(wf,180)
            self.assertTrue(too_long["issues"])
            self.assertEqual(sum(too_long["split"]),180)
            with self.assertRaisesRegex(ValueError,"images references"):
                profiles.runtime_graph(wf,shot,"x",["a.png"]*10,[],upload,"p")

    @unittest.skipUnless((ROOT/"selected-workflow.json").exists(), "Supply the user H3 workflow for this check.")
    def test_h3_changed_expression_or_playback_rate_never_guesses_seconds(self):
        raw = json.loads((ROOT/"selected-workflow.json").read_bytes())["graph"]
        raw["141"]["inputs"]["frame_rate"] = 30
        wf = profiles.inspect_workflow("30fps",raw)
        self.assertEqual(wf["issues"],[])
        self.assertEqual(wf["mapping"]["frames"],"56.length")
        self.assertEqual(wf["mapping"]["fps"],30)
        g = profiles.runtime_graph(wf,{"seconds":5},"words",[],[],str,"p")
        self.assertEqual(g["56"]["inputs"]["length"],158)
        self.assertIn("75",g)
        raw["141"]["inputs"]["frame_rate"] = 24
        raw["75"]["inputs"]["expression"] = "a * 3"
        self.assertEqual(profiles.inspect_workflow("changed calculator",raw)["mapping"]["frames"],"56.length")

    def test_editor_export_and_actual_incomplete_api_export_report_action(self):
        with self.assertRaisesRegex(ValueError,"API format"):
            profiles.inspect_workflow("editor",{"nodes":[{"type":"LoadImage"}],"links":[]})
        if not (ROOT/"workflow.json").exists():
            return
        with self.assertRaisesRegex(ValueError,"missing class_type"):
            profiles.inspect_workflow("incomplete", (ROOT/"workflow.json").read_bytes())

    def test_generic_custom_output_negative_optional_and_required_references(self):
        graph,metadata = generic_graph()
        wf = profiles.inspect_workflow("custom",graph,metadata)
        self.assertEqual(wf["issues"],[])
        self.assertEqual(wf["adapter"],"api")
        self.assertEqual(profiles.limits(wf)["max_images"],1)
        self.assertEqual(profiles.limits(wf)["min_audio"],0)
        self.assertTrue(profiles.validate_shot(wf,{"seconds":3},[],[]))
        shot = dict(seconds=3,negativePrompt="changed negative")
        rendered = profiles.runtime_graph(wf,shot,"changed positive",["shot.png"],[],lambda n:"new/"+n,"new/shot")
        self.assertEqual(rendered["t"]["inputs"],dict(positive_prompt="changed positive",negative_prompt="changed negative"))
        self.assertEqual(rendered["pic"]["inputs"]["image"],"new/shot.png")
        self.assertEqual(rendered["voice"],graph["voice"])
        self.assertEqual(set(rendered.keys()),set(graph.keys()))
        self.assertEqual(rendered["out"]["inputs"]["filename_prefix"],"original")
        self.assertNotIn("speech",rendered["gen"]["inputs"])
        self.assertEqual(rendered["gen"]["inputs"]["frame_count"],73)
        self.assertEqual(profiles.timing(wf,7)["split"],[4,3])
        with self.assertRaisesRegex(ValueError,"maximum"):
            profiles.runtime_graph(wf,{"seconds":7},"x",["shot.png"],[],str,"p")
        self.assertEqual(graph["pic"]["inputs"]["image"],"unrelated.png")
        no_voice = profiles.inspect_workflow("custom",graph,metadata,dict(wf["mapping"],audio=[]))
        self.assertEqual(no_voice["issues"],[])
        no_voice_rendered = profiles.runtime_graph(no_voice,shot,"p",["right.png"],[],str,"p")
        self.assertEqual(no_voice_rendered["voice"],graph["voice"])
        self.assertNotIn("speech",no_voice_rendered["gen"]["inputs"])

    def test_missing_nonprimitive_and_required_widget(self):
        graph,metadata = generic_graph()
        metadata["MyVideo"]["input"]["required"].update(model=["MODEL"],guidance=["FLOAT",{"min":0,"max":10}])
        wf = profiles.inspect_workflow("custom",graph,metadata)
        self.assertIn("gen.model"," ".join(wf["issues"]))
        self.assertIn("gen.guidance"," ".join(wf["issues"]))
        bad = profiles.inspect_workflow("custom",graph,metadata,dict(parameters={"gen.guidance":11,"gen.model":"some.safetensors"}))
        self.assertIn("requires a connected MODEL"," ".join(bad["issues"]))
        self.assertIn("allowed range"," ".join(bad["issues"]))
        graph["model"] = node("ModelLoader",name="model.safetensors")
        graph["gen"]["inputs"]["model"] = ["model",0]
        fixed = profiles.inspect_workflow("custom",graph,metadata,dict(parameters={"gen.guidance":4}))
        self.assertEqual(fixed["issues"],[])
        shot = {"seconds":3,"workflowParameters":{"gen.guidance":6}}
        out = profiles.runtime_graph(fixed,shot,"words",["ref.png"],[],str,"prefix")
        self.assertEqual(out["gen"]["inputs"]["guidance"],6)

    def test_unwired_image_socket_is_rejected_without_synthesizing_nodes(self):
        graph,metadata = generic_graph()
        del graph["gen"]["inputs"]["portrait"],graph["pic"]
        wf = profiles.inspect_workflow("custom",graph,metadata)
        self.assertEqual(wf["mapping"]["images"],[])
        mapped = profiles.inspect_workflow("custom",graph,metadata,{"images":["gen.portrait"]})
        self.assertTrue(any("does not add nodes" in issue for issue in mapped["issues"]))

    def test_strict_mode_preserves_extra_nodes_and_rejects_mismatched_contracts(self):
        graph,metadata = generic_graph()
        graph["preview"] = node("PreviewImage", images=["gen",0])
        graph["up"] = node("MinimaxH3LatentUpscaler3D", latent=["gen",0], device=True, precision="cuda")
        wf = profiles.inspect_workflow("strict",graph,metadata)
        self.assertEqual(wf["issues"],[])
        out = profiles.runtime_graph(wf,{"seconds":2},"text",["a.png"],[],str,"prefix")
        self.assertIn("preview",out)
        self.assertEqual(out["preview"],graph["preview"])
        self.assertEqual(out["up"],graph["up"])
        upscaler_info = lambda kind: {"MinimaxH3LatentUpscaler3D":{"input":{"required":{
            "device":["COMBO",{"options":["cuda","rocm","cpu"]}],
            "precision":["COMBO",{"options":["fp32","fp16","bf16"]}],
        }}}} if kind=="MinimaxH3LatentUpscaler3D" else {}
        graph["gen"]["inputs"]["up"] = ["up",0]
        wf_up = profiles.inspect_workflow("strict-up",graph,metadata)
        with self.assertRaisesRegex(ValueError,"does not modify exported workflows"):
            profiles.runtime_graph(wf_up,{"seconds":2},"text",["a.png"],[],str,"prefix",model_info=upscaler_info)
        with self.assertRaisesRegex(ValueError,"Strict workflow integrity violation"):
            tampered = copy.deepcopy(graph)
            tampered["out"]["inputs"]["fps"] = 60
            profiles.verify_strict_integrity(graph, tampered, set())

    def test_ambiguous_text_output_and_source_bytes_survive_remapping(self):
        graph = {"a":node("CustomWords",literal_a="first",literal_b="second"),
                 "b":node("CustomVideo",text=["a",0],seconds=8),
                 "z":node("OpaqueSaver",media=["b",0])}
        raw = b"\xef\xbb\xbf"+json.dumps({"prompt":graph},indent=3).encode()
        with tempfile.TemporaryDirectory() as tmp:
            store = profiles.WorkflowStore(tmp)
            prev = store.import_workflow("confirmed",json.dumps({"t":node("Words",positive_prompt="scene"),
                                                                "g":node("Video",text=["t",0],seconds=4),
                                                                "o":node("VHS_VideoCombine",images=["g",0],format="video/h264-mp4")}).encode())
            draft = store.import_workflow("custom.json",raw)
            self.assertTrue(draft["issues"])
            self.assertEqual(store.active()["version"],prev["version"])
            with self.assertRaisesRegex(ValueError,"Review workflow"):
                store.select(draft["version"])
            approved = store.remap(draft["version"],{"output":"z","positive":"a.literal_a","negative":"a.literal_b"})
            self.assertEqual(approved["issues"],[])
            new = store.remap(approved["version"],dict(approved["mapping"],positive="a.literal_b",negative="a.literal_a"))
            self.assertNotEqual(new["version"],approved["version"])
            self.assertEqual(store.original(new["version"]),raw)
            self.assertEqual(store.get(approved["version"])["mapping"]["positive"],"a.literal_a")
            self.assertEqual(store.select(approved["version"]),store.active())
            self.assertTrue(all(row.keys()>={"version","name","ready"} for row in store.list()))

    def test_image_only_graph_rejected_and_metadata_does_not_switch_selection(self):
        graph,metadata = generic_graph()
        graph["out"]=node("SaveImage",images=["gen",0],filename_prefix="x")
        wf = profiles.inspect_workflow("still",graph,metadata,dict(output="out"))
        self.assertIn("MP4"," ".join(wf["issues"]))
        graph,metadata = generic_graph()
        with tempfile.TemporaryDirectory() as tmp:
            store = profiles.WorkflowStore(tmp)
            current = store.import_workflow("confirmed",json.dumps({"t":node("Words",positive_prompt="scene"),
                                                                "g":node("Video",text=["t",0],seconds=4),
                                                                "o":node("VHS_VideoCombine",images=["g",0],format="video/h264-mp4")}).encode())
            pending = store.import_workflow("other",json.dumps(graph).encode())
            self.assertTrue(pending["issues"])
            inspected = store.inspect_metadata(pending["version"],metadata)
            self.assertFalse(inspected["issues"])
            self.assertEqual(store.active()["version"],current["version"])
            self.assertEqual(store.select(inspected["version"]),store.active())

    def test_ui_to_api_local_conversion_and_deno_minimax_h3(self):
        ui = {
            "nodes": [
                {"id": 1, "type": "MarkdownNote", "mode": 0, "widgets_values": ["note"]},
                {"id": 10, "type": "UNETLoader", "mode": 0, "inputs": [{"name": "unet_name", "type": "COMBO", "widget": {"name": "unet_name"}, "link": None}, {"name": "weight_dtype", "type": "COMBO", "widget": {"name": "weight_dtype"}, "link": None}], "outputs": [{"name": "MODEL", "type": "MODEL", "links": [1]}], "widgets_values": ["model.safetensors", "default"]},
                {"id": 11, "type": "LoraLoaderModelOnly", "mode": 0, "inputs": [{"name": "model", "type": "MODEL", "link": 1}], "outputs": [{"name": "MODEL", "type": "MODEL", "links": [2]}], "widgets_values": ["pixar.safetensors", 0.8]},
                {"id": 12, "type": "LoraLoaderModelOnly", "mode": 4, "inputs": [{"name": "model", "type": "MODEL", "link": 2}], "outputs": [{"name": "MODEL", "type": "MODEL", "links": [3]}], "widgets_values": ["bypassed.safetensors", 0.5]},
                {"id": 13, "type": "RandomNoise", "mode": 0, "inputs": [{"name": "noise_seed", "type": "INT", "widget": {"name": "noise_seed"}, "link": None}], "outputs": [{"name": "NOISE", "type": "NOISE", "links": [4]}], "widgets_values": [42, "randomize"]},
                {"id": 14, "type": "PrimitiveFloat", "mode": 0, "title": "Duration", "inputs": [{"name": "value", "type": "FLOAT", "widget": {"name": "value"}, "link": None}], "outputs": [{"name": "FLOAT", "type": "FLOAT", "links": [5]}], "widgets_values": [5]},
                {"id": 15, "type": "ComfyMathExpression", "mode": 0, "inputs": [{"name": "values.a", "type": "FLOAT", "link": 5}, {"name": "expression", "type": "STRING", "widget": {"name": "expression"}, "link": None}], "outputs": [{"name": "FLOAT", "type": "FLOAT", "links": []}, {"name": "INT", "type": "INT", "links": [6]}], "widgets_values": ["max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17"]},
                {"id": 16, "type": "DenoMiniMaxH3ReferenceImageLoader", "mode": 0, "inputs": [{"name": "image_paths", "type": "STRING", "widget": {"name": "image_paths"}, "link": None}], "outputs": [{"name": "ref_images", "type": "DENO_MINIMAX_H3_REFERENCE_IMAGES", "links": [7]}], "widgets_values": ["", ""]},
                {"id": 17, "type": "DenoMiniMaxH3ReferenceToVideo", "mode": 0, "inputs": [{"name": "ref_images", "type": "DENO_MINIMAX_H3_REFERENCE_IMAGES", "link": 7}, {"name": "prompt", "type": "STRING", "widget": {"name": "prompt"}, "link": None}, {"name": "length", "type": "INT", "widget": {"name": "length"}, "link": 6}], "outputs": [{"name": "positive", "type": "CONDITIONING", "links": [8]}], "widgets_values": ["", 124]},
                {"id": 18, "type": "SamplerCustomAdvanced", "mode": 0, "inputs": [{"name": "model", "type": "MODEL", "link": 3}, {"name": "noise", "type": "NOISE", "link": 4}, {"name": "cond", "type": "CONDITIONING", "link": 8}], "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [9, 10]}]},
                {"id": 19, "type": "VHS_VideoCombine", "mode": 0, "title": "Save Base Master", "inputs": [{"name": "images", "type": "IMAGE", "link": 9}], "outputs": [], "widgets_values": {"frame_rate": 24, "filename_prefix": "Base", "format": "video/h264-mp4", "save_output": True}},
                {"id": 20, "type": "ImageScale", "mode": 0, "inputs": [{"name": "image", "type": "IMAGE", "link": 10}], "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [11]}], "widgets_values": ["lanczos", 1920, 1080, "disabled"]},
                {"id": 21, "type": "VHS_VideoCombine", "mode": 0, "title": "Save Final Upscaled Video", "inputs": [{"name": "images", "type": "IMAGE", "link": 11}], "outputs": [], "widgets_values": {"frame_rate": 24, "filename_prefix": "Final_Upscaled", "format": "video/h264-mp4", "save_output": True}},
            ],
            "links": [
                [1, 10, 0, 11, 0, "MODEL"],
                [2, 11, 0, 12, 0, "MODEL"],
                [3, 12, 0, 18, 0, "MODEL"],
                [4, 13, 0, 18, 1, "NOISE"],
                [5, 14, 0, 15, 0, "FLOAT"],
                [6, 15, 1, 17, 2, "INT"],
                [7, 16, 0, 17, 0, "DENO_MINIMAX_H3_REFERENCE_IMAGES"],
                [8, 17, 0, 18, 2, "CONDITIONING"],
                [9, 18, 0, 19, 0, "IMAGE"],
                [10, 18, 0, 20, 0, "IMAGE"],
                [11, 20, 0, 21, 0, "IMAGE"],
            ],
        }
        raw = json.dumps(ui).encode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            store = profiles.WorkflowStore(tmp)
            wf = store.import_workflow("deno_v11.json", raw)
            self.assertTrue(wf["executable"])
            self.assertEqual(wf["format"], "ComfyUI UI JSON")
            self.assertEqual(wf["issues"], [])
            self.assertEqual(wf["adapter"], "minimax-h3")
            self.assertEqual(wf["output"], "21")
            self.assertEqual(wf["mapping"]["positive"], "17.prompt")
            self.assertEqual(wf["mapping"]["duration"], "14.value")
            self.assertEqual(wf["mapping"]["images"], ["16.image_paths"])
            self.assertEqual(wf["mapping"]["audio"], ["17.ref_audios"])
            self.assertEqual(wf["graph"]["18"]["inputs"]["model"], ["11", 0])
            self.assertEqual(wf["graph"]["13"]["inputs"], {"noise_seed": 42})
            self.assertEqual(wf["graph"]["20"]["inputs"]["width"], 1920)
            self.assertEqual(store.active()["version"], wf["version"])
            with_refs = profiles.runtime_graph(wf, {"seconds": 5}, "pixar scene", ["a.png", "b.png"], ["v.wav"], lambda n: "u/" + n, "p")
            self.assertEqual(with_refs["16"]["inputs"]["image_paths"], "u/a.png\nu/b.png")
            self.assertEqual(with_refs["17"]["inputs"]["prompt"], "pixar scene")
            self.assertEqual(with_refs[with_refs["17"]["inputs"]["ref_audios.ref_audio_0"][0]]["inputs"]["audio"], "u/v.wav")
            no_refs = profiles.runtime_graph(wf, {"seconds": 5}, "text only", [], [], lambda n: "u/" + n, "p")
            self.assertNotIn("ref_images", no_refs["17"]["inputs"])


if __name__=="__main__":
    unittest.main()
