let workflowLive=null, workflowPolling=false, workflowTerminalOpen=false;
let pendingAutoDeployVersion='', autoDeployAttempted=new Set();
let lastThumbsSignature='';
const terminalWorkflowStates=['installed','ready','incompatible','needs attention','failed','cancelled','timed out'];

const selectedWorkflow=()=>currentProfile(editingProfile)||currentProfile();
const workflowCall=obj=>post('/workflow-manager',{version:selectedWorkflow()?.version??null,...obj});
const timeLabel=s=>!Number.isFinite(s)?'—':(s<60?Math.round(s)+' s':Math.floor(s/60)+'m '+Math.round(s%60)+'s');
const workflowStatus=()=>workflowLive?.version===(selectedWorkflow()?.version??null)?workflowLive.data:null;

function queueAutoDeploy(version){
    pendingAutoDeployVersion=version||'';
    if(pendingAutoDeployVersion)tryAutoDeploy();
}

async function tryAutoDeploy(forceAlign=false){
    const p=selectedWorkflow();
    if(!p||!p.packaged||!p.executable||p.issues?.length)return;
    const version=p.version;
    if(!forceAlign&&pendingAutoDeployVersion!==version)return;
    if(state.connection?.state!=='Ready'||state.running)return;
    const data=workflowStatus();
    const op=data?.installation;
    if(op?.status&&!terminalWorkflowStates.includes(op.status))return;
    const inst=String(state.connection?.instance??'ssh');
    const key=version+'@'+inst+(forceAlign?'#align':'');
    if(!forceAlign&&autoDeployAttempted.has(key))return;
    autoDeployAttempted.add(key);
    pendingAutoDeployVersion='';
    try{
        await post('/workflow-manager',{
            action:'install',
            version,
            confirmed:true,
            timeout:60,
            instance:state.connection.instance??null,
            alignComfyui:Boolean(forceAlign),
        });
        reportStatus('Package uploaded to /workspace — extracting and installing automatically.');
        await pollWorkflow();
    }catch(err){
        reportError(err);
    }
}

function workflowWorkspace(){
    lastThumbsSignature='';
    return '<div id="wmUnified">'+
        workflowPanel()+
        '<section class="panel" id="wmInstallPanel"><h2>Installation Monitor</h2>'+
        '<div id="wmInstallSummary">'+installSummaryHtml()+'</div>'+
        '<details id="wmTerminalLog" '+(workflowTerminalOpen?'open':'')+'><summary>Live Terminal Logs</summary>'+
        '<pre id="wmTerminalLogPre" class="preserve-lines">'+esc(workflowStatus()?.installation?.log||'No terminal output yet.')+'</pre></details>'+
        '</section>'+
        '<section class="panel" id="wmProdPanel"><h2>Production Monitor</h2>'+
        '<div id="wmProdControls">'+productionControlsHtml()+'</div>'+
        '<div id="wmThumbsWrap">'+producedThumbsHtml()+'</div>'+
        '</section>'+
        '<dialog id="wmVideoZoom" class="wm-zoom-dialog">'+
        '<div class="top"><strong id="wmZoomTitle"></strong><button type="button" data-wm-zoom-close>Close</button></div>'+
        '<video id="wmZoomVideo" controls playsinline></video>'+
        '</dialog></div>';
}

function extractMetrics(data, profile){
    const inst=data?.installation||{};
    const m=inst.metrics||{};
    const report=inst.report||data?.environmentState?.report||{};
    const fallbackTotal=(profile?.packageNodes?.length||0)+(profile?.packageModels?.length||0);
    const reportNodes=Array.isArray(report.nodeRepos)?report.nodeRepos:[];
    const reportModels=Array.isArray(report.models)?report.models:[];
    const doneFromReport=reportNodes.filter(r=>r.status==='Installed').length+reportModels.filter(x=>x.status==='Installed').length;
    const total=Number.isFinite(m.filesTotal)&&m.filesTotal>0?m.filesTotal:(reportNodes.length+reportModels.length||fallbackTotal);
    const completed=Number.isFinite(m.filesCompleted)?m.filesCompleted:doneFromReport;
    const speed=Number.isFinite(m.downloadSpeedMiBs)?m.downloadSpeedMiBs:0;
    const currentFile=m.currentFile||'';
    const percent=Number.isFinite(m.filePercent)?m.filePercent:(total>0?Math.round(completed*100/total):0);
    return {total,completed,speed,currentFile,percent};
}

