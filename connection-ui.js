let sshConnectionDraft=null, instanceSelection=null;

function connectionPanel(){
    const c=state.connection||{};
    const current=instanceSelection??String(c.instance||'');
    const existing=current&&!rentals.some(r=>String(r.id)===current)?option(current,'#'+current,current):'';
    const instances=rentals.map(r=>option(String(r.id),'#'+r.id+' '+r.gpu+' '+(r.status||''),current)).join('');
    const address=sshConnectionDraft??(c.ssh?'ssh -p '+c.ssh.port+' root@'+c.ssh.host:'');
    const active=c.ssh?c.ssh.host+':'+c.ssh.port:c.instance?'Instance #'+c.instance:'';
    return '<div class="connection-panels">'+
      '<section class="panel" aria-labelledby="sshConnectionTitle"><h2 id="sshConnectionTitle">SSH connection</h2>'+
      '<label for="vastSshCommand">SSH command from Vast</label>'+
      '<input id="vastSshCommand" type="text" dir="ltr" autocomplete="off" spellcheck="false" placeholder="ssh -p PORT root@HOST" value="'+esc(address)+'">'+
      '<p class="muted">Copy the SSH command from your server’s Connect menu. AXDAR uses the SSH key already on this computer.</p>'+
      '<div class="row"><button data-connection="connect-ssh" class="primary">Connect using SSH</button><button data-connection="disconnect">Disconnect</button></div>'+
      '<label><input type="checkbox" id="connectionAuto" '+(c.auto?'checked':'')+'> Reconnect on Studio startup</label>'+
      '<p role="status">'+esc(c.state||'Disconnected')+(active?' · '+esc(active):'')+'</p>'+connectionIndicators(c,['ssh','comfy','gpu'])+
      (c.error?'<p class="danger">'+esc(c.error)+'</p>':'')+
      '<button data-tools="open">Terminal &amp; HF Token</button></section>'+
      '<section class="panel" aria-labelledby="vastApiTitle"><h2 id="vastApiTitle">Vast API</h2>'+
      '<label for="vastApiKey">Vast API key</label><input id="vastApiKey" type="password" autocomplete="off" dir="ltr">'+
      '<div class="row"><button data-connection="key">Save API key</button><button data-connection="instances">Discover instances</button></div>'+
      '<label for="connectionInstance">Rented instance</label><select id="connectionInstance">'+option('','Choose instance',current)+existing+instances+'</select>'+
      '<div class="row"><button data-connection="connect">Connect / Retry</button>'+connectionIndicators(c,['api'])+'</div>'+
      '<p class="muted">Saved in Windows Credential Locker. Use this panel to find and connect to a rented instance from your account.</p></section></div>';
}

document.addEventListener('input',event=>{
    if(event.target.id==='vastSshCommand')sshConnectionDraft=event.target.value;
});
document.addEventListener('change',event=>{
    if(event.target.id==='connectionInstance')instanceSelection=event.target.value;
});
document.addEventListener('click',async e=>{
    const button=e.target.closest('button');
    if(!button)return;
    if(button.dataset.tools==='open'){document.getElementById('workflowTools').showModal();return}
    if(button.dataset.tools==='close'){document.getElementById('hfToken').value='';document.getElementById('workflowTools').close();return}
    const action=button.dataset.connection;
    if(!action)return;
    button.disabled=true;
    try{
        const body={action};
        if(action==='key')body.key=document.getElementById('vastApiKey').value;
        if(action==='connect'){
            body.instance=document.getElementById('connectionInstance').value;
            if(!body.instance)throw Error('Choose a rented instance first.');
        }
        if(action==='connect-ssh')body.ssh=document.getElementById('vastSshCommand').value;
        if(action==='connect'||action==='connect-ssh')body.auto=document.getElementById('connectionAuto').checked;
        if(action==='hf')body.token=document.getElementById('hfToken').value;
        if(action==='command')body.command=document.getElementById('remoteCommand').value;
        const result=await post('/connection',body);
        if(action==='hf'){document.getElementById('hfToken').value='';document.getElementById('hfSaved').textContent='Saved in Windows credential vault';return}
        if(action==='key')document.getElementById('vastApiKey').value='';
        if(action==='command'){
            document.getElementById('remoteOutput').textContent=result.output;
            return;
        }
        if(action==='instances')rentals=result;
        state=await(await fetch('/status')).json();
        if(action==='connect'||action==='connect-ssh'){
            const instance=state.connection?.instance;
            const picked=rentals.find(row=>String(row.id)===String(instance));
            if(picked?.rate!=null)productionForm.rate=picked.rate;
            if(action==='connect-ssh')sshConnectionDraft=null;
            localStorage.setItem('axdar.production',JSON.stringify(productionForm));
        }
        render();
        if(action==='key')reportStatus('Vast API key saved.');
    }catch(err){const dialog=document.getElementById('workflowTools');if(dialog.open)document.getElementById('remoteOutput').textContent=err.message;else reportError(err)}
    finally{button.disabled=false}
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
