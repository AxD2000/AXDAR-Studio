let discoveredInstances = [];

function connectionPanel(){
    const c=state.connection||{};
    const creds=state.credentials||{};
    const details=c.instance&&!['Vast starting','Waiting for Vast','Unavailable'].includes(c.state)?
      ' · Instance #'+esc(c.instance)+(c.gpu?' · '+esc(c.gpu):''):'';
    const pending=['Connecting','Reconnecting','Vast starting','ComfyUI Starting'].includes(c.state);
    const allSaved=creds.vastApiSaved===true&&creds.sshKeySaved===true;
    const keyBadge=(ok,yes,no)=>'<strong>'+(ok?yes:no)+'</strong>';
    const keysLine=allSaved
      ?'<p class="muted"><strong>Automatic connection active</strong> · All keys auto-saved (Vast API · SSH key'+(creds.hfTokenSaved?' · HF Token':'')+') — no manual entry needed.</p>'
      :'<p class="muted">Keys (auto-saved): Vast API '+keyBadge(creds.vastApiSaved,'Saved','Not set')+
        ' · SSH key '+keyBadge(creds.sshKeySaved,'Auto-saved','Auto-generated on connect')+
        ' · HF Token '+keyBadge(creds.hfTokenSaved,'Saved','Not set')+'</p>';
    return '<section class="panel" aria-labelledby="vastConnectionTitle">'+
      '<h2 id="vastConnectionTitle">Vast connection</h2>'+
      '<p role="status">'+esc(c.state||'Looking for an instance')+details+'</p>'+
      connectionIndicators(c,['api','ssh','comfy','gpu'])+
      keysLine+
      (c.error?'<p class="'+(pending?'muted':'danger')+'">'+esc(c.error)+'</p>':'')+
      '<div class="row"><button data-connection="refresh">Refresh instances</button>'+
      '<button data-connection="reconnect">Reconnect from scratch</button>'+
      '<button data-connection="disconnect">Disconnect tunnel</button>'+
      (state.restartSupported?'<button data-connection="restart-studio">Restart Studio</button>':'')+
      '<button data-tools="open">Terminal &amp; HF Token</button></div>'+
      '<details '+(c.indicators?.api==='red'||creds.vastApiSaved===false?'open':'')+'><summary>'+(allSaved?'Advanced: Manage saved keys &amp; instance selector':'Vast API key &amp; instance selector')+'</summary>'+
      '<p class="muted">'+(creds.vastApiSaved?'Vast API key and SSH key are saved permanently and loaded automatically on every launch. Leave blank unless replacing your key.':'Save your Vast API key once; AXDAR stores it in Windows Credential Manager and auto-registers your SSH key for all future instances.')+'</p>'+
      '<label for="vastApiKey">Vast API key</label><input id="vastApiKey" type="password" autocomplete="off" dir="ltr" placeholder="'+(creds.vastApiSaved?'Saved automatically — leave blank':'Paste Vast API key')+'">'+
      '<div class="row"><button data-connection="key">Save API key</button>'+
      '<button data-connection="instances">Discover instances</button></div>'+
      (discoveredInstances.length?'<label for="vastInstanceSelect">Rented instance<select id="vastInstanceSelect">'+
        discoveredInstances.map(row=>option(String(row.id),'#'+row.id+' · '+(row.gpu||'GPU')+' · '+(row.status||'unknown')+(row.rate!=null?' · $'+Number(row.rate).toFixed(3)+'/hr':''),String(c.instance||''))).join('')+
        '</select></label><button data-connection="connect">Connect selected instance</button>':'')+
      '<details style="margin-top:8px"><summary>Manual SSH fallback (only if not using Vast API)</summary>'+
      '<p class="muted">Not needed when Vast API is saved — AXDAR connects automatically using your saved SSH key.</p>'+
      '<label for="manualSshInput">Vast SSH command (ssh -p PORT root@HOST)</label>'+
      '<input id="manualSshInput" type="text" autocomplete="off" dir="ltr" placeholder="ssh -p 12345 root@ssh.vast.ai" value="'+esc(c.ssh?'ssh -p '+c.ssh.port+' root@'+c.ssh.host:'')+'">'+
      '<div class="row"><button data-connection="connect-ssh">Connect using SSH</button>'+
      (c.ssh?'<button data-connection="refresh">Switch back to automatic Vast API</button>':'')+'</div></details></details></section>';
}