function managerSyncHtml(sync){
    if(!sync||typeof sync!=='object')return '';
    const missing=Array.isArray(sync.missingNodepacks)?sync.missingNodepacks:[];
    const installed=Array.isArray(sync.installedNodepacks)?sync.installedNodepacks:[];
    const updatable=Array.isArray(sync.updatableNodepacks)?sync.updatableNodepacks:[];
    const updated=Array.isArray(sync.updatedNodepacks)?sync.updatedNodepacks:[];
    const skipped=Array.isArray(sync.skippedProtected)?sync.skippedProtected:[];
    const errs=Array.isArray(sync.errors)?sync.errors:[];
    const impErrs=Array.isArray(sync.importErrors)?sync.importErrors:[];
    const remMissing=Array.isArray(sync.remainingMissingClasses)?sync.remainingMissingClasses:[];
    const parts=[];
    parts.push('<p class="muted"><strong>ComfyUI-Manager</strong>'+(sync.managerVersion?' ('+esc(sync.managerVersion)+')':'')+' · Phase: <strong>'+esc(sync.phase||'idle')+'</strong> · Missing: '+missing.length+' · Installed: '+installed.length+' · Updates: '+updated.length+'/'+updatable.length+(sync.restarted?' · ComfyUI restarted':'')+'</p>');
    if(sync.currentInstalling)parts.push('<p class="muted">Installing nodepack: <strong>'+esc(sync.currentInstalling)+'</strong></p>');
    if(sync.currentUpdating)parts.push('<p class="muted">Updating nodepack: <strong>'+esc(sync.currentUpdating)+'</strong></p>');
    if(installed.length)parts.push('<p class="muted">Installed nodepacks: '+esc(installed.join(', '))+'</p>');
    if(updated.length)parts.push('<p class="muted">Updated nodepacks: '+esc(updated.join(', '))+'</p>');
    if(skipped.length)parts.push('<ul class="muted">'+skipped.map(s=>'<li>Protected (skipped): <strong>'+esc(s.name)+'</strong> — '+esc(s.reason)+'</li>').join('')+'</ul>');
    if(remMissing.length)parts.push('<p class="danger">Remaining missing nodes: '+esc(remMissing.join(', '))+'</p>');
    if(errs.length)parts.push('<ul class="danger">'+errs.map(e=>'<li>'+esc(e.nodepack)+' ('+esc(e.operation)+'): '+esc(e.error)+'</li>').join('')+'</ul>');
    if(impErrs.length)parts.push('<ul class="danger">'+impErrs.map(e=>'<li>Import error · '+esc(e.nodepack)+': '+esc(e.error)+'</li>').join('')+'</ul>');
    return parts.join('');
}

