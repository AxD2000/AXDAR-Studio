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
        with self.assertRaisesRegex(ValueError,"Attach"):
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
        connection.target.return_value={"id":42,"host":"fixture","port":2222,"rate":0.7}
        connection.ssh_target.return_value=connection.target.return_value
        connection._remote_port.return_value=18188
        ctx={"controller":SimpleNamespace(connection=connection),"BASE":Path.cwd(),"DATA":self.root,
             "actions":SimpleNamespace(store=store),"production_busy":lambda:False,
             "running":False,"LOCK":threading.RLock()}
        return workflow_manager.WorkflowManager(ctx),wf["version"]

    def test_unconfirmed_script_and_credentials_in_script_never_reach_ssh(self):
        manager,version=self.manager()
        with patch.object(workflow_manager.vast_deploy,"ssh") as ssh:
            with self.assertRaisesRegex(ValueError,"confirm"):
                manager.action({"action":"install","version":version,"script":"echo hi"})
            with self.assertRaisesRegex(ValueError,"environment"):
                manager.action({"action":"install","version":version,"script":"export HF_TOKEN=hf_secretabcdefgh","confirmed":True})
        ssh.assert_not_called()

    def test_ssh_dispatch_sends_credential_on_stdin_only(self):
        manager,version=self.manager()
        manager.record=dict(id="a"*32,version=version)
        token="hf_fixtureabcdefghijkl"
        with patch.object(workflow_manager,"hf_token",return_value=token), \
             patch.object(workflow_manager.vast_deploy,"ssh") as ssh, \
             patch.object(workflow_manager.vast_deploy,"deploy_files"), \
             patch.object(workflow_manager.vast_deploy,"put_json"),patch.object(manager,"watch"):
            manager.launch(manager.connection.target(),{"mode":"install"},"echo ready")
        self.assertEqual(ssh.call_args.kwargs["data"],(token+"\n").encode())
        self.assertNotIn(token,ssh.call_args.args[1])
        self.assertNotIn(token,manager.path.read_text("utf-8"))

    def test_reopen_reconciles_same_worker_and_sends_cancel_marker(self):
        manager,version=self.manager()
        manager.record=dict(id="b"*32,version=version,instance="42",status="reconnecting",
                            dispatched=True,started=time.time(),timeout=60,cancel=True)
        manager.save()
        reopened=workflow_manager.WorkflowManager(manager.ctx)
        replies=iter([b"",OSError("Disconnected"),b"",json.dumps({"started":True,"status":"cancelled",
                        "report":{"nodes":[],"models":[]},"exitCode":-15,"finished":time.time()}).encode()])
        def ssh(*args,**kwargs):
            result=next(replies)
            if isinstance(result,Exception):
                raise result
            return result
        with patch.object(workflow_manager.vast_deploy,"ssh",side_effect=ssh) as mock,patch.object(workflow_manager.time,"sleep"):
            reopened.reconcile("b"*32)
        self.assertEqual(reopened.status(version)["installation"]["status"],"cancelled")
        self.assertTrue(any("touch" in call.args[1] for call in mock.call_args_list))
        self.assertTrue(all("b"*32 in call.args[1] for call in mock.call_args_list))

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
        with patch.object(workflow_profiles,"timing",return_value={"playback_seconds":2}):
            session.run("http://fixture",wf)
        self.assertEqual(view["phase"],"Stopped")
        self.assertEqual(calls,["/object_info","/prompt",f"/api/jobs/{ticket}/cancel"])
        row=next((self.root/eid/jobs[0][1]["id"]).glob("*/job.json"))
        self.assertEqual((production.read(row)["status"],production.read(row)["ticket"]),("cancelled",ticket))
        self.assertEqual(session.plan["index"],0)

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
        class MediaServer(BaseHTTPRequestHandler):
            def do_GET(handler):
                if "filename=real.mp4" not in handler.path:
                    handler.send_error(404)
                    return
                downloaded.append(handler.path)
                payload=src.read_bytes()
                handler.send_response(200)
                handler.send_header("Content-Length",str(len(payload)))
                handler.end_headers()
                handler.wfile.write(payload)
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
        connection=SimpleNamespace(target=lambda:target,snapshot=lambda:{"url":tunnel[0]})
        def api(url,path,obj=None):
            self.assertEqual(url,tunnel[0])
            if path=="/prompt":
                submitted.append(obj)
                if len(submitted)==1:
                    tunnel[0]=new_url
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
        with patch.object(direct_session.vast_deploy,"prepare_direct"),patch.object(workflow_profiles,"timing",return_value={"playback_seconds":2}),patch.object(production.time,"sleep"):
            session.run(tunnel[0],wf,target)
        self.assertEqual((view["phase"],view["completed"]),("Finished",3),view.get("error"))
        self.assertEqual([x["prompt"]["id"] for x in submitted],[shot["id"] for _,shot,_ in jobs])
        self.assertEqual(len(downloaded),3)
        for _,shot,_ in jobs:
            record=next((self.root/eid/shot["id"]).glob("*/job.json"))
            job=production.read(record)
            self.assertEqual(job["workflow_version"],wf["version"])
            self.assertTrue(job["ticket"])
            self.assertEqual(job["quality"]["audioStreams"],1)
            self.assertEqual(production.digest(record.parent/"video.mp4"),production.digest(src))


if __name__=="__main__":
    unittest.main()
