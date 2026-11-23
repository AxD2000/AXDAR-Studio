let workflowTab='Setup', workflowLive=null, workflowPolling=false, workflowLogOpen=false, workflowTerminalOpen=false;
const workflowTabs=['Setup','Dependencies','Installation Monitor','Production Monitor'];
const terminalWorkflowStates=['ready','needs attention','failed','cancelled','timed out'];
const workflowDrafts=new Map();
const selectedWorkflow=()=>currentProfile(editingProfile)||currentProfile();
const workflowCall=obj=>post('/workflow-manager',{version:selectedWorkflow()?.version??null,...obj});
const timeLabel=s=>!Number.isFinite(s)?'—':(s<60?Math.round(s)+' s':Math.floor(s/60)+'m '+Math.round(s%60)+'s');
const moneyLabel=value=>'$'+Number(value).toFixed(3);
const workflowStatus=()=>workflowLive?.version===(selectedWorkflow()?.version??null)?workflowLive.data:null;
const hfTokenStatus=saved=>saved===true?'Token saved in Windows Credential Manager.':
    saved===false?'No Hugging Face token saved.':'Checking Windows Credential Manager…';

function installReason(){
    const p=selectedWorkflow(),operation=workflowStatus()?.installation;
    if(!p)return 'Import a ComfyUI workflow first.';
    if(!p.executable)return 'Prepare the original JSON on the connected ComfyUI instance first.';
    if(!p.mapping?.output)return 'Select the final MP4 output in Setup.';
    if(state.connection?.state!=='Ready')return 'Connect to Vast to install dependencies.';
    if(state.running||['running','deploying','starting worker','checking ComfyUI and requirements'].includes(state.production?.status))
        return 'Wait for the current production session.';
    if(operation?.status&&!terminalWorkflowStates.includes(operation.status))
        return operation.mode==='scan'?'Checking dependencies…':'An installation is already running.';
    return '';
}

function installActions(){
    const reason=installReason();
    return '<div class="row"><button class="primary" data-wm="preview" '+(reason?'disabled':'')+'>Start Installation</button></div>'+
        '<p id="wmActionNote" class="muted" '+(reason?'':'hidden')+'>'+esc(reason)+'</p>';
}

function storedInstallDraft(data=workflowStatus()){
    const version=selectedWorkflow()?.version;
    return workflowDrafts.get(version)||{script:data?.savedScript||'',timeout:30,restart:false};
}

function saveInstallDraft(){
    const p=selectedWorkflow(),field=$('#wmScript');
    if(p&&field)workflowDrafts.set(p.version,{script:field.value,timeout:$('#wmTimeout').value,restart:$('#wmRestart').checked});
}

function workflowWorkspace(){
    const subnav='<div class="toolbar">'+workflowTabs.map(label=>'<button data-wm-tab="'+label+'" aria-pressed="'+(workflowTab===label)+'">'+label+'</button>').join('')+'</div>';
    let content='';
    if(workflowTab==='Setup'){
        const draft=storedInstallDraft();
        content=workflowPanel()+
        '<section class="panel"><h2>Workflow installation</h2>'+installActions()+'</section>'+
        '<section class="panel"><h2>Hugging Face</h2><p id="wmTokenStatus" role="status">'+hfTokenStatus(workflowStatus()?.hfTokenSaved)+'</p>'+
        '<label>Hugging Face Access Token<input id="wmToken" type="password" autocomplete="off" placeholder="hf_…"></label>'+
        '<div class="row"><button data-wm="showToken" aria-controls="wmToken">Show</button><button class="primary" data-wm="save-token">Save Token</button>'+
        '<button data-wm="token">Test Token</button></div></section>'+
        '<section class="panel"><h2>Custom Installation Commands (Bash)</h2><label>Optional script<textarea id="wmScript" rows="10" spellcheck="false">'+esc(draft.script)+'</textarea></label>'+
        '<label>Import .sh file<input id="wmScriptFile" type="file" accept=".sh,text/x-shellscript"></label>'+
        '<div class="form-pair"><label>Timeout (minutes)<input id="wmTimeout" type="number" min="1" max="180" value="'+esc(draft.timeout)+'" required></label>'+
        '<label><input id="wmRestart" type="checkbox" '+(draft.restart?'checked':'')+'> Restart ComfyUI after this script even if all nodes are registered</label></div>'+
        '<p>Scripts run over SSH on the connected instance. Use the HF_TOKEN environment variable for authenticated downloads.</p></section>';
    }
    if(workflowTab==='Dependencies'){
        content='<section class="panel"><h2>Dependency verification</h2>'+installActions()+
            '<button data-wm="scan">Check current instance</button> <button data-wm-tab="Setup">Open Setup</button>'+
            '<div id="wmLive">'+dependencyView()+'</div></section>';
    }
    if(workflowTab==='Installation Monitor'){
        content='<section class="panel"><h2>Installation monitor</h2>'+installActions()+'<div id="wmLive">'+installationView()+'</div></section>';
    }
    if(workflowTab==='Production Monitor'){
        content='<section class="panel"><h2>Video production monitor</h2><div id="wmLive">'+productionMonitorView()+'</div>'+
            '<details id="wmShotLogs" '+(workflowLogOpen?'open':'')+' hidden><summary>Shot logs</summary><pre class="preserve-lines" id="wmShotLogText"></pre></details></section>';
    }
    return subnav+content+'<dialog id="wmConfirm"></dialog>';
}

