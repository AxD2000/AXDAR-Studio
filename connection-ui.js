function connectionPanel(){
    const c=state.connection||{};
    return '<section class="panel"><h2>Vast connection</h2>'+connectionIndicators(c)+'<p role="status">'+esc(c.state||'Disconnected')+'</p>'+
      (c.error?'<p class="danger">'+esc(c.error)+'</p>':'')+
      '<label>Vast API key (Windows credential vault)<input id="vastApiKey" type="password" autocomplete="off"></label><button data-connection="key">Save API key</button>'+
      '<div class="row"><button data-connection="instances">Discover instances</button><select id="connectionInstance">'+option('','Choose instance',String(c.instance||''))+(c.instance&&!rentals.some(r=>String(r.id)===String(c.instance))?option(String(c.instance),'#'+c.instance,String(c.instance)):'')+rentals.map(r=>option(String(r.id),'#'+r.id+' '+r.gpu+' '+(r.status||''),String(c.instance||''))).join('')+'</select></div>'+
      '<label><input type="checkbox" id="connectionAuto" '+(c.auto?'checked':'')+'> Auto-connect selected running instance on Studio startup</label>'+
      '<div class="row"><button data-connection="connect">Connect / Retry</button><button data-connection="disconnect">Disconnect</button></div>'+
      '<small>Stopped instances stay stopped. SSH key and verified host trust must be configured once. Disconnect does not stop Vast billing.</small>'+
      '<button data-tools="open">Terminal &amp; HF Token</button></section>';
}
document.addEventListener('click',async e=>{
    if(e.target.dataset.tools==='open'){document.getElementById('workflowTools').showModal();return}
    if(e.target.dataset.tools==='close'){document.getElementById('hfToken').value='';document.getElementById('workflowTools').close();return}
    const action=e.target.dataset.connection;
    if(!action)return;
    e.target.disabled=true;
    try{
        const body={action};
        if(action==='key')body.key=document.getElementById('vastApiKey').value;
        if(action==='connect'){
            body.instance=document.getElementById('connectionInstance').value||state.connection?.instance;
            body.auto=document.getElementById('connectionAuto').checked;
        }
        if(action==='hf')body.token=document.getElementById('hfToken').value;
        if(action==='command')body.command=document.getElementById('remoteCommand').value;
        const result=await post('/connection',body);
        if(action==='hf'){document.getElementById('hfToken').value='';document.getElementById('hfSaved').textContent='Saved in Windows credential vault';return}
        if(action==='key')document.getElementById('vastApiKey').value='';
        if(action==='command'){
            const out=document.getElementById('remoteOutput');
            if(out)out.textContent=result.output;
            return;
        }
        if(action==='instances')rentals=result;
        state=await(await fetch('/status')).json();
        if(state.connection?.instance){
            productionForm.instance=String(state.connection.instance);
            localStorage.setItem('axdar.production',JSON.stringify(productionForm));
        }
        render();
    }catch(err){const dialog=document.getElementById('workflowTools');if(dialog.open)document.getElementById('remoteOutput').textContent=err.message;else document.getElementById('notice').textContent=err.message}
    finally{e.target.disabled=false}
});

function connectionIndicators(c){
    const colors={grey:'#8b929e',red:'#f05252',green:'#22c55e'};
    const meanings={grey:'No current check / disconnected',red:'Failed / interrupted',green:'Passed'};
    return '<div class="row" aria-label="Connection checks">'+[['api','Vast API'],['ssh','SSH key'],['comfy','ComfyUI'],['gpu','GPU']].map(([key,label])=>{
        const value=c.indicators?.[key];const color=colors[value]?value:'grey';
        return '<span role="status" aria-label="'+label+': '+meanings[color]+'" title="'+label+': '+meanings[color]+'"><span aria-hidden="true" style="display:inline-block;width:11px;height:11px;border-radius:50%;background:'+colors[color]+';margin-right:6px"></span>'+label+'</span>';
    }).join('')+'</div>';
}

document.addEventListener('close',e=>{if(e.target.id==='workflowTools')document.getElementById('hfToken').value=''},true);
