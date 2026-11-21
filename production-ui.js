const productionDefaults = {instance:'',host:'',port:22,rate:0,budget:0,retries:2,minutes:60,shot:'',policy:'pause'};
let productionForm = {...productionDefaults,...JSON.parse(localStorage.getItem('axdar.production')||'{}')};
let rentals = [];

let lastProductionNotice='';
function productionNotice(p){
    if(!p || !['finished','needs attention','deployment failed','connection lost','limit reached'].includes(p.status))return;
    const key=[p.id||p.episode,p.dispatch,p.status,p.error].join('|');
    if(key===lastProductionNotice)return;
    lastProductionNotice=key;
    const episodeTitle=db.episodes.find(e=>e.id===p.episode)?.title||'Production';
    $('#notice').textContent=episodeTitle+' · '+p.status+(p.error?' · '+p.error:'');
}
function productionPanel(){
    const p=state.production||{}, counts=Object.values(p.shots||{});
    const done=counts.filter(x=>x==='done').length;
    const failed=counts.filter(x=>x==='error'||x==='uncertain').length;
    const pending=Math.max(0,(p.total||0)-done-failed);
    const field=(k,label,type='number',step='1')=>'<label>'+label+'<input data-prod-field="'+k+'" type="'+type+'" step="'+step+'" value="'+esc(String(productionForm[k]))+'"></label>';
    const action=(k,label)=>'<button data-production="'+k+'">'+label+'</button>';
    const options=rentals.map(r=>option(String(r.id),'#'+r.id+' Â· '+r.gpu+' Â· $'+(r.rate??'?')+'/h',productionForm.instance)).join('');
    const title=p.episode?db.episodes.find(e=>e.id===p.episode)?.title:'';
    return connectionPanel()+'<section class="panel"><h2>Vast production</h2><div class="row"><label>Rented instance<select data-prod-field="instance">'+option('','Verified SSH host',productionForm.instance)+options+'</select></label>'+action('instances','Refresh instances')+'</div>'+
      (!productionForm.instance?'<div class="row">'+field('host','SSH host','text')+field('port','SSH port')+'</div>':'')+
      '<div class="row">'+field('rate','GPU rate ($/hour)','number','0.001')+field('budget','Budget ($, 0 = unlimited)','number','0.01')+field('minutes','Session minutes')+field('retries','Transient retries (0â€“5)')+'</div>'+
      '<label>At time or budget limit<select data-prod-field="policy">'+option('pause','Pause worker',productionForm.policy)+option('stop-instance','Stop selected Vast instance after output sync',productionForm.policy)+'</select></label>'+
      '<div class="actions row">'+action('start','Start Production')+action('pause','Pause after shot')+action('resume','Resume Production')+action('stop','Stop after shot')+(p.healthy===false&&p.status!=='stopped'?action('sync','Reconnect status')+action('archive','Archive disconnected session'):'')+'</div>'+
      '<small>Pause and stop finish the current shot. Vast billing continues until you stop the instance on Vast.</small>'+
      (p.status?'<p role="status">'+esc(title||'Production')+' Â· '+esc(p.status)+' Â· '+(p.healthy===false?'Disconnected':'Worker status')+'</p>'+
      '<p>'+done+' complete Â· '+pending+' pending Â· '+failed+' need attention'+(p.current?' Â· Current shot '+esc(p.current):'')+'</p>'+
      '<p>Runtime '+Math.round((p.elapsed||0)/60)+' min Â· Estimated GPU spend $'+Number(p.estimate||0).toFixed(3)+(done?' Â· $'+(Number(p.estimate||0)/done).toFixed(3)+'/completed shot':'')+(p.meanSeconds?' Â· About '+Math.ceil(pending*p.meanSeconds/60)+' min remaining':'')+'</p>':'')+
      (p.warning||p.error?'<p class="danger">'+esc(p.error||p.warning)+'</p>':'')+
      (failed?'<label>Failed shot<select data-prod-field="shot">'+option('','Choose a shot',productionForm.shot)+Object.entries(p.shots||{}).filter(([,v])=>v==='error').map(([id])=>option(id,id,productionForm.shot)).join('')+'</select></label>'+action('retry','Retry failed shot'):'')+'</section>';
}
function rememberProduction(target){
    const key=target.dataset.prodField;
    if(!key)return;
    productionForm[key]=target.value;
    if(key==='instance'){
        const row=rentals.find(r=>String(r.id)===target.value);
        if(row){productionForm.rate=row.rate||0;productionForm.host=row.host||'';productionForm.port=row.port||22}
    }
    localStorage.setItem('axdar.production',JSON.stringify(productionForm));
}
document.addEventListener('input',e=>rememberProduction(e.target));
document.addEventListener('change',e=>{if(e.target.dataset.prodField==='instance'){rememberProduction(e.target);render()}});
function reportSubmission(result){
    reportStatus(result.submitted+' shot(s) submitted.');
    const skipped=Object.entries(result.blocked||{});
    if(skipped.length)$('#notice').textContent=skipped.map(([id,reasons])=>{
        const shot=ep.shots.find(s=>s.id===id);
        return (shot?titleShot(shot):id)+': '+reasons.join(' · ');
    }).join(' / ');
}
async function productionAction(action,archive=null){
    let result;
    if(action==='instances'){
        rentals=await post('/production',{action});
        if(!rentals.some(x=>String(x.id)===productionForm.instance))productionForm.instance='';
    }else{
        if(action==='start'){await flushEdits();if(productionForm.instance && state.connection?.state!=='Ready')throw Error('Connect to Vast and wait for Ready first')}
        result=await post('/production',{...productionForm,action,episode:ep.id,archive});
        state=await(await fetch('/status')).json();
    }
    render();
    if(action==='start'&&result)reportSubmission(result);
}