function dependencyReport(){
    const data=workflowStatus(),op=data?.installation;
    const live=op?.version===selectedWorkflow()?.version&&op?.instance===data?.connectedInstance?op?.report:null;
    return Array.isArray(live?.models)?live:data?.verification?.report;
}

function dependencyView(){
    const p=selectedWorkflow(),report=dependencyReport();
    if(!p)return '<p>Import a workflow in Setup to see its dependencies.</p>';
    if(!p.executable)return '<p>Prepare this workflow to check its dependencies.</p>';
    if(!Array.isArray(report?.models))return '<p>Check the connected instance to verify '+esc(p.name)+'.</p>';
    const nodes=Array.isArray(report.nodes)?report.nodes:[];
    const outstanding=report.models.filter(m=>m.status!=='Installed');
    const sageMissing=report.sageAttention==='Missing';
    const space=report.diskFreeBytes!=null&&report.downloadBytes!=null?
        '<p>'+((report.downloadBytes||0)/1e9).toFixed(2)+' GB verified downloads · '+
        (report.diskFreeBytes/1e9).toFixed(2)+' GB available'+
        (report.downloadBytes>report.diskFreeBytes?' · Insufficient disk space':'')+'</p>':'';
    return '<p>'+esc(p.name)+'</p><p>'+(nodes.length||outstanding.length||sageMissing?
        nodes.length+' node classes and '+outstanding.length+' model files need attention on this instance.'+
        (sageMissing?' SageAttention needs repair.':''):
        'All required dependencies are available on this instance.')+'</p><h3>Custom nodes</h3>'+
        (nodes.length?'<ul>'+nodes.map(node=>'<li>'+esc(node)+'</li>').join('')+'</ul>':'<p>All required classes registered.</p>')+
        (report.sageAttention?'<p>SageAttention · '+esc(report.sageAttention)+'</p>':'')+
        '<h3>Model files</h3>'+space+'<table><thead><tr><th>File</th><th>Status</th><th>Verified source</th></tr></thead><tbody>'+
        report.models.map(m=>'<tr><td>'+esc(m.name||'Unknown file')+'</td><td>'+esc(m.status||'Pending')+(m.error?' · '+esc(m.error):'')+'</td><td>'+
            (m.source?.repo?esc(m.source.repo)+(m.source.revision?' @ '+esc(String(m.source.revision).slice(0,12)):''):'—')+'</td></tr>').join('')+'</tbody></table>';
}

