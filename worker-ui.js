/* Unified Worker Workspace UI (Single Execution Owner over DirectSession + Vast Connection) */
let workerNodesCache = null;
let workerLogsTab = 'comfyui';
let workerLogsData = null;

function workerSettings(){
    return state.worker?.settings || {};
}

function workerMandatory(){
    return state.worker?.mandatory || {
        skipUnusedCharacterRefs: true,
        reconcileBeforeResubmit: true,
        strictGraphIntegrity: true,
        verifyOutputIntegrity: true,
    };
}

function workerShotState(shot, latest){
    const st = String(latest?.status || '').toLowerCase();
    if(['preparing','submitting','queued','waiting'].includes(st)) return 'Generating';
    if(shot?.skipped) return 'Skipped';
    if(st==='done' && latest?.video) return 'Completed';
    if(['error','uncertain'].includes(st)) return 'Failed';
    return 'Pending';
}

function workerShotLabel(shot, idx){
    const code = shot.shot_code || ('EP01S'+String(idx+1).padStart(2,'0'));
    const title = shot.sourceHeading || ('Shot '+(idx+1));
    return title.toUpperCase().includes(code) ? title : (code+' · '+title);
}

function workerControlsHtml(){
    const session = state.session?.episode===ep.id ? state.session : {};
    const cfg = workerSettings();
    const approved = ep.shots.filter(s=>s.approved && !s.skipped).length;
    const canStart = Boolean(approved && currentProfile()?.executable && state.connection?.state==='Ready' && !state.running && !state.workflowError);
    const activeQueue = Boolean(session.episode && !['Finished','Stopped'].includes(session.phase));
    const canPause = Boolean(state.running);
    const canResume = Boolean(!state.running && activeQueue && ['Paused','Needs attention'].includes(session.phase) && state.connection?.state==='Ready');
    const canStop = Boolean(state.running || activeQueue);
    const canSkip = Boolean(state.running || activeQueue);
    const targetRegenShot = ep.shots.find(s=>s.id===session.current) || selectedShot() || ep.shots.find(s=>s.approved);
    const canRegen = Boolean(!state.running && state.connection?.state==='Ready' && currentProfile()?.executable && targetRegenShot?.approved);

    let completedCount=0, failedCount=0, skippedCount=0, generatingCount=0;
    for(const shot of ep.shots){
        const st = workerShotState(shot, versions(shot).at(-1));
        if(st==='Completed') completedCount++;
        else if(st==='Failed') failedCount++;
        else if(st==='Skipped') skippedCount++;
        else if(st==='Generating') generatingCount++;
    }
    const runLimit = Number(cfg.runLimit || 0);
    return '<section class="panel" id="workerControlPanel">'+
        '<div class="top"><h2>Worker Execution</h2>'+
        '<div class="row">'+
        '<button type="button" data-worker="open-logs">Logs &amp; Error Classifier</button>'+
        '<button type="button" data-worker="export-downloads" '+(completedCount?'':'disabled')+'>Export Completed to Downloads</button>'+
        '</div></div>'+
        '<p><strong>'+esc(ep.title)+'</strong> · '+ep.shots.length+' shots · '+approved+' approved · '+
        completedCount+' completed'+(generatingCount?' · '+generatingCount+' generating':'')+
        (failedCount?' · '+failedCount+' failed':'')+(skippedCount?' · '+skippedCount+' skipped':'')+
        ' · Workflow: <strong>'+esc(state.workflow||'')+'</strong> · Status: <strong>'+esc(session.phase||'Idle')+'</strong>'+
        (state.running&&session.requestedAction?' ('+esc(session.requestedAction)+'…)':'')+'</p>'+
        (state.workflowError?'<p class="danger">'+esc(state.workflowError)+'</p>':'')+
        (session.pauseReason?'<p class="status">'+esc(session.pauseReason)+'</p>':'')+
        (session.error?'<p class="danger">'+esc(localize(session.error))+'</p>':'')+
        (session.errorClassification?'<p class="danger"><strong>['+esc(session.errorClassification.category)+']</strong> '+esc(session.errorClassification.summary||'')+'</p>':'')+
        '<div class="row">'+
        '<label class="wk-inline">Run Limit <select id="wkRunLimit">'+
        option('0','All Approved',String(runLimit))+
        option('1','Test: Next 1 Shot',String(runLimit))+
        option('3','Test: Next 3 Shots',String(runLimit))+
        option('5','Test: Next 5 Shots',String(runLimit))+
        (![0,1,3,5].includes(runLimit)?option(String(runLimit),'Next '+runLimit+' Shots',String(runLimit)):'')+
        '</select></label>'+
        '<button type="button" class="primary" data-worker="start" '+(canStart?'':'disabled')+'>Start Production</button>'+
        '<button type="button" data-worker="resume" '+(canResume?'':'disabled')+'>Resume</button>'+
        '<button type="button" data-worker="pause" '+(canPause?'':'disabled')+'>Pause</button>'+
        '<button type="button" class="danger" data-worker="stop" '+(canStop?'':'disabled')+'>Stop Production</button>'+
        '<button type="button" data-worker="skip" '+(canSkip?'':'disabled')+'>Skip Shot</button>'+
        '<button type="button" data-worker="regenerate" data-worker-shot="'+esc(targetRegenShot?.id||'')+'" '+(canRegen?'':'disabled')+'>Regenerate Shot</button>'+
        '</div></section>';
}

