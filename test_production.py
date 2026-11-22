import io
import sys
import tarfile
import types
import json
import runpy
import struct
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import production as p
import vast_deploy as vd
import vast_worker as vw

ROOT=Path(__file__).resolve().parent

def box(k,v):
    return struct.pack(">I4s",len(v)+8,k)+v

def movie(seconds,audio=True):
    header=bytes(12)+struct.pack(">II",1000,round(seconds*1000))
    track=box(b"trak",box(b"mdia",box(b"hdlr",bytes(8)+b"vide")))
    if audio:track+=box(b"trak",box(b"mdia",box(b"hdlr",bytes(8)+b"soun")))
    return box(b"ftyp",b"isom")+box(b"mdat",b"sample")+box(b"moov",box(b"mvhd",header)+track)


class QueueTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.dir=Path(tmp.name); self.status=self.dir/"status.json"; self.control=self.dir/"control.json"
        self.eid=str(uuid.uuid4());self.requests=[];self.failed_shots=set();self.lose=False;self.network=0
        self.workflow={"name":"fixture","output":"141"}
        self.ctx={"DATA":self.dir,"prompt":lambda s,r:s["prompt"],"prepare_shot":lambda url,folder,eid,s,w,j:{"id":s["id"]},"json_api":self.api,"download_output":lambda u,f,h,j:(f/"video.part").write_bytes(movie(j["settings"]["seconds"]))}
        self.spec=dict(id="test",started=time.time(),url="http://fixture",budget=0,expectedSeconds=30,rate=1,end=time.time()+600,retries=2)
        self.sleep=patch("production.time.sleep");self.sleep.start();self.addCleanup(self.sleep.stop)

    def jobs(self,n):
        return [(self.eid,dict(id=str(uuid.uuid4()),revision="rev",seconds=5,prompt="Tom"),[]) for _ in range(n)]

    def api(self,url,path,obj=None):
        if path=="/prompt":
            self.requests.append(obj)
            if self.lose:
                self.lose=False
                raise ConnectionError("lost acknowledgement")
            return {"prompt_id":str(len(self.requests))}
        if path=="/queue":
            return dict(queue_running=[[i,str(i),x["prompt"],x["extra_data"]] for i,x in enumerate(self.requests,1)],queue_pending=[])
        if path=="/history":return {}
        if self.network:
            self.network-=1;raise ConnectionError("reset")
        ticket=path.split("/")[-1];shot=self.requests[int(ticket)-1]["prompt"]["id"]
        return {ticket:{"status":{"status_str":"error" if shot in self.failed_shots else "success"}}}

    def drive(self,jobs,retry=None):
        p.drive(self.ctx,self.spec,jobs,self.workflow,self.status,self.control,retry)
        return p.read(self.status)

    def test_200_shots_and_repeat_no_duplicate(self):
        jobs=self.jobs(200); state=self.drive(jobs)
        self.assertEqual(state["status"],"finished"); self.assertEqual(len(self.requests),200)
        self.assertEqual([x["prompt"]["id"] for x in self.requests],[s["id"] for _,s,_ in jobs])
        self.status.unlink();self.drive(jobs);self.assertEqual(len(self.requests),200)

    def test_direct_output_is_available_after_technical_checks(self):
        eid,shot,rules=self.jobs(1)[0]
        ctx=dict(self.ctx,direct=True,instance=12,
                 technical_qc=lambda path,job:{"technical":"passed","visual":"review required"})
        args=(eid,shot,rules,self.workflow,0,lambda:None)
        self.assertEqual(p.render(ctx,self.spec["url"],self.spec["end"],*args),"done")
        record=next((self.dir/eid/shot["id"]).glob("*/job.json"))
        take=p.read(record)
        self.assertEqual(take["instance"],12)
        self.assertNotIn("review",take)
        self.assertEqual(take["quality"],{"technical":"passed"})
        self.assertTrue((record.parent/"video.mp4").is_file())
        self.assertEqual(p.render(ctx,"http://new-tunnel",self.spec["end"],*args),"done")
        self.assertEqual(len(self.requests),1)

    def test_direct_upgrade_reconciles_legacy_numeric_instance_id(self):
        eid,shot,rules=self.jobs(1)[0]
        prior=dict(self.ctx,direct=True,instance=12)
        args=(eid,shot,rules,self.workflow,0,lambda:None)
        self.network=1
        self.assertEqual(p.render(prior,"http://old-tunnel",self.spec["end"],*args),"waiting")
        updated=dict(prior,instance="12")
        self.assertEqual(p.render(updated,"http://new-tunnel",self.spec["end"],*args),"done")
        self.assertEqual(len(self.requests),1)

    def test_atomic_retries_when_windows_reader_holds_destination(self):
        error=PermissionError("sharing violation")
        error.winerror=32
        replace=p.os.replace
        def retryable_replace(src,dest):
            if mocked.call_count==1:
                raise error
            replace(src,dest)
        with patch("production.os.replace",side_effect=retryable_replace) as mocked:
            p.atomic(self.status,{"phase":"done"})
        self.assertEqual(mocked.call_count,2)
        self.assertEqual(p.read(self.status),{"phase":"done"})

    def test_failure_continues_and_manual_retry(self):
        jobs=self.jobs(3);bad=jobs[1][1]["id"];self.failed_shots.add(bad)
        state=self.drive(jobs);self.assertEqual(list(state["shots"].values()),["done","error","done"])
        self.failed_shots.clear();self.assertEqual(self.drive(jobs,bad)["status"],"finished")
        self.assertEqual(len(self.requests),4)

    def test_definite_failures_retry_only_after_the_initial_pass(self):
        jobs=self.jobs(3)
        ids=[s["id"] for _,s,_ in jobs]
        self.failed_shots.add(ids[0])
        self.spec["shotRetries"]=1
        def api(url,path,obj=None):
            if path=="/prompt" and len(self.requests)==3:
                self.failed_shots.clear()
            return self.api(url,path,obj)
        self.ctx["json_api"]=api
        state=self.drive(jobs)
        self.assertEqual(state["status"],"finished")
        self.assertEqual([x["prompt"]["id"] for x in self.requests],[*ids,ids[0]])
        takes=sorted((self.dir/self.eid/ids[0]).glob("*/job.json"))
        self.assertEqual([p.read(t)["status"] for t in takes],["error","done"])
        self.assertEqual(len({p.read(t)["take_id"] for t in takes}),2)
        self.assertEqual(self.drive(jobs)["status"],"finished")
        self.assertEqual(len(self.requests),4)

    def test_budget_is_checked_again_after_preparing_and_before_submission(self):
        jobs=self.jobs(1)
        self.spec["monthBudget"]=.15
        self.spec["priorMonthEstimate"]=.1
        started=self.spec["started"]
        elapsed=0
        def prepare(*args):
            nonlocal elapsed
            elapsed=210
            return {"id":args[3]["id"]}
        self.ctx["prepare_shot"]=prepare
        with patch("production.time.time",side_effect=lambda:started+elapsed):
            self.assertEqual(self.drive(jobs)["status"],"limit reached")
        self.assertEqual(self.requests,[])
        self.assertEqual(len(list(self.dir.glob("*/*/*/job.json"))),1)

    def test_ambiguous_submit_and_transient_poll(self):
        self.lose=True;self.network=1
        self.assertEqual(self.drive(self.jobs(2))["status"],"finished")
        self.assertEqual(len(self.requests),2)

    def test_restart_poll_same_ticket(self):
        jobs=self.jobs(2);self.network=4
        self.assertEqual(self.drive(jobs)["status"],"waiting")
        self.assertEqual(self.drive(jobs)["status"],"finished");self.assertEqual(len(self.requests),2)

    def test_missing_ack_no_evidence_never_resubmits(self):
        def no_ack(url,path,obj=None):
            if path=="/prompt":
                self.requests.append(obj);raise ConnectionError("lost")
            if path=="/queue":return dict(queue_running=[],queue_pending=[])
            return {}
        self.ctx["json_api"]=no_ack
        state=self.drive(self.jobs(1));self.assertIn("uncertain",state["shots"].values())
        self.assertEqual(len(self.requests),1)

    def test_budget_pause_stop(self):
        jobs=self.jobs(2);self.spec["budget"]=.0001
        self.assertEqual(self.drive(jobs)["status"],"limit reached");self.assertFalse(self.requests)
        self.spec["budget"]=0
        for action,status in [("pause","paused"),("stop","stopped")]:
            p.atomic(self.control,dict(action=action));self.assertEqual(self.drive(jobs)["status"],status)
        self.control.unlink();self.assertEqual(self.drive(jobs)["status"],"finished")

    def test_bad_video_fails_without_blocking_next(self):
        jobs=self.jobs(2)
        def download(url,folder,h,j):
            v=movie(5)
            (folder/"video.part").write_bytes(v[:-9] if j["shot"]==jobs[0][1]["id"] else v)
        self.ctx["download_output"]=download
        self.assertEqual(list(self.drive(jobs)["shots"].values()),["error","done"])
        file=self.dir/"wrong.mp4";file.write_bytes(movie(25))
        with self.assertRaises(ValueError):p.inspect_video(file,5)

    def test_frame_grid_duration_is_used_for_render_and_resume(self):
        from test_workflow_profiles import generic_graph
        from workflow_profiles import inspect_workflow

        graph, metadata = generic_graph()
        graph["out"]["inputs"]["fps"] = 10
        metadata["MyVideo"]["input"]["required"]["frame_count"][1]["step"] = 20
        self.workflow = inspect_workflow("long frame steps", graph, metadata)
        self.assertEqual(self.workflow["issues"], [])
        jobs = self.jobs(1)
        jobs[0][1]["seconds"] = 1
        self.ctx["download_output"] = lambda url,folder,history,j: (folder/"video.part").write_bytes(movie(2.1))
        self.assertEqual(self.drive(jobs)["status"],"finished")
        record, = self.dir.glob("*/*/*/job.json")
        self.assertEqual(p.read(record)["playback_seconds"],2.1)
        self.status.unlink()
        self.assertEqual(self.drive(jobs)["status"],"finished")
        self.assertEqual(len(self.requests),1)

    def test_output_audio_is_checked_against_the_runtime_graph(self):
        jobs = self.jobs(2)
        silent, voiced = (shot["id"] for _,shot,_ in jobs)
        self.workflow["graph"] = {"141":{"inputs":{"audio":["51",0]}}}
        def prepare(_url,_folder,_eid,shot,_workflow,_job):
            return {"id":shot["id"], "141":{"inputs":{"audio":["51",0]} if shot["id"]==voiced else {}}}
        self.ctx["prepare_shot"] = prepare
        self.ctx["download_output"] = lambda url,folder,history,j: (folder/"video.part").write_bytes(movie(5,False))
        self.assertEqual(self.drive(jobs)["shots"],{silent:"done",voiced:"error"})

    def test_missing_audio_track(self):
        path=self.dir/"video.mp4";path.write_bytes(movie(5,False))
        with self.assertRaisesRegex(ValueError,"audio track"):p.inspect_video(path,5,True)

    def test_repeat_deployment_sends_only_missing_files(self):
        target={};spec={"id":uuid.uuid4().hex,"release":"a"*64,"assets":{"test.png":"d"*64}}
        asset=self.dir/"asset";asset.write_bytes(b"image")
        spec["assets"]["test.png"]=p.digest(asset)
        inventory={}
        def fake_ssh(target,cmd,**kw):
            if cmd.startswith("while "):
                pairs=[line.split("  ",1) for line in kw["data"].decode().splitlines()]
                return "\n".join(dst for sha,dst in pairs if inventory.get(dst)!=sha).encode()
            return b""
        def fake_put(t,dst,src):inventory[dst]=p.digest(src)
        with patch.object(vd,"ssh",side_effect=fake_ssh),patch.object(vd,"put",side_effect=fake_put) as send:
            vd.deploy(ROOT,target,spec,{"test.png":asset})
            count=send.call_count;self.assertGreater(count,1)
            vd.deploy(ROOT,target,spec,{"test.png":asset})
            self.assertEqual(send.call_count,count)

    def test_remote_worker_to_local_editor_artifacts(self):
        # Exercise the deployed entry point's run manifest, real Studio graph builder
        # and downloader, then the desktop tar importer. No provider or GPU calls.
        import contextlib, threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        movie_bytes=movie(5)
        selected = ROOT/"selected-workflow.json"
        if selected.exists():
            graph=json.loads(selected.read_bytes())["graph"]
        elif (ROOT/"workflow.json").exists():
            graph=json.loads((ROOT/"workflow.json").read_bytes())
        else:
            self.skipTest("Supply the user H3 API workflow for the remote artifact check.")
        graphs=[]
        class FakeComfy(BaseHTTPRequestHandler):
            def reply(inner,obj):
                body=obj if isinstance(obj,bytes) else json.dumps(obj).encode()
                inner.send_response(200);inner.send_header("Content-Length",str(len(body)));inner.end_headers();inner.wfile.write(body)
            def do_GET(inner):
                if inner.path.startswith("/history/"):
                    t=inner.path.rsplit("/",1)[1]
                    inner.reply({t:{"status":{"status_str":"success"},"outputs":{"141":{"gifs":[{"filename":"video.mp4","type":"output"}]}}}})
                elif inner.path.startswith("/object_info/"):inner.reply({})
                elif inner.path.startswith("/view?"):inner.reply(movie_bytes)
                else:inner.send_error(404)
            def do_POST(inner):
                body=inner.rfile.read(int(inner.headers["Content-Length"]))
                if inner.path=="/prompt":
                    graphs.append(json.loads(body));inner.reply({"prompt_id":str(len(graphs))})
                elif inner.path=="/upload/image":inner.reply({"name":"uploaded.png"})
                else:inner.send_error(404)
            def log_message(inner,*args):pass
        server=ThreadingHTTPServer(("127.0.0.1",0),FakeComfy)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        spec=dict(self.spec,id=uuid.uuid4().hex,episode=self.eid,workflow={"name":"test","graph":graph},assets={})
        base=self.dir/"remote";run=base/"runs"/spec["id"];run.mkdir(parents=True)
        blobs=base/"blobs";blobs.mkdir(); pic=blobs/"pic";pic.write_bytes(b"test")
        sha=p.digest(pic);pic.rename(blobs/sha);spec["assets"]["tom.png"]=sha
        jobs=self.jobs(2)
        for _,shot,_ in jobs:shot.update(image="tom.png",refs=[],dialog=False,change="",approved=True)
        spec["jobs"]=jobs;p.atomic(run/"spec.json",spec)
        lock=types.SimpleNamespace(LOCK_EX=2,LOCK_NB=4,flock=lambda *a:None)
        with patch.dict(sys.modules,{"fcntl":lock}),patch.object(vw,"ready",return_value="http://127.0.0.1:"+str(server.server_port)):
            vw.execute(run)
        self.assertEqual(p.read(run/"status.json")["status"],"finished")
        self.assertEqual(len(graphs),2)
        local=self.dir/"desktop"
        for job in (run/"episodes").rglob("job.json"):
            rel=job.parent.relative_to(run/"episodes").as_posix();j=p.read(job)
            def transfer(target,cmd,out,timeout):
                with tarfile.open(fileobj=out,mode="w") as tar:
                    tar.add(job.parent,arcname=".")
            with patch.object(vd,"ssh",side_effect=transfer):
                vd.fetch_take({},dict(spec,release="a"*64),local,rel,j)
            dest=local/rel
            self.assertEqual(p.read(dest/"job.json")["status"],"done")
            self.assertEqual(p.digest(dest/"video.mp4"),j["sha256"])
            self.assertTrue((dest/"graph.json").is_file());self.assertTrue((dest/"tom.png").is_file())

    def test_missing_dependency_preflight_and_existing_template(self):
        wf={"graph":{"1":{"class_type":"X","inputs":{"model":"a.safetensors"}}}}
        def healthy(url,path):
            if path=="/system_stats":return {"system":{}}
            return {"X":{"input":{"required":{"model":[["a.safetensors"]]}}}}
        with patch("vast_worker.provision.install_nodes") as install:
            self.assertTrue(vw.ready({"json_api":healthy},wf).endswith("18188"))
            install.assert_not_called()
        with self.assertRaisesRegex(ValueError,"Still unavailable"):
            vw.ready({"json_api":lambda u,path: {"system":{}} if path=="/system_stats" else {}},wf,False)

    def test_provider_stop_is_stop_only(self):
        import os
        with patch.dict(os.environ,{"VAST_API_KEY":"test-secret"}),patch("vast_deploy.urlopen",return_value=io.BytesIO(b'{"success":true}')) as api:
            vd.request_instance_stop({"id":42})
            req=api.call_args.args[0]
            self.assertEqual(req.get_method(),"PUT")
            self.assertEqual(json.loads(req.data),{"state":"stopped"})
            self.assertNotIn("test-secret",req.full_url)

    def test_real_studio_builds_graph_with_selected_assets(self):
        ctx=runpy.run_path(str(ROOT/"studio.pyw"))["prepare_shot"].__globals__
        wf=ctx["active_workflow"]
        if not wf or wf.get("adapter")!="minimax-h3":
            self.skipTest("Select the user H3 workflow for the reference-graph check.")
        shot=dict(image="tom.png",refs=["emma.webp","tom.mp3"],prompt="Tom speaks to Emma",seconds=5,dialog=True,change="",id=str(uuid.uuid4()))
        for name in [shot["image"],*shot["refs"]]:
            path=self.dir/(name+".source");path.write_bytes(b"x");ctx["files"][name]=path
        ctx["upload"]=lambda url,f,name:f.name
        ctx["json_api"]=lambda *_:{}
        graph=ctx["prepare_shot"]("",self.dir,self.eid,shot,wf,dict(prompt=ctx["prompt"](shot,[])))
        refs=graph[wf["reference"]]["inputs"]
        self.assertEqual(graph[refs["ref_audios.ref_audio_0"][0]]["inputs"]["audio"],"tom.mp3")
        self.assertIn("<Audio 1>",graph[wf["prompt"]]["inputs"]["value"])
        self.assertIn("ref_images.ref_image_1",refs);self.assertEqual(graph[wf["duration"]]["inputs"]["value"],5)

if __name__=="__main__":
    unittest.main()