function installationView(){
    const data=workflowStatus(),i=data?.installation;
    const parked=data?.otherInstallations?
        '<p class="muted">Unfinished installations on other instances are saved for when you reconnect to those instances.</p>':'';
    if(!i?.status)return '<p>No installation started on this instance.</p>'+parked;
    const terminal=terminalWorkflowStates.includes(i.status);
    const scan=i.mode==='scan';
    const scanDone=scan&&['ready','needs attention'].includes(i.status);
    const label=scanDone?(i.status==='ready'?'Dependency scan complete':'Dependency scan complete · items missing'):
        (scan?'Dependency scan · ':'Installation · ')+i.status;
    const profile=workflowCatalog.profiles.find(item=>item.version===i.version);
    const cost=i.rate!=null?' · '+moneyLabel(i.elapsed/3600*i.rate)+' estimated':'';
    const stages=[...(data.phases||[])];
    if(i.stages?.SageAttention&&!stages.includes('SageAttention'))stages.splice(stages.indexOf('ComfyUI restart'),0,'SageAttention');
    return (profile?'<p>'+esc(profile.name)+'</p>':'')+
        '<p role="status">'+esc(label)+' · '+timeLabel(i.elapsed)+cost+(!scanDone&&i.exitCode!=null?' · '+(scan?'Scan exit ':'Installer exit ')+i.exitCode:'')+(i.bashExitCode!=null?' · Bash exit '+i.bashExitCode:'')+'</p>'+
        (scanDone?'<p>Open Dependencies for the results, or choose Start Installation.</p>':'')+
        '<p>'+esc(i.message||'')+'</p>'+
        (!terminal?'<button data-wm="cancel" data-wm-instance="'+esc(i.instance)+'">'+(scan?'Cancel Scan':'Cancel Installation')+'</button>':'')+
        parked+
        '<table><thead><tr><th>Stage</th><th>Status</th><th>Elapsed</th><th>Progress</th></tr></thead><tbody>'+
        stages.map(name=>{
            const stage=i.stages?.[name]||{},t=(stage.status==='Running'?Date.now()/1000:stage.updated)-stage.started;
            const status=scanDone?(stage.status==='Failed'?'Items missing':stage.status||'Skipped'):stage.status||'Pending';
            return '<tr><td>'+esc(name)+'</td><td>'+esc(status)+'</td><td>'+timeLabel(t)+'</td><td>'+
                (stage.total&&Number.isFinite(stage.count)?'<progress value="'+stage.count+'" max="'+stage.total+'"></progress> '+stage.count+'/'+stage.total:
                stage.status==='Running'?'<progress aria-label="In progress"></progress> ':'')+esc(stage.message||'')+'</td></tr>';
        }).join('')+'</tbody></table>'+
        '<details id="wmTerminalLog" '+(workflowTerminalOpen?'open':'')+'><summary>Live Terminal Logs</summary><pre class="preserve-lines">'+
        esc(i.log||'No terminal output yet.')+'</pre></details>';
}

function productionMonitorView(){
    const session=state.session?.episode===ep.id?state.session:{};
    const remote=state.production?.episode===ep.id?state.production:{};
    const telemetry=workflowLive?.data?.telemetry||{};
    const rate=session.rate??remote.rate, elapsed=session.started?((session.finished||Date.now()/1000)-session.started):remote.elapsed;
    const phases=workflowLive?.data?.installation?.status;
    const installing=phases&&!['ready','needs attention','failed','cancelled','timed out'].includes(phases);
    const blocked=installing||!ep.shots.some(s=>s.approved)||!currentProfile()?.executable||state.connection?.state!=='Ready'||state.running;
    const directControl=(label,action,disabled)=>'<button data-wm="'+action+'" '+(disabled?'disabled':'')+'>'+label+'</button>';
    let counts={done:0,pending:0,failed:0},observed='';
    const timings=[];
    const rows=ep.shots.map((shot,index)=>{
        const r=versions(shot).find(t=>t.revision===shot.revision);
        const rendering=Boolean(r?.promptId&&telemetry.running?.includes(r.promptId));
        const queued=Boolean(r?.promptId&&telemetry.queued?.includes(r.promptId));
        const done=r?.status==='done',failed=r?.status==='error';
        if(done)counts.done++;else if(failed)counts.failed++;else if(shot.approved)counts.pending++;
        if(done&&r.workflowVersion===workflowCatalog.active&&r.renderSeconds>0)timings.push(r.renderSeconds);
        const status=done?(r.video?'Completed':'Completed remotely'):failed?'Failed':rendering?'Generating':queued?'Queued':r?.status||(shot.approved?'Pending':'Unapproved');
        if(rendering)observed=' · '+heading(shot,index)+' · Prompt '+esc(r.promptId)+' · '+timeLabel(Date.now()/1000-r.started);
        const output=r?.video?'<a href="'+esc(r.video)+'" target="_blank" rel="noopener">Preview</a>':'—';
        const logs=r?'<button data-wm="logs" data-wm-shot="'+esc(shot.id)+'" data-wm-version="'+esc(r.version)+'">View Shot Logs</button>':'';
        const retry=failed&&!state.running?'<button data-wm="retry" data-wm-shot="'+esc(shot.id)+'">Retry Failed Shot</button>':'';
        return '<tr><td>'+heading(shot,index)+'</td><td>'+esc(status)+'</td><td>'+
            timeLabel(r?.renderSeconds??(rendering?Date.now()/1000-r.started:null))+'</td><td>'+output+'</td><td>'+logs+retry+'</td></tr>';
    });
    const eta=timings.length>=3&&counts.pending&&state.running&&session.episode?
        '<p>Estimated remaining '+timeLabel(timings.reduce((a,b)=>a+b,0)/timings.length*counts.pending)+
        ' from '+timings.length+' completed shots.</p>':'';
    return '<h3>'+esc(ep.title)+'</h3><p>'+ep.shots.length+' total · '+counts.done+' complete · '+counts.pending+' pending · '+counts.failed+' failed</p>'+
        '<p role="status">'+esc(session.phase||remote.status||'Idle')+observed+
        (state.running&&session.requestedAction?' · '+(session.requestedAction==='pause'?'Pausing after current shot':'Stopping production'):'')+'</p>'+
        (session.error?'<p class="danger">'+esc(session.error)+'</p>':'')+eta+
        (rate!=null?'<p>'+moneyLabel(rate)+'/hour'+(elapsed!=null?' · '+moneyLabel(elapsed/3600*rate)+' estimated session cost':'')+'</p>':'')+
        (telemetry.gpus||[]).map(g=>'<p>'+esc(g.name||'GPU')+(g.vram_total!=null&&g.vram_free!=null?' · '+((g.vram_total-g.vram_free)/1073741824).toFixed(1)+'/'+(g.vram_total/1073741824).toFixed(1)+' GiB VRAM':'')+
          (g.utilization!=null?' · '+Number(g.utilization)+'% utilization':'')+'</p>').join('')+
        (currentProfile()?.format==='ComfyUI UI JSON'?'<p>Start Production first verifies the original graph at its authored duration.</p>':'')+
        '<div class="row">'+directControl('Start Production','start',blocked)+
        directControl('Pause After Current Shot','pause',!state.running||!session.episode)+
        directControl('Resume Production','resume',state.running||!['Paused','Needs attention','Stopped'].includes(session.phase))+
        directControl('Stop Production','stop',!session.episode||['Finished','Stopped'].includes(session.phase))+
        '<button data-wm="final">Open Final Video</button></div>'+
        '<table><thead><tr><th>Shot</th><th>Status</th><th>Duration</th><th>Output</th><th>Actions</th></tr></thead><tbody>'+
        rows.join('')+'</tbody></table>';
}