function workerSettingsHtml(){
    const cfg = workerSettings();
    const inv = workerMandatory();
    const locked = state.running;
    const chk = (key, label, disabled=false) =>
        '<label class="wk-check"><input type="checkbox" data-wk-setting="'+key+'" '+(cfg[key]?'checked':'')+' '+(disabled?'disabled':'')+'> '+esc(label)+'</label>';
    return '<section class="panel" id="workerSettingsPanel">'+
        '<div class="top"><h2>Worker Settings</h2><small class="muted">'+(locked?'Input settings locked while running; flow settings remain live.':'Saved locally per studio.')+'</small></div>'+
        '<div class="wk-invariants"><small class="muted">Mandatory Safety Invariants (Always Enabled):</small> '+
        '<span class="pill">Skip Unused Refs</span> '+
        '<span class="pill">Reconcile Before Resubmit</span> '+
        '<span class="pill">Strict Graph Integrity</span> '+
        '<span class="pill">Verify Output MP4</span>'+
        (inv.skipUnusedCharacterRefs?'':'')+
        '</div>'+
        '<div class="wk-checks">'+
        chk('autoNodeDetection','Automatic Node Detection',locked)+
        chk('autoCharacterImages','Auto Character Image Mapping',locked)+
        chk('autoCharacterAudio','Auto Character Audio Mapping',locked)+
        chk('autoReconnect','Automatic Reconnection',false)+
        chk('continuousExecution','Continuous Shot Execution',false)+
        chk('autoRetry','Automatic Retry (Shot Errors)',false)+
        chk('financialTracking','Financial Tracking',false)+
        chk('autoExportDownloads','Auto-Export to Downloads on Finish',locked)+
        '</div>'+
        '<div class="form-pair">'+
        '<label>Max Shot Retries (0–5)<input type="number" min="0" max="5" data-wk-setting="maxRetries" value="'+esc(cfg.maxRetries??1)+'"></label>'+
        '<label>Circuit Breaker (Consecutive Fails)<input type="number" min="1" max="10" data-wk-setting="consecutiveFailureLimit" value="'+esc(cfg.consecutiveFailureLimit??2)+'"></label>'+
        '<label>Session Limit Minutes (0 = Unlimited)<input type="number" min="0" max="1440" data-wk-setting="sessionMinutes" value="'+esc(cfg.sessionMinutes??0)+'" '+(locked?'disabled':'')+'></label>'+
        '<label>Seed Mode<select data-wk-setting="seedMode" '+(locked?'disabled':'')+'>'+
        option('random','Randomize per Take',cfg.seedMode||'random')+
        option('fixed','Fixed Seed',cfg.seedMode||'random')+
        '</select></label>'+
        (cfg.seedMode==='fixed'?'<label>Fixed Seed (blank = workflow default)<input type="number" min="0" data-wk-setting="fixedSeed" value="'+esc(cfg.fixedSeed??'')+'" '+(locked?'disabled':'')+'></label>':'')+
        '</div>'+
        '<div class="row"><button type="button" class="primary" data-worker="save-settings">Save Worker Settings</button></div>'+
        '</section>';
}

