let confirmedCharges=null;
document.addEventListener('click',async event=>{
    const button=event.target.closest('[data-vast-charges]');
    if(!button)return;
    button.disabled=true;
    try{
        confirmedCharges=await post('/production',{action:'charges',instance:state.connection?.instance});
        render();
    }catch(error){reportError(error)}finally{button.disabled=false}
});
function productionMetrics(){
    const p=state.production||{}, money=value=>'$'+Number(value||0).toFixed(3);
    return '<section class="panel"><h2>Production costs</h2>'+
      (p.rate!=null?'<p>Rental rate '+money(p.rate)+'/hour</p>':'')+
      (p.instanceUptimeMinutes!=null?'<p>Vast-reported uptime '+Number(p.instanceUptimeMinutes).toFixed(0)+' min</p>':'')+
      (p.elapsed!=null?'<p>Session '+Math.round(p.elapsed/60)+' min · Rendering '+Math.round((p.renderSeconds||0)/60)+' min · Other time '+Math.round((p.overheadSeconds||0)/60)+' min · Retry time '+Math.round((p.retrySeconds||0)/60)+' min</p>'+
      '<p>Estimated session '+money(p.estimate)+' · Episode '+money(p.projectEstimate)+' · Month '+money(p.monthlyEstimate)+'</p>'+
      '<p>'+Math.round(p.acceptedSeconds||0)+' s accepted'+(p.acceptedSecondsPerDollar?' · '+Number(p.acceptedSecondsPerDollar).toFixed(1)+' accepted seconds per estimated dollar':'')+'</p>':'')+
      (p.monthlyBudgetSource?'<p>'+esc(p.monthlyBudgetSource)+'</p>':'')+
      (confirmedCharges?'<p>Vast confirmed charges ('+esc(confirmedCharges.month)+' UTC) · Account '+money(confirmedCharges.confirmedAccount)+(confirmedCharges.confirmedInstance!=null?' · Instance '+money(confirmedCharges.confirmedInstance):'')+'</p>':'')+
      '<button data-vast-charges>Read confirmed Vast charges</button></section>';
}

function takeReview(v){
    if(!v.review||v.status!=='done')return '';
    const button=(action,label)=>'<button data-take-review="'+action+'" data-review-shot="'+esc(v.id)+'" data-review-version="'+esc(v.version)+'">'+label+'</button>';
    const quality=v.quality||{};
    return '<p>Take review · '+esc(v.review)+' · Technical QC '+esc(quality.technical||'pending')+
      (quality.width?' · '+quality.width+'×'+quality.height+' · '+quality.frames+' decoded frames':'')+
      ' · Visual '+esc(quality.visual||'review required')+(v.renderSeconds?' · '+Math.round(v.renderSeconds)+' s processing time':'')+'</p>'+
      (quality.reason?'<p>'+esc(quality.reason)+'</p>':'')+
      (v.preview&&!v.video?'<video controls preload="metadata" src="'+esc(v.preview)+'"></video>':'')+
      (!v.preview&&!v.video?button('preview','Load review preview'):'')+
      (quality.technical!=='passed'?button('recheck','Recheck technical QC'):'')+
      (quality.technical==='passed'&&(v.preview||v.video)&&v.review!=='accepted'?button('accepted','Accept take'):'')+
      (v.review!=='rejected'&&(v.preview||v.video)?button('rejected','Reject take'):'');
}
function reportSubmission(result){
    reportStatus(result.submitted+' shot(s) queued.');
    const skipped=Object.entries(result.blocked||{});
    if(skipped.length)$('#notice').textContent=skipped.map(([id,reasons])=>{
        const shot=ep.shots.find(s=>s.id===id);
        return (shot?titleShot(shot):id)+': '+reasons.join(' · ');
    }).join(' / ');
}
document.addEventListener('click',async event=>{
    const b=event.target.closest('[data-take-review]');
    if(!b)return;
    b.disabled=true;
    try{
        const choice=b.dataset.takeReview;
        await post('/production',{action:'review',episode:ep.id,shot:b.dataset.reviewShot,version:b.dataset.reviewVersion,
            decision:['accepted','rejected'].includes(choice)?choice:null,recheck:choice==='recheck'});
        state=await(await fetch('/status')).json();render();
    }catch(error){reportError(error)}finally{if(b.isConnected)b.disabled=false}
});
