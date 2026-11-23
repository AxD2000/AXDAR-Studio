let discoveredInstances = [];

function connectionPanel(){
    const c=state.connection||{};
    const details=c.instance&&!['Vast starting','Waiting for Vast','Unavailable'].includes(c.state)?
      ' · Instance #'+esc(c.instance)+(c.gpu?' · '+esc(c.gpu):''):'';
    const pending=['Connecting','Reconnecting','Vast starting','ComfyUI Starting'].includes(c.state);
    return '<section class="panel" aria-labelledby="vastConnectionTitle">'+
      '<h2 id="vastConnectionTitle">Vast connection</h2>'+
      '<p role="status">'+esc(c.state||'Looking for an instance')+details+'</p>'+
      connectionIndicators(c,['api','ssh','comfy','gpu'])+
      (c.error?'<p class="'+(pending?'muted':'danger')+'">'+esc(c.error)+'</p>':'')+
      '<div class="row"><button data-connection="refresh">Refresh instances</button>'+
      '<button data-connection="reconnect">Reconnect from scratch</button>'+
      '<button data-connection="disconnect">Disconnect tunnel</button>'+
      (state.restartSupported?'<button data-connection="restart-studio">Restart Studio</button>':'')+
      '<button data-tools="open">Terminal &amp; HF Token</button></div>'+
      '<details '+(c.indicators?.api==='red'?'open':'')+'><summary>Vast API key &amp; instance selector</summary>'+
      '<label for="vastApiKey">Vast API key</label><input id="vastApiKey" type="password" autocomplete="off" dir="ltr">'+
      '<div class="row"><button data-connection="key">Save API key</button>'+
      '<button data-connection="instances">Discover instances</button></div>'+
      (discoveredInstances.length?'<label for="vastInstanceSelect">Rented instance<select id="vastInstanceSelect">'+
        discoveredInstances.map(row=>option(String(row.id),'#'+row.id+' · '+(row.gpu||'GPU')+' · '+(row.status||'unknown')+(row.rate!=null?' · $'+Number(row.rate).toFixed(3)+'/hr':''),String(c.instance||''))).join('')+
        '</select></label><button data-connection="connect">Connect selected instance</button>':'')+
      '</details>'+
      '<details><summary>Direct SSH connection (without Vast API)</summary>'+
      '<label for="manualSshInput">Vast SSH command (ssh -p PORT root@HOST)</label>'+
      '<input id="manualSshInput" type="text" autocomplete="off" dir="ltr" placeholder="ssh -p 12345 root@ssh.vast.ai">'+
      '<button data-connection="connect-ssh">Connect using SSH</button></details></section>';
}

document.addEventListener('click',async e=>{
    const button=e.target.closest('button');
    if(!button)return;
    if(button.dataset.tools==='open'){document.getElementById('workflowTools').showModal();return}
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
        if(action==='connect')body.instance=Number(document.getElementById('vastInstanceSelect')?.value||0);
        if(action==='connect-ssh')body.ssh=document.getElementById('manualSshInput')?.value||'';
        const result=await post('/connection',body);
        if(action==='hf'){document.getElementById('hfToken').value='';document.getElementById('hfSaved').textContent='Saved in Windows credential vault';return}
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
    try{localStorage.setItem(workspaceLayoutKey,JSON.stringify({episode:ep.id,page}))}catch{}
}

function restoreWorkspace(previous){
    let saved=null;
    try{saved=JSON.parse(localStorage.getItem(workspaceLayoutKey))}catch{}
    ep=db.episodes.find(item=>item.id===(previous.episode||saved?.episode))||ep;
    if(Number.isInteger(saved?.page)&&saved.page>=0&&saved.page<spaces.length)page=saved.page;
}

function chooseWorkspaceSession(){
    const dialog=document.createElement('dialog');
    dialog.id='workspaceSession';
    dialog.setAttribute('aria-labelledby','workspaceSessionTitle');
    dialog.innerHTML='<h2 id="workspaceSessionTitle">Open AXDAR Studio</h2>'+
        '<p>Start a fresh connection or pick up your previous workspace. Saved keys and automatic connection settings stay available.</p>'+
        '<p id="workspaceSessionError" class="danger" role="alert" hidden></p>'+
        '<div class="row"><button class="primary" data-session-mode="new">New session</button>'+
        '<button data-session-mode="resume" autofocus>Resume previous session</button></div>';
    document.body.append(dialog);
    dialog.addEventListener('cancel',event=>event.preventDefault());
    dialog.showModal();
    return new Promise(resolve=>{
        dialog.addEventListener('click',async event=>{
            const mode=event.target.closest('button[data-session-mode]')?.dataset.sessionMode;
            if(!mode)return;
            const buttons=[...dialog.querySelectorAll('button')];
            buttons.forEach(button=>button.disabled=true);
            const message=dialog.querySelector('#workspaceSessionError');
            message.hidden=true;
            try{
                const previous=await post('/workspace-session',{mode});
                if(mode==='resume')restoreWorkspace(previous);
                dialog.close();
                dialog.remove();
                resolve();
            }catch(error){
                message.textContent=error.message;
                message.hidden=false;
                buttons.forEach(button=>button.disabled=false);
            }
        });
    });
}

document.addEventListener('change',event=>{
    if(event.target.id==='episodeMenu')queueMicrotask(rememberWorkspace);
});
document.addEventListener('click',event=>{
    if(event.target.closest('button[data-page],button[data-shot],button[data-open-shot]'))queueMicrotask(rememberWorkspace);
});