function workerFinancialHtml(){
    if(workerSettings().financialTracking===false) return '';
    const f = state.financial;
    if(!f) return '<section class="panel" id="workerFinancePanel"><h2>Financial Monitor</h2><p class="muted">Connect a Vast instance or set a manual hourly rate to track session costs.</p></section>';
    const fmtUsd = v => v==null||!Number.isFinite(v)?'—':'$'+Number(v).toFixed(3);
    const fmtMin = s => s==null||!Number.isFinite(s)?'—':(s<60?Math.round(s)+'s':Math.floor(s/60)+'m '+Math.round(s%60)+'s');
    return '<section class="panel" id="workerFinancePanel">'+
        '<div class="top"><h2>Financial Monitor</h2><span class="pill">'+esc(f.rateSourceLabel||'Estimated')+'</span></div>'+
        '<div class="wm-metrics">'+
        '<div class="wm-stat"><small>Rental Price / Hour ('+esc(f.rateBadge||'Actual')+')</small><strong>'+fmtUsd(f.hourlyRate)+'</strong></div>'+
        '<div class="wm-stat"><small>Rental Price / Min (Estimated)</small><strong>'+fmtUsd(f.minuteRate)+'</strong></div>'+
        '<div class="wm-stat"><small>Session Elapsed</small><strong>'+fmtMin(f.sessionElapsedSeconds)+'</strong></div>'+
        '<div class="wm-stat"><small>Current Session Cost (Estimated)</small><strong>'+fmtUsd(f.currentSessionCost)+'</strong></div>'+
        '<div class="wm-stat"><small>Shots (Done / Rem / Total)</small><strong>'+f.completedShots+' / '+f.remainingShots+' / '+f.totalShots+'</strong></div>'+
        '<div class="wm-stat"><small>Avg Time / Shot'+(f.coldStartOnly?' (Cold Start)':'')+'</small><strong>'+fmtMin(f.avgShotSeconds)+'</strong></div>'+
        '<div class="wm-stat"><small>Est. Remaining Time</small><strong>'+fmtMin(f.estimatedRemainingSeconds)+'</strong></div>'+
        '<div class="wm-stat"><small>Est. Total Cost</small><strong>'+fmtUsd(f.estimatedTotalCost)+'</strong></div>'+
        '</div>'+
        '<p class="muted"><strong>Notice:</strong> Stopping Worker or closing AXDAR Studio does NOT stop your Vast.ai instance or billing.</p>'+
        '</section>';
}

function workerNodesHtml(){
    return '<section class="panel" id="workerNodesPanel">'+
        '<div class="top"><h2>Detected Workflow Nodes &amp; Session Controls</h2>'+
        '<button type="button" data-worker="inspect-nodes">Refresh Detected Nodes</button></div>'+
        '<div id="workerNodesBody"><p class="muted">Click Refresh Detected Nodes after installing the Workflow Package to inspect and override Resolution, Aspect Ratio, FPS, Seed, and Node bindings.</p></div>'+
        '</section>';
}

