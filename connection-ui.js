function connectionPanel(){
    const c=state.connection||{};
    const details=c.instance&&!['Waiting for Vast','Unavailable'].includes(c.state)?
      ' · Instance #'+esc(c.instance)+(c.gpu?' · '+esc(c.gpu):''):'';
    return '<section class="panel" aria-labelledby="vastConnectionTitle">'+
      '<h2 id="vastConnectionTitle">Vast connection</h2>'+
      '<p role="status">'+esc(c.state||'Looking for an instance')+details+'</p>'+
      connectionIndicators(c,['api','ssh','comfy','gpu'])+
      (c.error?'<p class="danger">'+esc(c.error)+'</p>':'')+
      '<div class="row"><button data-connection="refresh">Refresh instances</button>'+
      '<button data-tools="open">Terminal &amp; HF Token</button></div>'+
      '<details '+(c.indicators?.api==='red'?'open':'')+'><summary>Vast API key</summary>'+
      '<label for="vastApiKey">Vast API key</label><input id="vastApiKey" type="password" autocomplete="off" dir="ltr">'+
      '<button data-connection="key">Save API key</button></details></section>';
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
        const body={action};
        if(action==='key')body.key=document.getElementById('vastApiKey').value;
        if(action==='hf')body.token=document.getElementById('hfToken').value;
        if(action==='command')body.command=document.getElementById('remoteCommand').value;
        const result=await post('/connection',body);
        if(action==='hf'){document.getElementById('hfToken').value='';document.getElementById('hfSaved').textContent='Saved in Windows credential vault';return}
        if(action==='command'){
            document.getElementById('remoteOutput').textContent=result.output;
            return;
        }
        state=await(await fetch('/status')).json();
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