async function pollWorkflow(){
    if(!document.querySelector('[data-wm-tab]')||workflowPolling)return;
    workflowPolling=true;
    const version=selectedWorkflow()?.version??null,tab=workflowTab;
    const sameView=()=>version===(selectedWorkflow()?.version??null)&&tab===workflowTab&&document.querySelector('[data-wm-tab]');
    try{
        const data=await workflowCall({action:'status',monitor:tab==='Production Monitor'});
        if(!sameView())return;
        workflowLive={version,data};
        if($('#wmTokenStatus'))$('#wmTokenStatus').textContent=hfTokenStatus(data.hfTokenSaved);
        const live=$('#wmLive');
        if(live)live.innerHTML=tab==='Dependencies'?dependencyView():tab==='Installation Monitor'?installationView():productionMonitorView();
        const install=document.querySelector('[data-wm="preview"]'),notice=$('#wmActionNote');
        if(install&&notice){
            const reason=installReason();
            install.disabled=Boolean(reason);
            notice.textContent=reason;
            notice.hidden=!reason;
        }
        const field=$('#wmScript');
        if(field&&version&&!workflowDrafts.has(version)&&document.activeElement!==field&&!field.value){
            field.value=data.savedScript||'';
        }
    }catch(error){
        if(sameView()&&$('#wmLive'))$('#wmLive').textContent='Unable to read this workflow status. '+error.message;
    }
    finally{workflowPolling=false}
}
setInterval(pollWorkflow,2200);

document.addEventListener('toggle',event=>{
    if(event.target.id==='wmTerminalLog')workflowTerminalOpen=event.target.open;
    if(event.target.id==='wmShotLogs')workflowLogOpen=event.target.open;
},true);

document.addEventListener('change',async event=>{
    const input=event.target;
    if(input.id==='wmRestart')saveInstallDraft();
    if(input.id!=='wmScriptFile'||!input.files.length)return;
    const file=input.files[0];input.value='';
    try{
        if(file.size>256000)throw Error('Choose a Bash file under 256 KB.');
        $('#wmScript').value=(await file.text()).replace(/^\uFEFF/,'');
        saveInstallDraft();
    }catch(error){reportError(error)}
});
document.addEventListener('input',event=>{
    if(['wmScript','wmTimeout'].includes(event.target.id))saveInstallDraft();
});