document.addEventListener('click',async e=>{
    const button=e.target.closest('button');
    if(!button)return;
    if(button.dataset.tools==='open'){
        const saved=document.getElementById('hfSaved');
        if(saved)saved.textContent=state.credentials?.hfTokenSaved?'Saved in Windows credential vault (auto-loaded)':'';
        document.getElementById('workflowTools').showModal();return;
    }
    if(button.dataset.tools==='close'){document.getElementById('hfToken').value='';document.getElementById('workflowTools').close();return}
    const action=button.dataset.connection;
    if(!action)return;
    button.disabled=true;
    try{
        if(action==='restart-studio'){
            await saveQueue;
            await post('/studio/restart',{});
            reportStatus('Restarting Studio…');
            return;
        }
        const body={action};
        if(action==='key')body.key=document.getElementById('vastApiKey').value;
        if(action==='hf')body.token=document.getElementById('hfToken').value;
        if(action==='command')body.command=document.getElementById('remoteCommand').value;
        if(action==='connect'){body.instance=Number(document.getElementById('vastInstanceSelect')?.value||0);body.auto=true;}
        if(action==='connect-ssh')body.ssh=document.getElementById('manualSshInput')?.value||'';
        const result=await post('/connection',body);
        if(action==='hf'){document.getElementById('hfToken').value='';if(state.credentials)state.credentials.hfTokenSaved=true;document.getElementById('hfSaved').textContent='Saved in Windows credential vault (auto-loaded)';return}
        if(action==='command'){
            document.getElementById('remoteOutput').textContent=result.output;
            return;
        }
        if(action==='instances'){
            discoveredInstances=Array.isArray(result)?result:[];
            render();
            reportStatus(discoveredInstances.length+' Vast instance(s) found.');
            return;
        }
        state=await(await fetch('/status')).json();
        render();
        if(action==='key')reportStatus('Vast API key saved.');
    }catch(err){const dialog=document.getElementById('workflowTools');if(dialog.open)document.getElementById('remoteOutput').textContent=err.message;else reportError(err)}
    finally{if(button.isConnected)button.disabled=false}
});

function connectionIndicators(c,keys){
    const colors={grey:'#8b929e',red:'#f05252',green:'#22c55e'};
    const meanings={grey:'No current check / disconnected',red:'Failed / interrupted',green:'Passed'};
    const labels={api:'Vast API',ssh:'SSH',comfy:'ComfyUI',gpu:'GPU'};
    return '<div class="row" aria-label="Connection checks">'+keys.map(key=>{
        const label=labels[key];
        const value=c.indicators?.[key];const color=colors[value]?value:'grey';
        return '<span role="status" aria-label="'+label+': '+meanings[color]+'" title="'+label+': '+meanings[color]+'"><span aria-hidden="true" style="display:inline-block;width:11px;height:11px;border-radius:50%;background:'+colors[color]+';margin-right:6px"></span>'+label+'</span>';
    }).join('')+'</div>';
}

document.addEventListener('close',e=>{if(e.target.id==='workflowTools')document.getElementById('hfToken').value=''},true);

const workspaceLayoutKey='axdar.lastWorkspace';

function rememberWorkspace(){
    if(!ep)return;
    try{
        localStorage.setItem(workspaceLayoutKey,JSON.stringify({
            episode:ep.id,
            page,
            selectedId:typeof selectedId==='string'?selectedId:'',
            storyTab:typeof storyTab==='string'?storyTab:'Script',
            workflowTab:typeof workflowTab==='string'?workflowTab:'Setup',
        }));
    }catch{}
}

function restoreWorkspace(previous){
    let saved=null;
    try{saved=JSON.parse(localStorage.getItem(workspaceLayoutKey))}catch{}
    ep=db.episodes.find(item=>item.id===(saved?.episode||previous?.episode))||ep;
    if(Number.isInteger(saved?.page)&&saved.page>=0&&saved.page<spaces.length)page=saved.page;
    if(typeof saved?.selectedId==='string'&&ep?.shots?.some(s=>s.id===saved.selectedId))selectedId=saved.selectedId;
    if(typeof saved?.storyTab==='string'&&['Script','Scenes','Storyboard'].includes(saved.storyTab))storyTab=saved.storyTab;
    if(typeof saved?.workflowTab==='string'&&typeof workflowTabs!=='undefined'&&workflowTabs.includes(saved.workflowTab))workflowTab=saved.workflowTab;
}

async function resumeWorkspaceSession(){
    const previous=await post('/workspace-session',{mode:'resume'});
    restoreWorkspace(previous);
    return previous;
}

document.addEventListener('change',event=>{
    if(event.target.id==='episodeMenu')queueMicrotask(rememberWorkspace);
});
document.addEventListener('click',event=>{
    if(event.target.closest('button[data-page],button[data-shot],button[data-open-shot],button[data-storytab],button[data-wm-tab]'))queueMicrotask(rememberWorkspace);
});