function workerTakesHtml(){
    const canRegen = !state.running && state.connection?.state==='Ready' && Boolean(currentProfile()?.executable);
    if(!ep.shots.length) return '<section class="panel"><h2>Shots &amp; Takes</h2><p class="muted">Import a story and approve shots first.</p></section>';
    const rows = ep.shots.map((shot, idx)=>{
        const takes = versions(shot);
        const latest = takes.at(-1);
        const st = workerShotState(shot, latest);
        const code = shot.shot_code || ('EP01S'+String(idx+1).padStart(2,'0'));
        const completedTakes = takes.filter(t=>t.video);
        const selectedId = shot.selected_take_id || (completedTakes.at(-1)?.takeId || '');
        const takeCards = takes.map((t, ti)=>{
            const tNum = t.takeNumber || (ti+1);
            const fname = t.displayFilename || (code+'_T'+String(tNum).padStart(2,'0')+'.mp4');
            const isSelected = Boolean(t.takeId && t.takeId===selectedId);
            return '<div class="wk-take-chip '+(isSelected?'wk-take-selected':'')+'">'+
                '<strong>T'+String(tNum).padStart(2,'0')+'</strong> · '+esc(statuses[t.status]||t.status)+
                (t.duration?' · '+Number(t.duration).toFixed(1)+'s':'')+
                (t.seed!=null?' · seed '+esc(t.seed):'')+
                (t.video?
                    ' <button type="button" data-wm-zoom="'+esc(t.video)+'" data-wm-zoom-title="'+esc(fname)+'">Enlarge</button>'+
                    ' <a class="wk-link" href="'+esc(t.video)+'?download=1" download="'+esc(fname)+'">Download</a>'+
                    (t.takeId?' <button type="button" data-worker="select-take" data-worker-shot="'+esc(shot.id)+'" data-worker-take="'+esc(t.takeId)+'" '+(isSelected?'disabled':'')+'>'+(isSelected?'Selected':'Select')+'</button>':'')
                :'')+
                (t.error?'<div class="danger">'+esc(localize(t.error))+'</div>':'')+
                '</div>';
        }).join('');
        return '<div class="wk-shot-row">'+
            '<div class="top"><strong>'+esc(workerShotLabel(shot, idx))+'</strong>'+
            '<div class="row"><span class="pill wk-state-'+st.toLowerCase()+'">'+st+'</span>'+
            '<button type="button" data-worker="toggle-skip" data-worker-shot="'+esc(shot.id)+'" data-worker-skipped="'+(shot.skipped?'0':'1')+'">'+(shot.skipped?'Unskip':'Skip')+'</button>'+
            '<button type="button" data-worker="regenerate" data-worker-shot="'+esc(shot.id)+'" '+(canRegen&&shot.approved?'':'disabled')+'>Regenerate</button>'+
            '</div></div>'+
            (completedTakes.length?'<div class="wm-thumbs">'+completedTakes.map((t,ti)=>{
                const tNum = t.takeNumber || (ti+1);
                const fname = t.displayFilename || (code+'_T'+String(tNum).padStart(2,'0')+'.mp4');
                return '<figure class="wm-thumb-card"><video muted preload="metadata" src="'+esc(t.video)+'"></video><figcaption><strong>'+esc(fname)+'</strong></figcaption></figure>';
            }).join('')+'</div>':'')+
            (takeCards||'<small class="muted">No takes yet.</small>')+
            '</div>';
    }).join('');
    return '<section class="panel" id="workerTakesPanel"><h2>Shots &amp; Takes</h2>'+rows+'</section>';
}

function workerWorkspace(){
    return '<div id="workerUnified">'+
        connectionPanel()+
        workerControlsHtml()+
        workerFinancialHtml()+
        workerSettingsHtml()+
        workerNodesHtml()+
        workerTakesHtml()+
        '<dialog id="wmVideoZoom" class="wm-zoom-dialog">'+
        '<div class="top"><strong id="wmZoomTitle"></strong><button type="button" data-wm-zoom-close>Close</button></div>'+
        '<video id="wmZoomVideo" controls playsinline></video>'+
        '</dialog>'+
        '<dialog id="workerLogsDialog" class="wk-log-dialog">'+
        '<div class="top"><strong>Worker Logs &amp; Error Classifier</strong>'+
        '<div class="row"><button type="button" data-worker="refresh-logs">Refresh</button><button type="button" data-worker="close-logs">Close</button></div></div>'+
        '<div class="toolbar">'+
        ['comfyui','remote','studio'].map(tab=>'<button type="button" class="'+(workerLogsTab===tab?'on':'')+'" data-worker-log-tab="'+tab+'">'+
            (tab==='comfyui'?'ComfyUI Log':tab==='remote'?'Remote Host (Vast/SSH)':'AXDAR Studio Log')+'</button>').join('')+
        '</div>'+
        '<div id="workerLogsClassification"></div>'+
        '<pre id="workerLogsPre" class="preserve-lines">Click Refresh to load logs.</pre>'+
        '</dialog></div>';
}

function collectWorkerSettingsForm(){
    const payload = {};
    for(const el of document.querySelectorAll('[data-wk-setting]')){
        const key = el.dataset.wkSetting;
        if(el.disabled) continue;
        if(el.type==='checkbox') payload[key] = Boolean(el.checked);
        else if(el.type==='number'){
            const raw = el.value.trim();
            if(raw===''){
                payload[key] = ['fixedSeed','customWidth','customHeight','customMegapixels','fpsOverride','framesOverride','manualHourlyRate'].includes(key) ? null : 0;
            }else{
                payload[key] = el.step==='any' ? Number(raw) : parseInt(raw, 10);
            }
        }else{
            payload[key] = el.value;
        }
    }
    const rl = document.getElementById('wkRunLimit');
    if(rl && !state.running) payload.runLimit = parseInt(rl.value, 10) || 0;
    return payload;
}