let pendingInstall=null;
document.addEventListener('click',async event=>{
    const tab=event.target.closest('button[data-wm-tab]');
    if(tab){workflowTab=tab.dataset.wmTab;render();pollWorkflow();return}
    const button=event.target.closest('button[data-wm]');
    if(!button)return;
    const action=button.dataset.wm;
    try{
        if(action==='showToken'){
            const field=$('#wmToken');field.type=field.type==='password'?'text':'password';
            button.textContent=field.type==='password'?'Show':'Hide';return;
        }
        button.disabled=true;
        if(action==='token'||action==='save-token'){
            const field=$('#wmToken');await workflowCall({action,token:field.value});field.value='';
            $('#wmTokenStatus').textContent=hfTokenStatus(true);
            reportStatus(action==='token'?'Hugging Face accepted the saved token.':'Hugging Face token saved.');
        }
        if(action==='preview'){
            const reason=installReason();
            if(reason)throw Error(reason);
            saveInstallDraft();
            const profile=selectedWorkflow();
            const data=workflowStatus()||await workflowCall({action:'status'});
            const draft=storedInstallDraft(data),timeout=Number(draft.timeout);
            if(!Number.isInteger(timeout)||timeout<1||timeout>180)throw Error('Choose a timeout from 1 to 180 minutes in Setup.');
            const preview=await workflowCall({action:'install-preview',script:draft.script});
            pendingInstall={script:preview.script,request:{version:profile.version,action:'install',confirmed:true,
                script:draft.script,timeout,instance:state.connection.instance??null,restart:draft.restart}};
            const bytes=dependencyReport()?.downloadBytes;
            const dialog=$('#wmConfirm');
            dialog.innerHTML='<h2>Review remote installation</h2><p>'+esc(profile.name)+'</p>'+
                '<p>Vast instance '+esc(state.connection.instance||'connected via SSH')+' · '+timeout+' minute timeout'+
                (Number.isFinite(bytes)?' · '+(bytes/1e9).toFixed(2)+' GB verified downloads':'')+'</p>'+
                (preview.willSaveHfToken?'<p>Confirmation saves the supplied Hugging Face token in Windows Credential Manager.</p>':
                    preview.hfTokenSaved?'<p>This installation uses the saved Hugging Face token.</p>':'')+
                '<pre class="preserve-lines">'+esc(preview.script||'Only install verified missing dependencies.')+
                '</pre><div class="row"><button data-wm="install">Confirm Installation</button><button data-wm="close">Back</button></div>';
            dialog.showModal();
        }
        if(action==='close'){$('#wmConfirm').close();pendingInstall=null}
        if(action==='install'){
            if(!pendingInstall||pendingInstall.request.version!==selectedWorkflow()?.version)throw Error('Review the current workflow before installing.');
            const {request,script}=pendingInstall;
            await workflowCall(request);
            workflowDrafts.set(request.version,{script,timeout:request.timeout,restart:request.restart});
            pendingInstall=null;$('#wmConfirm').close();
            workflowTab='Installation Monitor';render();await pollWorkflow();
        }
        if(action==='scan'||action==='cancel'){
            await workflowCall({action,instance:button.dataset.wmInstance});
            await pollWorkflow();
        }
        if(action==='start'||action==='retry'){
            await flushEdits();
            const result=await post('/start',{episode:ep.id,retry:button.dataset.wmShot});
            reportSubmission(result);
        }
        if(['pause','stop','resume'].includes(action))await post('/direct-production',{action,episode:ep.id});
        if(action==='final'){
            const cut=await post('/editor',{action:'get',episode:ep.id});
            const job=cut.finals.find(item=>item.status==='done'&&item.video);
            if(!job)throw Error('No finished Assembly export for this episode.');
            const link=document.createElement('a');
            link.href=job.video;link.target='_blank';link.rel='noopener';link.click();
        }
        if(action==='logs'){
            const result=await post('/direct-production',{action,episode:ep.id,shot:button.dataset.wmShot,version:button.dataset.wmVersion});
            $('#wmShotLogText').textContent=[result.promptId,result.status,result.error,
                ...result.messages.map(line=>[line.event,line.node,line.nodeType,line.error].filter(Boolean).join(' · '))].filter(Boolean).join('\n');
            $('#wmShotLogs').hidden=false;$('#wmShotLogs').open=true;
        }
        if(['start','retry','pause','stop','resume'].includes(action)){
            state=await(await fetch('/status')).json();await pollWorkflow();
        }
    }catch(error){reportError(error)}
    finally{if(button.isConnected)button.disabled=action==='preview'&&Boolean(installReason())}
});
