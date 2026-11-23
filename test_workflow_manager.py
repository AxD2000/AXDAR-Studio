"""Workflow Manager contracts with a disposable ComfyUI/SSH fixture."""
import io
import json
import runpy
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import direct_session
from test_production import movie
from workflow_gate import verify_first_shot
import editor_media
import production
import provision
import take_quality
import workflow_install_remote as remote
import workflow_manager
import workflow_profiles


def node(kind, **inputs):
    return {"class_type":kind,"inputs":inputs}


class WorkflowManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/"production").mkdir()
        self.graph={
            "10":node("CLIPTextEncode",text="Original"),
            "11":node("PrimitiveFloat",value=5),
            "56":node("MiniMaxH3ReferenceToVideo",length=124,conditioning=["10",0]),
            "120":node("FirstSampler",latent=["56",0]),
            "121":node("SecondSampler",latent=["120",0]),
            "135":node("VHS_VideoCombine",images=["120",0],format="video/h264-mp4",save_output=False),
            "141":node("VHS_VideoCombine",images=["121",0],format="video/h264-mp4",frame_rate=24),
        }

    def test_ui_capture_preserves_original_and_required_upstream(self):
        store=workflow_profiles.WorkflowStore(self.root/"workflows")
        original=json.dumps({"nodes":[{"id":int(k),"type":v["class_type"]} for k,v in self.graph.items()]},
                            indent=2).encode()+b"\n\n"
        profile=store.import_workflow("Original",original)
        self.assertFalse(profile["executable"])
        self.assertEqual(profile["nodeCount"],7)
        self.assertIsNone(store.active())
        with self.assertRaisesRegex(ValueError,"prepare"):
            store.select(profile["version"])
        wrong=dict(self.graph, **{"141":node("SaveImage",images=["121",0])})
        with self.assertRaisesRegex(ValueError,"does not match"):
            store.attach(profile["version"],json.dumps(wrong).encode())
        capture=store.attach(profile["version"],json.dumps(self.graph).encode())
        self.assertEqual(store.original(capture["version"]),original)
        self.assertTrue(capture["executable"])
        self.assertEqual(capture["output"],"141")
        self.assertEqual(workflow_profiles._upstream(capture["graph"],["141"]),{"141","10","56","120","121"})
        direct=store.import_workflow("API",json.dumps(self.graph).encode())
        self.assertEqual(direct["format"],"ComfyUI API JSON")

    def test_readiness_checks_missing_and_incompatible_assets(self):
        graph={"1":node("Loader",model="x.safetensors"),
               "2":node("A missing class",model="unknown.gguf")}
        info={"Loader":{"input":{"required":{"model":[["other.safetensors"]]}}}}
        result=remote.scan(graph,info,lambda name:name=="x.safetensors")
        self.assertEqual(result["nodes"],["A missing class"])
        self.assertEqual({m["name"]:m["status"] for m in result["models"]},
                         {"x.safetensors":"Incompatible","unknown.gguf":"Requires manual action"})

    def test_verified_model_source_matches_its_known_comfy_subfolder(self):
        graph={"1":node("VAELoader",vae_name="Minimax/minimax_h3_video_vae_fp16.safetensors")}
        result=remote.scan(graph,{})
        model=result["models"][0]
        self.assertEqual(model["status"],"Missing")
        self.assertTrue(model["verified"])
        self.assertEqual(model["source"]["repo"],"Comfy-Org/MiniMax-H3")

    def test_install_checks_actual_registration_and_runs_confirmed_script(self):
        (self.root/"install.sh").write_text("printf success","utf-8")
        fixture={"1":node("Lora Loader Stack (rgthree)")}
        installed=set()
        verified=False
        def endpoint(url, **_):
            if url.endswith("/queue"):
                data={"queue_running":[],"queue_pending":[]}
            else:
                data={key:{} for key in installed} if verified else {}
            return io.BytesIO(json.dumps(data).encode())
        def command(argv, **_):
            nonlocal verified
            if argv[:2]==["supervisorctl","restart"]:
                verified=True
            return subprocess.CompletedProcess(argv,0)
        def install(root,missing):
            self.assertEqual(root,self.root)
            installed.update(missing)
            return missing
        config={"port":18188,"graph":fixture,"mode":"install"}
        with patch.object(remote,"root_path",return_value=self.root),patch.object(remote,"urlopen",side_effect=endpoint), \
             patch.object(remote.subprocess,"run",side_effect=command) as run,patch.object(provision,"install_nodes",side_effect=install):
            self.assertEqual(remote.execute(self.root,config),0)
            self.assertEqual(run.call_args_list[0].args[0][0],"bash")
            self.assertEqual(run.call_args_list[0].args[0][:2],["bash","-n"])
            self.assertEqual(run.call_args_list[2].args[0],["supervisorctl","restart","comfyui"])
            verified=False
            run.side_effect=lambda args,**_kw:subprocess.CompletedProcess(args,0)
            self.assertEqual(remote.execute(self.root,config),2)
        self.assertEqual(production.read(self.root/"stage.json")["report"]["nodes"],["Lora Loader Stack (rgthree)"])

    def test_sage_library_is_checked_and_repaired_independently_of_node_registration(self):
        name="MiniMaxH3MemoryEfficientSageAttentionPatch"
        graph={"1":node(name)}
        ready=False
        def endpoint(url, **_):
            payload={"queue_running":[],"queue_pending":[]} if url.endswith("/queue") else {name:{}}
            return io.BytesIO(json.dumps(payload).encode())
        def build(root):
            nonlocal ready
            self.assertEqual(root,self.root)
            ready=True
        config={"port":18188,"graph":graph}
        with patch.object(remote,"root_path",return_value=self.root), \
             patch.object(remote,"urlopen",side_effect=endpoint), \
             patch.object(provision,"sage_ready",side_effect=lambda:ready), \
             patch.object(provision,"install_sage",side_effect=build) as install, \
             patch.object(remote.subprocess,"run") as command:
            self.assertEqual(remote.execute(self.root,dict(config,mode="scan")),2)
            report=production.read(self.root/"stage.json")["report"]
            self.assertEqual((report["nodes"],report["sageAttention"]),([],"Missing"))
            install.assert_not_called()
            self.assertEqual(remote.execute(self.root,dict(config,mode="install")),0)
            install.assert_called_once_with(self.root)
            command.assert_called_once_with(["supervisorctl","restart","comfyui"],check=True,timeout=90)
        self.assertEqual(production.read(self.root/"stage.json")["report"]["sageAttention"],"Installed")

    def test_script_token_filter_keeps_hf_hub_function_names(self):
        script="from huggingface_hub import hf_hub_download"
        self.assertEqual(remote.redact(script),script)
        self.assertEqual(remote.redact("hf_ABCDEFGH12345678"),"[REDACTED]")

    def test_invalid_bash_reports_exact_exit_code_before_script_or_installs(self):
        (self.root/"install.sh").write_text("echo unsafe if lint fails\\nfi","utf-8")
        fixture={"1":node("VHS_VideoCombine")}
        def endpoint(url,**_):
            return io.BytesIO(b'{"queue_running":[],"queue_pending":[]}' if url.endswith("/queue") else b"{}")
        with patch.object(remote,"root_path",return_value=self.root),patch.object(remote,"urlopen",side_effect=endpoint), \
             patch.object(remote.subprocess,"run",return_value=subprocess.CompletedProcess([],2)) as run, \
             patch.object(provision,"install_nodes") as install:
            result=remote.execute(self.root,{"port":18188,"graph":fixture,"mode":"install"})
        self.assertEqual(result,1)
        self.assertEqual(production.read(self.root/"stage.json")["bashExitCode"],2)
        self.assertEqual(run.call_count,1)
        self.assertEqual(run.call_args.args[0][:2],["bash","-n"])
        install.assert_not_called()

    def manager(self):
        store=workflow_profiles.WorkflowStore(self.root/"profiles")
        wf=store.import_workflow("API",json.dumps(self.graph).encode())
        connection=Mock()
        connection.target.return_value={"id":42,"host":"fixture","port":2222,"rate":0.7,"comfy_port":18188}
        connection.ssh_target.return_value=connection.target.return_value
        ctx={"controller":SimpleNamespace(connection=connection),"BASE":Path.cwd(),"DATA":self.root,
             "actions":SimpleNamespace(store=store),"production_busy":lambda:False,
             "running":False,"LOCK":threading.RLock()}
        return workflow_manager.WorkflowManager(ctx),wf["version"]

    def test_ui_json_is_converted_automatically_on_the_selected_connection(self):
        manager,_=self.manager()
        store=manager.ctx["actions"].store
        self.graph["10"]["_meta"]={"title":"Positive Prompt"}
        ui={"nodes":[{"id":int(k),"type":v["class_type"]} for k,v in self.graph.items()]}
        original=json.dumps(ui,indent=2).encode()+b"\n"
        profile=store.import_workflow("Only UI JSON",original)
        manager.connection.snapshot.return_value={"url":"http://127.0.0.1:18799"}
        manager.ctx["active_workflow"]=None
        converter=Mock(return_value=self.graph)
        manager.ctx["json_api"]=converter
        def workflow(action,obj):
            self.assertEqual(action,"select")
            manager.ctx["active_workflow"]=store.select(obj["version"])
        manager.ctx["actions"].workflow=workflow
        with patch.object(manager,"action",wraps=manager.action) as operation,patch.object(manager,"launch"):
            capture=manager.prepare(profile["version"])
        converted=capture["profile"]["version"]
        converter.assert_called_once_with("http://127.0.0.1:18799","/workflow/convert",ui)
        self.assertEqual(capture["active"],converted)
        self.assertEqual(store.get(converted)["graph"],self.graph)
        self.assertEqual(store.original(converted),original)
        operation.assert_called_once_with({"action":"scan","version":converted})

    def test_first_shot_gate_detects_lora_error_even_with_successful_mp4(self):
        graph={"1":node("DocumentedFixture"),"141":node("VHS_VideoCombine",audio=["1",0],images=["1",0])}
        wf=dict(name="Fixture",output="141",graph=graph)
        shot=dict(id="shot-one",revision="approved",seconds=2,prompt="Shot prompt")
        for mismatch in (True,False):
            sent=[]
            rows=[{"t":"2026-11-22T10:01:00","m":"Old unrelated ERROR lora incompatible shape\n"}]
            def api(_url,path,obj=None):
                if path=="/queue":return {"queue_running":[],"queue_pending":[]}
                if path=="/internal/logs/raw":return {"entries":list(rows)}
                if path=="/prompt":
                    sent.append(obj["prompt"])
                    rows.append({"t":"2026-11-22T10:01:01",
                                 "m":"ERROR lora blocks weight shape is invalid for input" if mismatch else "Sampling done"})
                    return {"prompt_id":"ticket"}
                if path=="/history/ticket":
                    return {"ticket":{"status":{"status_str":"success","completed":True},"outputs":{"141":{}}}}
                raise AssertionError(path)
            def download(_url,dest,history,job):
                self.assertEqual(job["output_node"],"141")
                (dest/"video.part").write_bytes(movie(2))
            ctx=dict(DATA=self.root,json_api=api,download_output=download,prepare_shot=lambda *_:graph,
                     prompt=lambda s,_:s["prompt"],technical_qc=lambda *_:{"technical":"passed","audioStreams":1})
            batch=uuid.uuid4().hex
            row=[batch,shot,[]]
            with patch("workflow_gate.editor_media.tool"):
                if mismatch:
                    with self.assertRaisesRegex(ValueError,"LoRA weight mismatch"):
                        verify_first_shot(ctx,"http://fixture",time.time()+60,batch,wf,row)
                    with self.assertRaisesRegex(ValueError,"LoRA weight mismatch"):
                        verify_first_shot(ctx,"http://fixture",time.time()+60,batch,wf,row)
                else:
                    self.assertEqual(verify_first_shot(ctx,"http://fixture",time.time()+60,batch,wf,row),"done")
            [attempt]=(self.root/batch/shot["id"]).glob("*/job.json")
            self.assertEqual(production.read(attempt)["status"],"error" if mismatch else "done")
            self.assertEqual((attempt.parent/"video.mp4").is_file(),not mismatch)
            self.assertEqual(sent,[graph])
            self.assertEqual(wf["graph"],graph)

    def test_direct_batch_waits_for_first_shot_workflow_verification(self):
        eid=str(uuid.uuid4())
        jobs=[[eid,dict(id=str(uuid.uuid4()),revision="approved",seconds=2,prompt="Original"),[]]]
        ctx={"DATA":self.root,"LOCK":threading.RLock(),"json_api":Mock(),"prepare_shot":Mock(),
             "download_output":Mock(),"take_quality":take_quality,"running":True}
        wf=dict(version="a"*64,name="Fixture",output="141",format="ComfyUI UI JSON",graph={"141":node("Out")})
        view=dict(end=time.time()+60,episode=eid,submitted=1,phase="Preparing ComfyUI",completed=0,failed=0)
        session=direct_session.DirectSession(ctx)
        session.start(view,jobs,wf,None,"http://fixture")
        session.plan["validated"]=True
        with patch.object(direct_session,"verify_first_shot",side_effect=ValueError("LoRA mismatch")), patch.object(production,"render") as batch:
            session.run("http://fixture",wf)
        batch.assert_not_called()
        self.assertEqual((view["phase"],view["error"],session.plan["index"]),("Needs attention","LoRA mismatch",0))

    def test_ui_batch_submits_each_shots_prompt_and_only_its_references(self):
        graph=dict(self.graph)
        linux_vae="Minimax/minimax_h3_video_vae_fp16.safetensors"
        graph["56"]=node("MiniMaxH3ReferenceToVideo",length=124,prompt=["84",0],vae=["65",0],
                         **{"ref_images.ref_image_0":["169",0],
                            "ref_images.ref_image_1":["170",0],
                            "ref_audios.ref_audio_0":["171",0],
                            "ref_audios.ref_audio_1":["172",0]})
        graph["65"]=node("VAELoader",vae_name=linux_vae)
        graph["84"]=node("PrimitiveStringMultiline",value="Template text")
        graph["169"]=node("LoadImage",image="template-only-0.png")
        graph["170"]=node("LoadImage",image="template-only-1.png")
        graph["171"]=node("LoadAudio",audio="template-only-0.wav")
        graph["172"]=node("LoadAudio",audio="template-only-1.wav")
        upscaler="MinimaxH3LatentUpscaler3D"
        graph["124"]=node(upscaler,latent=["121",0],device="cuda",precision="bf16",
                          enable_temporal_chunking=True,force_unload=True)
        graph["141"]=node("VHS_VideoCombine",images=["124",0],frame_rate=24,format="video/h264-mp4")
        original={"nodes":[{"id":int(k),"type":n["class_type"]} for k,n in graph.items()]}
        store=workflow_profiles.WorkflowStore(self.root/"upscaler-workflow")
        saved=store.import_workflow("H3 fixture",json.dumps(original).encode())
        captured=store.attach(saved["version"],json.dumps(graph).encode())
        wf=store.get(captured["version"])
        self.assertEqual(wf["mapping"]["positive"],"84.value")
        self.assertFalse(wf["issues"])
        eid=uuid.uuid4().hex
        jobs=[[eid, dict(id="one",revision="approved",seconds=2,frozenPrompt="Tom and Jack talk.",
                        image="Tom.png",refs=["Jack.png","Tom.wav","Jack.wav"]), []],
              [eid, dict(id="two",revision="approved",seconds=2,frozenPrompt="Jack walks silently.",
                        image="Jack.png",refs=[]), []]]
        ctx=runpy.run_path("studio.pyw")["prepare_shot"].__globals__
        ctx["DATA"]=self.root
        ctx["running"]=True
        ctx["files"]={}
        for name in ("Tom.png","Jack.png","Tom.wav","Jack.wav"):
            path=self.root/name
            path.write_text("fixture",encoding="utf-8")
            ctx["files"][name]=path
        uploaded=[]
        def upload(_url,path,_filename):
            uploaded.append(path.name)
            return path.name
        ctx["upload"]=upload
        nodes={n["class_type"]:{} for n in graph.values()}
        nodes.update(LoadAudio={},VAELoader={"input":{"required":{"vae_name":[[linux_vae],{}]}}})
        nodes[upscaler]={"input":{"required":{
            "device":["COMBO",{"default":"cuda","options":["cuda","rocm","cpu"]}],
            "precision":["COMBO",{"default":"fp16","options":["fp32","fp16","bf16"]}],
            "enable_temporal_chunking":["BOOLEAN",{"default":True}],
            "force_unload":["BOOLEAN",{"default":True}],
        }}}
        prompts=[]
        def api(_url,path,body=None):
            if path=="/object_info":return nodes
            if path.startswith("/object_info/"):
                kind=path.rsplit("/",1)[-1]
                return {kind:nodes[kind]}
            if path=="/queue":return {"queue_running":[],"queue_pending":[]}
            if path=="/internal/logs/raw":return {"entries":[{"t":"2026-11-23T01:00:00","m":"Fixture log"}]}
            if path=="/prompt":
                prompts.append(body["prompt"])
                return {"prompt_id":str(len(prompts))}
            if path.startswith("/history/"):
                ticket=path.rsplit("/",1)[-1]
                return {ticket:{"status":{"status_str":"success","completed":True},"outputs":{}}}
            raise AssertionError(path)
        ctx["json_api"]=api
        ctx["download_output"]=lambda _url,dest,*_: (dest/"video.part").write_bytes(movie(2))
        ctx["take_quality"]=SimpleNamespace(check=lambda *_:{"technical":"passed","audioStreams":1})
        view=dict(end=time.time()+60,episode=eid,submitted=2,phase="Preparing ComfyUI",completed=0,failed=0)
        session=direct_session.DirectSession(ctx)
        session.start(view,jobs,wf,None,"http://fixture")
        previous=self.root/"production"/"workflow-checks"/session.plan["id"]
        (previous/"original"/"attempt").mkdir(parents=True)
        production.atomic(previous/"original"/"attempt"/"job.json",{"status":"error"})
        production.atomic(previous/"verification.json",{"status":"failed","message":"Old template rejected"})
        rejected='ComfyUI 400: {"error": {"type": "prompt_outputs_failed_validation"}}'
        failed=self.root/eid/"one"/"1"/"job.json"
        failed.parent.mkdir(parents=True)
        production.atomic(failed,{"status":"error","revision":"approved","error":rejected})
        production.atomic(previous/"first-shot.json",{"status":"failed","prompt":None,
                          "message":"First shot verification failed. "+rejected})
        with patch("workflow_gate.editor_media.tool"):
            session.run("http://fixture",wf)
        self.assertEqual((view["phase"],view["completed"]),("Finished",2),view.get("error"))
        self.assertEqual([p["84"]["inputs"]["value"] for p in prompts],
                         ["Tom and Jack talk.","Jack walks silently."])
        self.assertEqual(uploaded,["Tom.png","Jack.png","Tom.wav","Jack.wav","Jack.png"])
        self.assertEqual(production.read(failed)["error"],rejected)
        for prompt, expected in zip(prompts,(["Tom.png","Jack.png","Tom.wav","Jack.wav"],["Jack.png"]),strict=True):
            self.assertEqual(set(prompt.keys()),set(graph.keys()))
            self.assertEqual(prompt["65"]["inputs"]["vae_name"],linux_vae)
            self.assertEqual(prompt["124"]["inputs"],graph["124"]["inputs"])
            reference=prompt["56"]["inputs"]
            actual=[next(iter(prompt[target[0]]["inputs"].values())) for slot,target in reference.items()
                    if slot.startswith(("ref_images.","ref_audios."))]
            self.assertEqual(actual,expected)
        self.assertEqual(wf["graph"]["84"]["inputs"]["value"],"Template text")
        self.assertEqual(wf["graph"]["65"]["inputs"]["vae_name"],linux_vae)

    def test_unconfirmed_script_and_credentials_in_script_never_reach_ssh(self):
        manager,version=self.manager()
        with patch.object(workflow_manager.vast_deploy,"ssh") as ssh:
            with self.assertRaisesRegex(ValueError,"confirm"):
                manager.action({"action":"install","version":version,"script":"echo hi"})
            with self.assertRaisesRegex(ValueError,"Save your Hugging Face token"):
                manager.action({"action":"install","version":version,"script":"echo hf_secretabcdefgh","confirmed":True})
        ssh.assert_not_called()

    def test_install_adopts_a_pasted_hf_assignment_without_persisting_it_in_the_script(self):
        manager,version=self.manager()
        fixture="hf_fixtureabcdefghijkl"
        script=f"export HF_TOKEN='{fixture}'\necho installing\n"
        stored=[]
        def vault(value=None):
            if value:
                stored.append(value)
            return stored[-1] if stored else None
        launched=threading.Event()
        with patch.object(workflow_manager,"hf_token",side_effect=vault), \
             patch.object(manager,"launch",side_effect=lambda *_:launched.set()) as launch:
            preview=manager.action({"action":"install-preview","script":script})
            self.assertEqual(stored,[])
            self.assertTrue(preview["willSaveHfToken"])
            self.assertNotIn(fixture,json.dumps(preview))
            manager.action({"action":"install","version":version,"script":script,"confirmed":True})
            self.assertTrue(launched.wait(2))
            self.assertEqual(stored,[fixture])
            self.assertEqual(launch.call_args.args[-1],preview["script"])
            reopened=workflow_manager.WorkflowManager(manager.ctx)
            self.assertTrue(reopened.status(version)["hfTokenSaved"])
            self.assertEqual(reopened.profile(version)["script"],'export HF_TOKEN="$HF_TOKEN"\necho installing\n')

    def test_save_and_test_hf_token_reuses_the_persistent_vault(self):
        manager,version=self.manager()
        fixture="hf_fixtureabcdefghijkl"
        stored=[]
        def vault(value=None):
            if value:
                stored.append(value)
            return stored[-1] if stored else None
        with patch.object(workflow_manager,"hf_token",side_effect=vault), \
             patch.object(workflow_manager,"build_opener") as opener:
            self.assertFalse(manager.status(version)["hfTokenSaved"])
            self.assertTrue(manager.action({"action":"save-token","token":fixture})["saved"])
            opener.assert_not_called()
            opener.return_value.open.return_value.__enter__.return_value.status=200
            self.assertTrue(manager.action({"action":"token","token":""})["valid"])
            self.assertEqual(opener.return_value.open.call_args.args[0].headers["Authorization"],"Bearer "+fixture)
            self.assertEqual(stored,[fixture])
            self.assertNotIn(fixture,json.dumps(manager.status(version)))

    def test_ssh_dispatch_sends_credential_on_stdin_only(self):
        manager,version=self.manager()
        record=manager.records["42"]=dict(id="a"*32,version=version,instance="42")
        token="hf_fixtureabcdefghijkl"
        with patch.object(workflow_manager,"hf_token",return_value=token), \
             patch.object(workflow_manager.vast_deploy,"ssh") as ssh, \
             patch.object(workflow_manager.vast_deploy,"deploy_files"), \
             patch.object(workflow_manager.vast_deploy,"put_json"),patch.object(manager,"watch"):
            manager.launch(record,manager.connection.target(),{"mode":"install"},"echo ready")
        self.assertEqual(ssh.call_args.kwargs["data"],(token+"\n").encode())
        self.assertNotIn(token,ssh.call_args.args[1])
        self.assertNotIn(token,manager.path.read_text("utf-8"))

    def test_reopen_reconciles_same_worker_and_sends_cancel_marker(self):
        manager,version=self.manager()
        record=manager.records["42"]=dict(id="b"*32,version=version,instance="42",status="reconnecting",
                            dispatched=True,started=time.time(),timeout=60,cancel=True)
        manager.save(record)
        reopened=workflow_manager.WorkflowManager(manager.ctx)
        replies=iter([b"",OSError("Disconnected"),b"",json.dumps({"started":True,"status":"cancelled",
                        "report":{"nodes":[],"models":[]},"exitCode":-15,"finished":time.time()}).encode()])
        def ssh(*args,**kwargs):
            result=next(replies)
            if isinstance(result,Exception):
                raise result
            return result
        with patch.object(workflow_manager.vast_deploy,"ssh",side_effect=ssh) as mock,patch.object(workflow_manager.time,"sleep"):
            reopened.reconcile(reopened.records["42"])
        self.assertEqual(reopened.status(version)["installation"]["status"],"cancelled")
        self.assertTrue(any("touch" in call.args[1] for call in mock.call_args_list))
        self.assertTrue(all("b"*32 in call.args[1] for call in mock.call_args_list))
        self.assertTrue(all(call.kwargs["timeout"]==45 for call in mock.call_args_list))
        reopened.connection.retry_saved.assert_called_once_with("42")

    def test_status_treats_unselected_workflow_as_empty(self):
        manager,version=self.manager()
        for placeholder in (None,"","undefined","null"):
            view=manager.status(placeholder)
            self.assertIsNone(view["selectedVersion"])
            self.assertIsNone(view["verification"])
            self.assertEqual(view["savedScript"],"")
        self.assertEqual(manager.status(version)["selectedVersion"],version)

    def test_failed_install_preserves_last_successful_dependency_scan(self):
        manager,version=self.manager()
        report={"nodes":["A missing node"],"models":[]}
        manager.save_profile(version,instances={"42":{"status":"needs attention","report":report}})
        record=manager.records["42"]=dict(id="c"*32,version=version,instance="42",status="running",
                            dispatched=True,started=time.time(),timeout=60)
        manager.save(record)
        terminal={"started":True,"status":"failed","report":{},"exitCode":1,"finished":time.time()}
        with patch.object(workflow_manager.vast_deploy,"ssh",return_value=json.dumps(terminal).encode()),patch.object(workflow_manager.time,"sleep"):
            manager.reconcile(record)
        view=manager.status(version)
        self.assertEqual(view["installation"]["status"],"failed")
        self.assertEqual(view["verification"]["report"],report)

    def test_interrupted_installation_on_old_instance_does_not_block_new_instance(self):
        manager,version=self.manager()
        previous={"id":"d"*32,"version":version,"instance":"41","status":"reconnecting",
                  "dispatched":True,"cancel":True,"started":time.time(),"timeout":1800}
        production.atomic(manager.path.with_name("workflow-installation.json"),previous)
        manager=workflow_manager.WorkflowManager(manager.ctx)
        with patch.object(manager,"watch"),patch.object(manager,"launch") as launch:
            self.assertFalse(manager.busy())
            view=manager.status(version)
            self.assertIsNone(view["installation"]["status"])
            self.assertEqual(view["otherInstallations"],1)
            manager.action({"action":"scan","version":version})
            self.assertTrue(manager.busy())
            with self.assertRaisesRegex(ValueError,"already active on this Vast instance"):
                manager.action({"action":"scan","version":version})
        self.assertTrue(launch.called)
        self.assertEqual(production.read(manager.path)["41"],previous)
        manager.connection.target.return_value={"id":41}
        self.assertTrue(manager.busy())
        with patch.object(manager,"watch"):
            self.assertEqual(manager.status(version)["installation"]["instance"],"41")
            with self.assertRaisesRegex(ValueError,"selected instance changed"):
                manager.action({"action":"cancel","instance":"42"})

    def test_install_cancellation_signals_child_process_group(self):
        production.atomic(self.root/"spec.json",{"timeout":60})
        child=Mock(pid=4242)
        child.wait.return_value=-15
        monitor=Mock()
        context=Mock()
        context.__enter__=Mock(return_value=monitor)
        context.__exit__=Mock(return_value=None)
        monitor.get_map.return_value={1:True}
        def spawn(*args,**kwargs):
            self.assertTrue(kwargs["start_new_session"])
            (self.root/"cancel").touch()
            return child
        with patch.object(remote.subprocess,"Popen",side_effect=spawn),patch.object(remote.selectors,"DefaultSelector",return_value=context), patch.object(remote.os,"killpg",create=True) as signal_group,patch.object(remote.signal,"SIGKILL",9,create=True):
            remote.run(self.root)
        self.assertEqual(signal_group.call_args_list[0].args,(4242,remote.signal.SIGTERM))
        result=remote.snapshot(self.root)
        self.assertEqual((result["status"],result["exitCode"]),("cancelled",-15))

    def test_install_displays_carriage_return_progress_and_redacts_split_tokens(self):
        production.atomic(self.root/"spec.json",{"timeout":60})
        token="hf_fixtureabcdefghijkl"
        child=Mock(pid=4242)
        child.wait.return_value=0
        key=SimpleNamespace(fd=7,fileobj=child.stdout)
        monitor=Mock()
        monitor.select.return_value=[(key,1)]
        monitor.get_map.side_effect=[True,True,True,True,False]
        selector=Mock(__enter__=Mock(return_value=monitor),__exit__=Mock(return_value=None))
        steps=iter([b"90%\r",b"secret hf_fixture",b"abcdefghijkl\n100%\r",b""])
        def output(_fd,_count):
            chunk=next(steps)
            if chunk.startswith(b"secret"):
                self.assertIn("90%",(self.root/"terminal.log").read_text("utf-8"))
            return chunk
        with patch.dict(remote.os.environ,HF_TOKEN=token), \
             patch.object(remote.subprocess,"Popen",return_value=child), \
             patch.object(remote.selectors,"DefaultSelector",return_value=selector), \
             patch.object(remote.os,"read",side_effect=output):
            remote.run(self.root)
        log=(self.root/"terminal.log").read_text("utf-8")
        self.assertNotIn(token,log)
        self.assertIn("90%\nsecret [REDACTED]\n100%\n",log)

    def test_stop_cancels_only_saved_prompt_and_leaves_next_shot_unsubmitted(self):
        eid=str(uuid.uuid4())
        jobs=[[eid,dict(id=str(uuid.uuid4()),revision="approved",seconds=2,prompt="Original"),[]] for _ in range(2)]
        ticket=str(uuid.uuid4())
        calls=[]
        session=None
        def api(url,path,obj=None):
            calls.append(path)
            if path=="/object_info":
                return {"Output":{}}
            if path=="/prompt":
                session.control({"action":"stop","episode":eid})
                return {"prompt_id":ticket}
            if path==f"/api/jobs/{ticket}/cancel":
                return {"cancelled":True}
            self.fail(f"Unexpected request: {path}")
        ctx={"DATA":self.root,"LOCK":threading.RLock(),"json_api":api,
             "prepare_shot":lambda *_:{"141":{"inputs":{}}},"download_output":Mock(),
             "prompt":lambda s,_:s["prompt"],"take_quality":take_quality,"running":True}
        wf=dict(version="a"*64,name="Fixture",output="141",graph={"141":node("Output")})
        view=dict(end=time.time()+60,episode=eid,submitted=2,phase="Preparing ComfyUI",completed=0,failed=0)
        session=direct_session.DirectSession(ctx)
        session.start(view,jobs,wf,None,"http://fixture")
        with patch.object(direct_session,"runtime_requirements",return_value={"graph":wf["graph"]}), \
             patch.object(workflow_profiles,"timing",return_value={"playback_seconds":2}):
            session.run("http://fixture",wf)
        self.assertEqual(view["phase"],"Stopped")
        self.assertEqual(calls,["/object_info","/prompt",f"/api/jobs/{ticket}/cancel"])
        row=next((self.root/eid/jobs[0][1]["id"]).glob("*/job.json"))
        self.assertEqual((production.read(row)["status"],production.read(row)["ticket"]),("cancelled",ticket))
        self.assertEqual(session.plan["index"],0)

    def test_interrupted_session_resumes_with_original_deadline_and_can_be_archived(self):
        folder=self.root/"production"
        deadline=time.time()+120
        existing={"control":"run","view":{"episode":"episode","phase":"Rendering","end":deadline},
                  "instance":"42","version":"a"*64}
        production.atomic(folder/"direct-session.json",existing)
        target={"id":42,"host":"fixture","port":22}
        connection=SimpleNamespace(target=lambda:target,snapshot=lambda:{"url":"http://tunnel"})
        workflow={"version":"a"*64}
        run=Mock()
        ctx={"DATA":self.root,"LOCK":threading.RLock(),"running":False,"run_direct":run,
             "production_busy":lambda:False,"controller":SimpleNamespace(connection=connection),
             "actions":SimpleNamespace(store=SimpleNamespace(get=lambda _:workflow)),
             "workflow_manager":SimpleNamespace(busy=lambda:False)}
        direct=direct_session.DirectSession(ctx)
        direct.resume_interrupted()
        run.assert_called_once_with("http://tunnel",workflow,target)
        self.assertEqual(ctx["session"]["end"],deadline)
        direct.archive_for_new_session()
        [archived]=(folder/"session-history").glob("*.json")
        self.assertEqual(production.read(archived)["view"]["episode"],"episode")
        self.assertEqual(production.read(folder/"direct-session.json"),{})

    def test_retry_releases_only_verified_video_on_missing_original_instance(self):
        path=self.root/"job.json"
        job={"instance":"42","ticket":"one", "status":"waiting","artifact_pending":True}
        production.atomic(path,job)
        partial=self.root/"video.part"
        partial.write_bytes(b"original partial")
        history=self.root/"history.json"
        production.atomic(history,{"status":{"status_str":"success","completed":True}})
        connection=Mock()
        ctx={"DATA":self.root,"controller":SimpleNamespace(connection=connection)}
        session=direct_session.DirectSession(ctx)
        connection.discover_instances.return_value=[{"id":42,"status":"exited"}]
        session.release_unavailable_video(path,job,{"id":73})
        self.assertEqual(production.read(path)["status"],"waiting")
        connection.discover_instances.return_value=[]
        session.release_unavailable_video(path,job,{"id":73})
        self.assertEqual(production.read(path)["status"],"error")
        self.assertEqual(partial.read_bytes(),b"original partial")
        self.assertTrue(history.exists())

    def test_model_download_resumes_range_and_keeps_partial(self):
        partial=self.root/"weight.part"
        partial.write_bytes(b"123")
        class Server(BaseHTTPRequestHandler):
            def do_GET(handler):
                self.assertEqual(handler.headers["Range"],"bytes=3-")
                handler.send_response(206)
                handler.send_header("Content-Range","bytes 3-5/6")
                handler.send_header("Content-Length","3")
                handler.end_headers()
                handler.wfile.write(b"456")
            def log_message(self,*args):
                pass
        server=ThreadingHTTPServer(("127.0.0.1",0),Server)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            provision.download_weight(f"http://127.0.0.1:{server.server_port}/model",partial,6)
            self.assertEqual(partial.read_bytes(),b"123456")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_three_direct_shots_reconcile_dropped_submission_with_real_mp4(self):
        try:
            ffmpeg=editor_media.tool("ffmpeg")
        except ValueError:
            self.skipTest("FFmpeg unavailable")
        src=self.root/"real.mp4"
        subprocess.run([ffmpeg,"-v","error","-f","lavfi","-i","testsrc2=size=160x120:rate=24",
                        "-f","lavfi","-i","sine=1000:sample_rate=48000","-t","2","-c:v","libx264",
                        "-c:a","aac",str(src)],check=True)
        eid=str(uuid.uuid4())
        jobs=[[eid,dict(id=str(uuid.uuid4()),revision="approved",seconds=2,prompt="Original"),[]] for _ in range(3)]
        submitted=[]
        downloaded=[]
        payload=src.read_bytes()
        partial_size=len(payload)//2
        class MediaServer(BaseHTTPRequestHandler):
            def do_GET(handler):
                if "filename=real.mp4" not in handler.path:
                    handler.send_error(404)
                    return
                sent_range=handler.headers.get("Range")
                downloaded.append((handler.path,sent_range,handler.headers.get("If-Range")))
                offset=partial_size if sent_range==f"bytes={partial_size}-" else 0
                handler.send_response(206 if offset else 200)
                handler.send_header("Content-Length",str(len(payload)-offset))
                handler.send_header("ETag",'"fixture-mp4"')
                if offset:
                    handler.send_header("Content-Range",f"bytes {offset}-{len(payload)-1}/{len(payload)}")
                handler.end_headers()
                handler.wfile.write(payload[:partial_size] if len(downloaded)==1 else payload[offset:])
            def log_message(self,*args):
                pass
        media=ThreadingHTTPServer(("127.0.0.1",0),MediaServer)
        thread=threading.Thread(target=media.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(media.server_close)
        self.addCleanup(media.shutdown)
        new_url=f"http://127.0.0.1:{media.server_port}"
        tunnel=["http://old-tunnel"]
        target={"id":42,"host":"fixture","port":2222,"rate":1}
        def current_target():
            if not tunnel[0]:
                raise ValueError("SSH disconnected")
            return target
        def reopen(instance):
            self.assertEqual(instance,"42")
            tunnel[0]=new_url
        connection=SimpleNamespace(target=current_target,snapshot=lambda:{"url":tunnel[0]},
                                   lock=threading.RLock(),retry_saved=Mock(side_effect=reopen))
        preflight=[]
        def api(url,path,obj=None):
            self.assertEqual(url,tunnel[0])
            if path=="/object_info":
                preflight.append((url,path))
                raise ConnectionResetError("Large preflight response reset the connection")
            if path.startswith("/object_info/"):
                preflight.append((url,path))
                return {path.rsplit("/",1)[-1]:{}}
            if path=="/prompt":
                submitted.append(obj)
                if len(submitted)==1:
                    tunnel[0]=None
                    raise ConnectionError("Lost POST acknowledgement")
                return {"prompt_id":str(len(submitted))}
            if path=="/queue":
                return {"queue_running":[[i,str(i),x["prompt"],x["extra_data"]] for i,x in enumerate(submitted,1)],
                        "queue_pending":[]}
            ticket=path.rsplit("/",1)[-1]
            return {ticket:{"status":{"status_str":"success","completed":True},
                            "outputs":{"135":{"videos":[{"filename":"wrong-preview.mp4"}]},
                                       "141":{"videos":[{"filename":"real.mp4","type":"output"}]}}}}
        download=runpy.run_path("studio.pyw")["download_output"]
        ctx={"DATA":self.root,"LOCK":threading.RLock(),"controller":SimpleNamespace(connection=connection),
             "json_api":api,"prepare_shot":lambda _u,_f,_e,s,_w,_r:{"id":s["id"],"141":{"inputs":{"audio":["arr",0]}}},
             "download_output":download,"prompt":lambda s,_:s["prompt"],"take_quality":take_quality,"running":True}
        # Versioned output, with existing timing checks.
        store=workflow_profiles.WorkflowStore(self.root/"graph")
        wf=store.import_workflow("API",json.dumps(self.graph).encode())
        wf=dict(wf,output="141")
        view=dict(end=time.time()+60,episode=eid,submitted=3,phase="Preparing ComfyUI",completed=0,failed=0)
        session=direct_session.DirectSession(ctx)
        session.start(view,jobs,wf,target,tunnel[0])
        with patch.object(direct_session,"runtime_requirements",return_value={"graph":self.graph}), \
             patch.object(workflow_manager.vast_deploy,"ssh") as ssh, \
             patch.object(workflow_profiles,"timing",return_value={"playback_seconds":2}),patch.object(production.time,"sleep"):
            session.run(tunnel[0],wf,target)
        ssh.assert_not_called()
        classes=sorted({n["class_type"] for n in self.graph.values()})
        self.assertEqual(preflight,[("http://old-tunnel",path) for path in
                                   ["/object_info",*("/object_info/"+kind for kind in classes)]])
        self.assertEqual((view["phase"],view["completed"]),("Finished",3),view.get("error"))
        self.assertEqual([x["prompt"]["id"] for x in submitted],[shot["id"] for _,shot,_ in jobs])
        connection.retry_saved.assert_called_once_with("42")
        self.assertEqual(len(downloaded),4)
        self.assertEqual(downloaded[1][1:],(f"bytes={partial_size}-",'"fixture-mp4"'))
        for _,shot,_ in jobs:
            record=next((self.root/eid/shot["id"]).glob("*/job.json"))
            job=production.read(record)
            self.assertEqual(job["workflow_version"],wf["version"])
            self.assertTrue(job["ticket"])
            self.assertEqual(job["quality"]["audioStreams"],1)
            self.assertEqual(production.digest(record.parent/"video.mp4"),production.digest(src))


if __name__=="__main__":
    unittest.main()
