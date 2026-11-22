"""Controls and reconnection for the existing direct Production queue."""
import re
import time
from http.client import HTTPException

import production
import vast_deploy
from workflow_manager import identity
from workflow_install_remote import redact, scan


class DirectSession:
    def __init__(self, ctx):
        self.ctx=ctx
        self.path=ctx["DATA"]/"production"/"direct-session.json"
        self.plan=production.read(self.path,{})
        if self.plan:
            view=self.plan["view"]
            if view["phase"] in ("Preparing ComfyUI","Rendering","Waiting for connection"):
                view.update(phase="Needs attention",error="Resume to reconcile the saved shot before submitting another.")
            ctx["session"]=view

    def save(self):
        production.atomic(self.path,self.plan)

    def start(self, view, jobs, workflow, target, url, retry=False):
        saved=self.plan.get("view",{})
        if saved.get("phase") in ("Paused","Needs attention") and self.plan.get("index",0)<len(self.plan["jobs"]):
            raise ValueError("Resume the saved production queue or stop it before starting a new session.")
        self.plan=dict(view=view,jobs=jobs,version=workflow["version"],index=0,control="run",
                       instance=identity(target) if target else None,url=url if not target else None,
                       retry=retry)
        view.update(rate=(target or {}).get("rate"),started=time.time())
        self.ctx["session"]=view
        self.save()

    def control(self, request):
        ctx=self.ctx
        action=request.get("action")
        view=self.plan.get("view")
        if not view or request.get("episode")!=view["episode"]:
            raise ValueError("This episode has no saved direct production session.")
        if action in ("pause","stop"):
            self.plan["control"]=action
            view["requestedAction"]=action
            if not ctx["running"]:
                view["phase"]="Paused" if action=="pause" else "Stopped"
            self.save()
            return {"ok":True}
        if action!="resume":
            raise ValueError("Unknown direct production control.")
        if ctx["running"] or ctx["production_busy"]() or ctx["workflow_manager"].busy():
            raise ValueError("Wait for the current production or installation.")
        if self.plan["index"]>=len(self.plan["jobs"]):
            raise ValueError("This queue is complete. Retry an individual failed shot.")
        connection=ctx["controller"].connection
        target=connection.target() if self.plan["instance"] else None
        if target and identity(target)!=self.plan["instance"]:
            raise ValueError("Connect the original instance to reconcile its queued shot.")
        wf=ctx["actions"].store.get(self.plan["version"])
        minutes=int(request.get("minutes",60))
        if not 2<=minutes<=1440:
            raise ValueError("Choose a session of 2–1440 minutes.")
        view.update(end=time.time()+60*minutes,phase="Preparing ComfyUI")
        view.pop("error",None)
        view.pop("requestedAction",None)
        view.pop("finished",None)
        self.plan.pop("validated",None)
        self.plan["control"]="run"
        self.save()
        ctx["run_direct"](connection.snapshot().get("url") if target else self.plan["url"],wf,target)
        return {"ok":True}

    def target_url(self, deadline, target, original):
        if not target:
            return original
        connection=self.ctx["controller"].connection
        while time.time()<deadline:
            try:
                selected=connection.target()
                url=connection.snapshot().get("url")
            except ValueError:
                url=None
            if url and identity(selected)==identity(target):
                return url
            self.plan["view"]["phase"]="Waiting for connection"
            time.sleep(min(2,max(0,deadline-time.time())))
        raise TimeoutError("Connection unavailable before the session limit. Resume the same prompt.")

    def run(self, url, wf, target=None):
        ctx=self.ctx
        view=self.plan["view"]
        deadline=view["end"]
        render_ctx=dict(ctx,technical_qc=ctx["take_quality"].check,direct=True,
                        instance=identity(target) if target else None)
        def on_current(fn):
            def call(_old_url,*args):
                current=self.target_url(deadline,target,url)
                view["phase"]="Rendering"
                return fn(current,*args)
            return call
        render_ctx["json_api"]=on_current(ctx["json_api"])
        render_ctx["prepare_shot"]=on_current(ctx["prepare_shot"])
        render_ctx["download_output"]=on_current(ctx["download_output"])
        cancel_unsupported=set()
        def cancel_prompt(ticket):
            if self.plan["control"]!="stop" or ticket in cancel_unsupported:
                return False
            if not re.fullmatch(r"[0-9a-f-]{36}",ticket):
                return False
            try:
                reply=render_ctx["json_api"](url,f"/api/jobs/{ticket}/cancel",{})
            except (OSError,HTTPException):
                return False
            except ValueError:
                # Older servers lack prompt-scoped cancellation. Never send a global interrupt.
                cancel_unsupported.add(ticket)
                return False
            if isinstance(reply,dict) and reply.get("cancelled") is True:
                return True
            cancel_unsupported.add(ticket)
            return False
        render_ctx["cancel_prompt"]=cancel_prompt
        try:
            jobs=self.plan["jobs"]
            # An already-submitted attempt must be reconciled before dependency
            # validation. Reconnecting only changes the tunnel, not its owner.
            for i in range(self.plan["index"],len(jobs)):
                if self.plan["control"]!="run":
                    view["phase"]="Paused" if self.plan["control"]=="pause" else "Stopped"
                    return
                if time.time()>=deadline-10:
                    raise TimeoutError("Session time limit reached. Resume to reconcile outstanding work.")
                eid,shot,rules=jobs[i]
                view.update(current=shot["id"],phase="Rendering")
                self.save()
                latest=next(iter(sorted((ctx["DATA"]/eid/shot["id"]).glob("*/job.json"),reverse=True)),None)
                prior=production.read(latest,{}) if latest else {}
                same_revision=prior.get("revision")==shot["revision"]
                in_flight=same_revision and prior.get("status") in ("submitting","queued","waiting","uncertain")
                if same_revision and prior.get("status")=="cancelled":
                    queued=render_ctx["json_api"](url,"/queue")
                    if any(row[1]==prior["ticket"] for row in queued["queue_running"]+queued["queue_pending"]):
                        raise RuntimeError("ComfyUI is still cancelling the original prompt. Resume after it leaves the queue.")
                if not in_flight and i==self.plan["index"] and not self.plan.get("validated"):
                    if target:
                        vast_deploy.prepare_direct(render_ctx,target,jobs[i:],wf,lambda:deadline-time.time(),repair=False)
                    else:
                        info=ctx["json_api"](url,"/object_info")
                        from workflow_profiles import _upstream
                        graph={k:wf["graph"][k] for k in _upstream(wf["graph"],[wf["output"]])}
                        dependencies=scan(graph,info)
                        if dependencies["nodes"] or any(m["status"]!="Installed" for m in dependencies["models"]):
                            raise ValueError("Resolve the missing workflow dependencies before production.")
                    self.plan["validated"]=True
                result=production.render(render_ctx,url,deadline,eid,shot,rules,wf,2,lambda:None,self.plan["retry"])
                if result in ("waiting","uncertain"):
                    raise RuntimeError("The original prompt is unresolved. Resume on the same instance to reconcile it.")
                if result=="cancelled":
                    view["phase"]="Stopped"
                    return
                if result=="error":
                    view["failed"]+=1
                if result=="done":
                    view["completed"]+=1
                self.plan["index"]=i+1
                self.save()
            if self.plan["control"]=="stop":
                view["phase"]="Stopped"
            else:
                view["phase"]="Finished" if not view["failed"] else "Needs attention"
        except Exception as exc:
            view.update(phase="Needs attention",error=redact(str(exc))[:900])
        finally:
            with ctx["LOCK"]:
                view.pop("current",None)
                view["finished"]=time.time()
                self.save()
                ctx["running"]=False
