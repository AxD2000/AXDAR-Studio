let workflowTab='Setup', workflowLive=null, workflowPolling=false, workflowLogOpen=false, workflowTerminalOpen=false, workflowSavedFor='';
const workflowTabs=['Setup','Dependencies','Installation Monitor','Production Monitor'];
const workflowCall=obj=>post('/workflow-manager',{version:editingProfile,...obj});
const timeLabel=s=>s==null?'—':(s<60?Math.round(s)+' s':Math.floor(s/60)+'m '+Math.round(s%60)+'s');
const moneyLabel=value=>'$'+Number(value).toFixed(3);

function workflowWorkspace(){
    const subnav='<div class="toolbar">'+workflowTabs.map(label=>'<button data-wm-tab="'+label+'" aria-pressed="'+(workflowTab===label)+'">'+label+'</button>').join('')+'</div>';
    let content='';
    if(workflowTab==='Setup'){
        content=workflowPanel()+
        '<section class="panel"><h2>Hugging Face</h2><label>Hugging Face Access Token<input id="wmToken" type="password" autocomplete="off" placeholder="hf_…"></label>'+
        '<div class="row"><button data-wm="showToken" aria-controls="wmToken">Show</button><button data-wm="token">Test Token</button></div></section>'+
        '<section class="panel"><h2>Custom Installation Commands (Bash)</h2><label>Optional script<textarea id="wmScript" rows="10" spellcheck="false"></textarea></label>'+
        '<label>Import .sh file<input id="wmScriptFile" type="file" accept=".sh,text/x-shellscript"></label>'+
        '<div class="form-pair"><label>Timeout (minutes)<input id="wmTimeout" type="number" min="1" max="180" value="30" required></label>'+
        '<label><input id="wmRestart" type="checkbox"> Restart ComfyUI after this script even if all nodes are registered</label></div>'+
        '<p>Scripts run over SSH on the connected instance. Use the HF_TOKEN environment variable for authenticated downloads.</p>'+
        '<button class="primary" data-wm="preview">Preview installation</button></section>'+
        '<dialog id="wmConfirm"></dialog>';
        workflowSavedFor='';
    }
    if(workflowTab==='Dependencies'){
        content='<section class="panel"><h2>Dependency verification</h2><button data-wm="scan">Check current instance</button> <button data-wm-tab="Setup">Open Setup</button>'+
            '<div id="wmLive">'+dependencyView()+'</div></section>';
    }
    if(workflowTab==='Installation Monitor'){
        content='<section class="panel"><h2>Installation monitor</h2><div id="wmLive">'+installationView()+'</div></section>';
    }
    if(workflowTab==='Production Monitor'){
        content='<section class="panel"><h2>Video production monitor</h2><div id="wmLive">'+productionMonitorView()+'</div>'+
            '<details id="wmShotLogs" '+(workflowLogOpen?'open':'')+' hidden><summary>Shot logs</summary><pre class="preserve-lines" id="wmShotLogText"></pre></details></section>';
    }
    return subnav+content;
}

function dependencyView(){
    const i=workflowLive?.data?.installation,v=workflowLive?.data?.verification;
    const report=v?.report||(i?.version===editingProfile?i.report:null);
    if(!report?.models)return '<p>Run a scan on the connected instance to verify the selected version.</p>';
    const space=report.diskFreeBytes!=null&&report.downloadBytes!=null?
        '<p>'+((report.downloadBytes||0)/1e9).toFixed(2)+' GB verified downloads · '+
        (report.diskFreeBytes/1e9).toFixed(2)+' GB available'+
        (report.downloadBytes>report.diskFreeBytes?' · Insufficient disk space':'')+'</p>':'';
    return '<p>Verification · '+esc(v?.status||i?.status||'Pending')+'</p><h3>Custom nodes</h3>'+
        (report.nodes.length?'<ul class="danger">'+report.nodes.map(node=>'<li>'+esc(node)+'</li>').join('')+'</ul>':'<p>All required classes registered.</p>')+
        '<h3>Model files</h3>'+space+'<table><thead><tr><th>File</th><th>Status</th><th>Verified source</th></tr></thead><tbody>'+
        report.models.map(m=>'<tr><td>'+esc(m.name)+'</td><td>'+esc(m.status)+(m.error?' · '+esc(m.error):'')+'</td><td>'+
            (m.source?esc(m.source.repo)+' @ '+esc(m.source.revision.slice(0,12)):'—')+'</td></tr>').join('')+'</tbody></table>';
}