function installSummaryHtml(){
    const p=selectedWorkflow(), data=workflowStatus(), i=data?.installation||{};
    const metrics=extractMetrics(data,p);
    const envState=data?.environmentState;
    const report=i.report||envState?.report||{};
    const incompat=Array.isArray(report.incompatibilities)?report.incompatibilities:(envState?.incompatibilities||[]);
    const mismatch=report.comfyuiMismatch;
    const active=Boolean(i.status&&!terminalWorkflowStates.includes(i.status));
    const canInstall=Boolean(p?.packaged&&p.executable&&!p.issues?.length&&state.connection?.state==='Ready'&&!state.running&&!active);
    const canSyncNodes=Boolean(p?.executable&&!p.issues?.length&&state.connection?.state==='Ready'&&!state.running&&!active);
    const statusText=i.status||(data?.installationStatus&&data.installationStatus!=='not_checked'?data.installationStatus:(pendingAutoDeployVersion===p?.version?'Waiting for Vast connection to auto-install':'Idle'));
    const prodReady=data?.productionReadiness==='verified'&&data?.verifiedTake
        ?'Verified ('+esc(String(data.verifiedTake.sha256||'').slice(0,10))+')'
        :'Pending first verified video';
    const stages=Array.isArray(data?.phases)?[...data.phases]:[];
    if(i.stages?.SageAttention&&!stages.includes('SageAttention'))stages.splice(stages.indexOf('ComfyUI restart'),0,'SageAttention');
    if(i.stages?.['ComfyUI-Manager sync']&&!stages.includes('ComfyUI-Manager sync'))stages.splice(stages.indexOf('Final verification'),0,'ComfyUI-Manager sync');
    return '<div class="wm-metrics">'+
        '<div class="wm-stat"><small>Status</small><strong>'+esc(statusText)+'</strong></div>'+
        '<div class="wm-stat"><small>Files</small><strong>'+metrics.completed+' / '+metrics.total+'</strong></div>'+
        '<div class="wm-stat"><small>Download speed</small><strong>'+(metrics.speed>0?metrics.speed.toFixed(1)+' MiB/s':'—')+'</strong></div>'+
        '<div class="wm-stat"><small>Elapsed</small><strong>'+(i.elapsed!=null?timeLabel(i.elapsed):'—')+'</strong></div>'+
        '<div class="wm-stat"><small>Production readiness</small><strong>'+prodReady+'</strong></div>'+
        '</div>'+
        (metrics.currentFile?'<p class="muted">Active file: <strong>'+esc(metrics.currentFile)+'</strong> ('+metrics.percent.toFixed(0)+'%)</p><progress class="wm-progress" value="'+metrics.percent+'" max="100"></progress>':'')+
        (i.message?'<p>'+esc(i.message)+'</p>':'')+
        managerSyncHtml(report.managerSync)+
        (incompat.length?'<ul class="danger">'+incompat.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>':'')+
        '<div class="row">'+
        '<button type="button" class="primary" data-wm="deploy" '+(canInstall?'':'disabled')+'>Upload &amp; Install on /workspace</button>'+
        '<button type="button" data-wm="sync-nodes" '+(canSyncNodes?'':'disabled')+'>Sync Custom Nodes</button>'+
        (mismatch?.canAutoAlign&&canInstall?'<button type="button" data-wm="align-install">Align ComfyUI &amp; Install</button>':'')+
        (active?'<button type="button" class="danger" data-wm="cancel" data-wm-instance="'+esc(i.instance)+'">Cancel</button>':'')+
        '<button type="button" class="danger" data-wm="reset-install" data-wm-instance="'+esc(i.instance||data?.connectedInstance||'')+'" '+(active||(!i.status&&!envState)?'disabled':'')+'>Reset</button>'+
        '</div>'+
        (i.status&&stages.length?'<table class="wm-table"><thead><tr><th>Stage</th><th>Status</th><th>Progress</th></tr></thead><tbody>'+
            stages.map(name=>{
                const st=i.stages?.[name]||{};
                return '<tr><td>'+esc(name)+'</td><td>'+esc(st.status||'Pending')+'</td><td>'+esc(st.message||'')+'</td></tr>';
            }).join('')+'</tbody></table>':'');
}

function productionControlsHtml(){
    const session=state.session?.episode===ep.id?state.session:{};
    const data=workflowStatus();
    const phases=data?.installation?.status;
    const installing=Boolean(phases&&!terminalWorkflowStates.includes(phases));
    const hasApproved=ep.shots.some(s=>s.approved);
    const canStart=!installing&&hasApproved&&Boolean(currentProfile()?.executable)&&state.connection?.state==='Ready'&&!state.running;
    const canStop=Boolean(state.running||(session.episode&&!['Finished','Stopped'].includes(session.phase)));
    const canSkip=Boolean(state.running||(session.episode&&!['Finished','Stopped'].includes(session.phase)));
    const targetRegenShot=ep.shots.find(s=>s.id===session.current)||selectedShot()||ep.shots.find(s=>s.approved);
    const canRegen=Boolean(!installing&&!state.running&&state.connection?.state==='Ready'&&currentProfile()?.executable&&targetRegenShot?.approved);
    let doneCount=0,failedCount=0;
    for(const shot of ep.shots){
        const r=versions(shot).at(-1);
        if(r?.status==='done')doneCount++;
        else if(r?.status==='error')failedCount++;
    }
    return '<p><strong>'+esc(ep.title)+'</strong> · '+ep.shots.length+' shots · '+doneCount+' produced'+(failedCount?' · '+failedCount+' failed':'')+' · Status: <strong>'+esc(session.phase||'Idle')+'</strong>'+
        (state.running&&session.requestedAction?' ('+esc(session.requestedAction)+'…)':'')+'</p>'+
        (session.error?'<p class="danger">'+esc(session.error)+'</p>':'')+
        '<div class="row">'+
        '<button type="button" class="primary" data-wm="start" '+(canStart?'':'disabled')+'>Start Production</button>'+
        '<button type="button" class="danger" data-wm="stop" '+(canStop?'':'disabled')+'>Stop Production</button>'+
        '<button type="button" data-wm="skip" '+(canSkip?'':'disabled')+'>Skip Shot</button>'+
        '<button type="button" data-wm="regenerate" data-wm-shot="'+esc(targetRegenShot?.id||'')+'" '+(canRegen?'':'disabled')+'>Regenerate Shot</button>'+
        '</div>';
}

function producedItems(){
    const items=[];
    ep.shots.forEach((shot,index)=>{
        const takes=versions(shot).filter(v=>v.video);
        const latest=takes.at(-1);
        if(latest){
            items.push({shot,index,take:latest,label:heading(shot,index)});
        }
    });
    return items;
}

function producedThumbsSignature(){
    return ep.id+'|'+producedItems().map(x=>x.shot.id+':'+x.take.version+':'+x.take.video).join(',')+'|'+state.running;
}

function producedThumbsHtml(){
    const items=producedItems();
    lastThumbsSignature=producedThumbsSignature();
    if(!items.length)return '<p class="muted">No produced shots yet.</p>';
    const canRegen=!state.running&&state.connection?.state==='Ready'&&Boolean(currentProfile()?.executable);
    return '<div class="wm-thumbs">'+items.map(({shot,take,label})=>
        '<figure class="wm-thumb-card">'+
        '<video muted preload="metadata" src="'+esc(take.video)+'"></video>'+
        '<figcaption><strong>'+label+'</strong>'+(take.duration?' <small>'+Number(take.duration).toFixed(1)+'s</small>':'')+'</figcaption>'+
        '<div class="row">'+
        '<button type="button" data-wm-zoom="'+esc(take.video)+'" data-wm-zoom-title="'+esc(label)+'">Enlarge</button>'+
        '<button type="button" data-wm="regenerate" data-wm-shot="'+esc(shot.id)+'" '+(canRegen&&shot.approved?'':'disabled')+'>Regenerate</button>'+
        '</div></figure>'
    ).join('')+'</div>';
}

function updateTerminalLogPreservingScroll(nextText){
    const pre=$('#wmTerminalLogPre');
    if(!pre)return;
    const text=nextText||'No terminal output yet.';
    if(pre.textContent===text)return;
    const atBottom=(pre.scrollHeight-pre.scrollTop-pre.clientHeight)<36;
    const prevTop=pre.scrollTop;
    pre.textContent=text;
    if(atBottom){
        pre.scrollTop=pre.scrollHeight;
    }else{
        pre.scrollTop=prevTop;
    }
}

async function pollWorkflow(){
    if(!$('#wmUnified')||workflowPolling)return;
    workflowPolling=true;
    const version=selectedWorkflow()?.version??null;
    const sameView=()=>version===(selectedWorkflow()?.version??null)&&Boolean($('#wmUnified'));
    try{
        const data=await workflowCall({action:'status',monitor:true});
        if(!sameView())return;
        workflowLive={version,data};
        const sum=$('#wmInstallSummary');
        if(sum)sum.innerHTML=installSummaryHtml();
        updateTerminalLogPreservingScroll(data.installation?.log);
        const ctrl=$('#wmProdControls');
        if(ctrl)ctrl.innerHTML=productionControlsHtml();
        const nextSig=producedThumbsSignature();
        if(nextSig!==lastThumbsSignature){
            const wrap=$('#wmThumbsWrap');
            if(wrap)wrap.innerHTML=producedThumbsHtml();
        }
        if(pendingAutoDeployVersion===version&&state.connection?.state==='Ready'){
            tryAutoDeploy(false);
        }
    }catch(error){
        if(sameView()&&$('#wmInstallSummary'))$('#wmInstallSummary').innerHTML='<p class="danger">Unable to read workflow status: '+esc(error.message)+'</p>';
    }finally{
        workflowPolling=false;
    }
}
setInterval(pollWorkflow,2200);

document.addEventListener('toggle',event=>{
    if(event.target.id==='wmTerminalLog')workflowTerminalOpen=event.target.open;
},true);

document.addEventListener('click',async event=>{
    const zoomBtn=event.target.closest('button[data-wm-zoom]');
    if(zoomBtn){
        const dialog=$('#wmVideoZoom'), video=$('#wmZoomVideo'), title=$('#wmZoomTitle');
        if(dialog&&video){
            title.textContent=zoomBtn.dataset.wmZoomTitle||'Shot Preview';
            video.src=zoomBtn.dataset.wmZoom;
            dialog.showModal();
            video.play().catch(()=>{});
        }
        return;
    }
    if(event.target.closest('button[data-wm-zoom-close]')){
        const dialog=$('#wmVideoZoom'), video=$('#wmZoomVideo');
        if(video){video.pause();video.removeAttribute('src');video.load()}
        if(dialog?.open)dialog.close();
        return;
    }
    const button=event.target.closest('button[data-wm]');
    if(!button)return;
    const action=button.dataset.wm;
    button.disabled=true;
    try{
        if(action==='deploy'||action==='align-install'){
            const p=selectedWorkflow();
            if(!p)throw Error('Import a Workflow Package (.zip) first.');
            autoDeployAttempted.delete(p.version+'@'+String(state.connection?.instance??'ssh'));
            pendingAutoDeployVersion=p.version;
            await tryAutoDeploy(action==='align-install');
            return;
        }
        if(action==='sync-nodes'){
            await workflowCall({action:'sync-nodes',timeout:30});
            reportStatus('Running ComfyUI-Manager custom node sync…');
            await pollWorkflow();
            return;
        }
        if(action==='cancel'){
            await workflowCall({action:'cancel',instance:button.dataset.wmInstance});
            await pollWorkflow();
            return;
        }
        if(action==='reset-install'){
            if(!confirm('Reset workflow installation state on this instance? Your Workflow Package and story will be kept.'))return;
            await workflowCall({action:'reset-install',instance:button.dataset.wmInstance||undefined});
            autoDeployAttempted.clear();
            await pollWorkflow();
            reportStatus('Workflow installation state reset.');
            return;
        }
        if(action==='start'){
            await flushEdits();
            const result=await post('/start',{episode:ep.id});
            reportSubmission(result);
        }
        if(action==='stop'||action==='skip'){
            await post('/direct-production',{action,episode:ep.id});
        }
        if(action==='regenerate'){
            await flushEdits();
            const shotId=button.dataset.wmShot;
            if(!shotId)throw Error('Select an approved shot to regenerate.');
            const result=await post('/direct-production',{action:'regenerate',episode:ep.id,shot:shotId});
            reportSubmission(result);
        }
        if(['start','stop','skip','regenerate'].includes(action)){
            state=await(await fetch('/status')).json();
            await pollWorkflow();
        }
    }catch(error){reportError(error)}
    finally{if(button.isConnected)button.disabled=false}
});

document.addEventListener('close',event=>{
    if(event.target.id==='wmVideoZoom'){
        const video=$('#wmZoomVideo');
        if(video){video.pause();video.removeAttribute('src');video.load()}
    }
},true);