document.addEventListener('click', async event => {
    const zoomBtn = event.target.closest('button[data-wm-zoom]');
    if(zoomBtn){
        const dialog = $('#wmVideoZoom'), video = $('#wmZoomVideo'), title = $('#wmZoomTitle');
        if(dialog && video){
            title.textContent = zoomBtn.dataset.wmZoomTitle || 'Shot Preview';
            video.src = zoomBtn.dataset.wmZoom;
            dialog.showModal();
            video.play().catch(()=>{});
        }
        return;
    }
    if(event.target.closest('button[data-wm-zoom-close]')){
        const dialog = $('#wmVideoZoom'), video = $('#wmZoomVideo');
        if(video){ video.pause(); video.removeAttribute('src'); video.load(); }
        if(dialog?.open) dialog.close();
        return;
    }
    const tabBtn = event.target.closest('button[data-worker-log-tab]');
    if(tabBtn){
        workerLogsTab = tabBtn.dataset.workerLogTab;
        if(typeof renderWorkerLogsModal === 'function') renderWorkerLogsModal();
        return;
    }
    const btn = event.target.closest('button[data-worker]');
    if(!btn) return;
    const action = btn.dataset.worker;
    if(action==='close-logs'){
        const dlg = $('#workerLogsDialog');
        if(dlg?.open) dlg.close();
        return;
    }
    btn.disabled = true;
    try{
        if(action==='save-settings'){
            const settings = collectWorkerSettingsForm();
            const updated = await post('/worker', {action:'save-settings', settings});
            state.worker = updated;
            render();
            reportStatus('Worker settings saved.');
            return;
        }
        if(action==='start'){
            await flushEdits();
            const rl = Number(document.getElementById('wkRunLimit')?.value || 0);
            const res = await post('/worker', {action:'start', episode:ep.id, runLimit:rl});
            state = await (await fetch('/status')).json();
            render();
            reportSubmission(res);
            return;
        }
        if(['pause','resume','stop','skip'].includes(action)){
            const body = {action, episode:ep.id};
            if(action==='resume') body.runLimit = Number(document.getElementById('wkRunLimit')?.value || 0);
            await post('/direct-production', body);
            state = await (await fetch('/status')).json();
            render();
            return;
        }
        if(action==='regenerate'){
            await flushEdits();
            const shotId = btn.dataset.workerShot;
            if(!shotId) throw Error('Select an approved shot to regenerate.');
            const res = await post('/direct-production', {action:'regenerate', episode:ep.id, shot:shotId});
            state = await (await fetch('/status')).json();
            render();
            reportSubmission(res);
            return;
        }
        if(action==='select-take'){
            await projectAction('/shots/select-take', {episode:ep.id, shot:btn.dataset.workerShot, takeId:btn.dataset.workerTake});
            render();
            reportStatus('Preferred take selected.');
            return;
        }
        if(action==='toggle-skip'){
            await projectAction('/shots/skip', {episode:ep.id, shot:btn.dataset.workerShot, skipped:btn.dataset.workerSkipped==='1'});
            render();
            reportStatus(btn.dataset.workerSkipped==='1'?'Shot marked Skipped.':'Shot restored to queue.');
            return;
        }
        if(action==='open-logs' || action==='refresh-logs'){
            const dlg = $('#workerLogsDialog');
            if(dlg && !dlg.open) dlg.showModal();
            try{
                workerLogsData = await post('/worker', {action:'logs', includeRemote: action==='refresh-logs'});
                if(typeof renderWorkerLogsModal === 'function') renderWorkerLogsModal();
            }catch(err){
                const pre = $('#workerLogsPre');
                if(pre) pre.textContent = err.message;
            }
            return;
        }
        if(action==='inspect-nodes'){
            workerNodesCache = await post('/worker', {action:'inspect-nodes'});
            if(typeof renderWorkerNodesBody === 'function') renderWorkerNodesBody();
            return;
        }
        if(action==='export-downloads'){
            const res = await post('/worker', {action:'export-downloads', episode:ep.id});
            reportStatus('Exported '+res.exportedCount+' video(s) to '+res.destination);
            return;
        }
    }catch(err){ reportError(err); }
    finally{ if(btn.isConnected) btn.disabled = false; }
});

document.addEventListener('close', event => {
    if(event.target.id==='wmVideoZoom'){
        const video = $('#wmZoomVideo');
        if(video){ video.pause(); video.removeAttribute('src'); video.load(); }
    }
}, true);