function installationView(){
    const data=workflowLive?.data,i=data?.installation;
    if(!i?.status)return '<p>No installation started.</p>';
    const terminal=['ready','needs attention','failed','cancelled','timed out'].includes(i.status);
    const cost=i.rate!=null?' · '+moneyLabel(i.elapsed/3600*i.rate)+' estimated':'';
    return '<p role="status">'+esc(i.status)+' · '+timeLabel(i.elapsed)+cost+(i.exitCode!=null?' · Installer exit '+i.exitCode:'')+(i.bashExitCode!=null?' · Bash exit '+i.bashExitCode:'')+'</p>'+
        '<p>'+esc(i.message||'')+'</p>'+
        (!terminal?'<button data-wm="cancel">Cancel Installation</button>':'')+
        '<table><thead><tr><th>Stage</th><th>Status</th><th>Elapsed</th><th>Progress</th></tr></thead><tbody>'+
        data.phases.map(name=>{
            const stage=i.stages?.[name]||{},t=(stage.status==='Running'?Date.now()/1000:stage.updated)-stage.started;
            return '<tr><td>'+esc(name)+'</td><td>'+esc(stage.status||'Pending')+'</td><td>'+timeLabel(Number.isFinite(t)?t:null)+'</td><td>'+
                (stage.total?'<progress value="'+stage.count+'" max="'+stage.total+'"></progress> '+stage.count+'/'+stage.total:
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
    const version=editingProfile,tab=workflowTab;
    try{
        const data=await workflowCall({action:'status',monitor:tab==='Production Monitor'});
        if(editingProfile!==version||workflowTab!==tab||!document.querySelector('[data-wm-tab]'))return;
        workflowLive={version,data};
        const live=$('#wmLive');
        if(live)live.innerHTML=tab==='Dependencies'?dependencyView():tab==='Installation Monitor'?installationView():productionMonitorView();
        const field=$('#wmScript');
        if(field&&workflowSavedFor!==version&&document.activeElement!==field&&!field.value){
            field.value=data.savedScript;
            workflowSavedFor=version;
        }
    }catch(error){if($('#wmLive'))$('#wmLive').textContent=error.message}
    finally{workflowPolling=false}
}
setInterval(pollWorkflow,2200);

document.addEventListener('toggle',event=>{
    if(event.target.id==='wmTerminalLog')workflowTerminalOpen=event.target.open;
    if(event.target.id==='wmShotLogs')workflowLogOpen=event.target.open;
},true);

document.addEventListener('change',async event=>{
    const input=event.target;
    if(input.id!=='wmScriptFile'||!input.files.length)return;
    const file=input.files[0];input.value='';
    try{
        if(file.size>256000)throw Error('Choose a Bash file under 256 KB.');
        $('#wmScript').value=(await file.text()).replace(/^\uFEFF/,'');
    }catch(error){reportError(error)}
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
        if(action==='token'){
            const field=$('#wmToken');await workflowCall({action,token:field.value});field.value='';
            reportStatus('Hugging Face accepted the token. Saved in Windows Credential Manager.');
        }
        if(action==='preview'){
            if(!currentProfile(editingProfile)?.executable)throw Error('Prepare the original workflow first.');
            if(state.connection?.state!=='Ready')throw Error('Select and connect a Vast instance first.');
            const time=$('#wmTimeout');
            if(!time.checkValidity())throw Error('Choose a timeout from 1 to 180 minutes.');
            pendingInstall={version:editingProfile,action:'install',confirmed:true,script:$('#wmScript').value,
                timeout:time.valueAsNumber,instance:state.connection.instance??null,restart:$('#wmRestart').checked};
            const dialog=$('#wmConfirm');
            dialog.innerHTML='<h2>Review remote installation</h2><p>Vast instance '+esc(state.connection.instance||'connected via SSH')+' · '+pendingInstall.timeout+
                ' minute timeout</p><pre class="preserve-lines">'+esc(pendingInstall.script||'Only install verified missing dependencies.')+
                '</pre><div class="row"><button data-wm="install">Confirm Installation</button><button data-wm="close">Back</button></div>';
            dialog.showModal();
        }
        if(action==='close'){$('#wmConfirm').close();pendingInstall=null}
        if(action==='install'){
            await workflowCall(pendingInstall);pendingInstall=null;$('#wmConfirm').close();
            workflowTab='Installation Monitor';render();await pollWorkflow();
        }
        if(action==='scan'||action==='cancel'){await workflowCall({action});await pollWorkflow()}
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
    finally{if(button.isConnected)button.disabled=false}
});
