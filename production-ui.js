const productionDefaults = {rate:0,budget:0,retries:2,minutes:60,shot:'',policy:'pause',shotRetries:1,episodeBudget:0,monthBudget:0,expectedWidth:"",expectedHeight:"",backupDrive:false,webhook:false,visual:false,lifecycle:'manual'};
let productionForm = {...productionDefaults,...JSON.parse(localStorage.getItem('axdar.production')||'{}')};
let rentals = [], confirmedCharges=null, driveConnected=null;

let lastProductionNotice='';
function productionNotice(p){
    if(!p || !['finished','needs attention','deployment failed','connection lost','limit reached'].includes(p.status))return;
    const key=[p.id||p.episode,p.dispatch,p.status,p.error].join('|');
    if(key===lastProductionNotice)return;
    lastProductionNotice=key;
    const episodeTitle=db.episodes.find(e=>e.id===p.episode)?.title||'Production';
    $('#notice').textContent=episodeTitle+' · '+p.status+(p.error?' · '+p.error:'');
}
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
      '<button data-production="charges">Read confirmed Vast charges</button></section>';
}

function servicesPanel(p){
    const checked=key=>productionForm[key]?'checked':'';
    const life=p.lifecycle||{}, backup=p.backup||{};
    return '<section class="panel"><h2>Archive, review and notifications</h2>'+
      '<label>Google OAuth desktop client JSON<input type="file" id="driveClient" accept=".json,application/json"></label>'+
      '<button data-production="drive-status">Check Drive authorization</button>'+
      (driveConnected===null?'':'<p>'+ (driveConnected?'Drive authorized.':'Drive authorization required.')+'</p>')+
      '<label><input type="checkbox" data-prod-field="backupDrive" '+checked('backupDrive')+'> Back up this run to my authorized Google Drive</label>'+
      '<label>HTTPS notification webhook<input type="password" autocomplete="off" id="notificationWebhook"></label>'+
      '<label>HTTPS visual reviewer endpoint<input type="password" autocomplete="off" id="visualEndpoint"></label>'+
      '<button data-production="service-settings">Save endpoint settings</button>'+
      '<label><input type="checkbox" data-prod-field="webhook" '+checked('webhook')+'> Deliver notifications for this run</label>'+
      '<label><input type="checkbox" data-prod-field="visual" '+checked('visual')+'> Send sample frames to my visual reviewer</label>'+
      '<label>After production<select data-prod-field="lifecycle">'+option('manual','Manual instance management',productionForm.lifecycle)+option('rehearse','Rehearse ten-minute termination countdown',productionForm.lifecycle)+'</select></label>'+
      (backup.state?'<p>Drive · '+esc(backup.state)+(backup.verified!=null?' · '+backup.verified+' takes verified':'')+(backup.error?' · '+esc(backup.error):'')+'</p>':'')+
      (life.status?'<p>Lifecycle · '+esc(life.status)+(life.deadline?' · '+new Date(life.deadline*1000).toLocaleTimeString():'')+(life.reason?' · '+esc(life.reason):'')+'</p>':'')+
      (life.status==='countdown'?'<button data-production="cancel-countdown">Cancel countdown</button>':'')+
      (p.id&&!p.maintenance?'<button data-production="maintenance">Retry backup and notification delivery</button>':'')+
      (p.notifications?.length?'<details><summary>Production notifications ('+p.notifications.length+')</summary><ul>'+p.notifications.map(n=>'<li>'+esc(n.kind)+' · '+esc(n.delivery)+(n.detail?' · '+esc(n.detail):'')+'</li>').join('')+'</ul></details>':'')+'</section>';
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
      (v.review!=='rejected'&&(v.preview||v.video)?button('rejected','Reject take'):'')+
      (v.review==='rejected'&&v.remote&&page===3&&v.preview?'<button data-archive-shot="'+esc(v.id)+'" data-archive-version="'+esc(v.version)+'">Regenerate this shot from archived inputs</button>':'');
}
function productionPanel(){
    const p=state.production||{}, counts=Object.values(p.shots||{});
    const done=counts.filter(x=>x==='done').length;
    const failed=counts.filter(x=>x==='error'||x==='uncertain').length;
    const pending=Math.max(0,(p.total||0)-done-failed);
    const field=(k,label,type='number',step='1')=>'<label>'+label+'<input data-prod-field="'+k+'" type="'+type+'" step="'+step+'" value="'+esc(String(productionForm[k]))+'"></label>';
    const action=(k,label)=>'<button data-production="'+k+'">'+label+'</button>';
    const title=p.episode?db.episodes.find(e=>e.id===p.episode)?.title:'';
    const current=state.connection||{};
    const target=current.ssh?current.ssh.host+':'+current.ssh.port:current.instance?'Instance #'+current.instance:'No server connected';
    return '<section class="panel"><h2>Vast production</h2><p>'+esc(target)+'</p>'+
      '<div class="row">'+field('rate','GPU rate ($/hour)','number','0.001')+field('budget','Session budget ($, 0 = unlimited)','number','0.01')+field('episodeBudget','Episode budget ($)','number','0.01')+field('monthBudget','Monthly budget ($)','number','0.01')+'</div>'+
      '<div class="row">'+field('minutes','Session minutes')+field('retries','Network retries (0–5)')+field('shotRetries','End-of-batch retries (0–5)')+'</div>'+
      '<div class="row">'+field('expectedWidth','Expected video width (optional)')+field('expectedHeight','Expected video height (optional)')+'</div>'+
      '<label>At time or budget limit<select data-prod-field="policy">'+option('pause','Pause worker',productionForm.policy)+ (current.instance?option('stop-instance','Stop selected Vast instance after output sync',productionForm.policy):'')+'</select></label>'+
      '<div class="actions row">'+action('start','Start Production')+action('pause','Pause after shot')+action('resume','Resume Production')+action('stop','Stop after shot')+(p.healthy===false&&p.status!=='stopped'?action('sync','Reconnect status')+action('archive','Archive disconnected session'):'')+'</div>'+
      '<small>Pause and stop finish the current shot. Vast billing continues until you stop the instance on Vast.</small>'+
      (p.status?'<p role="status">'+esc(title||'Production')+' Â· '+esc(p.status)+' Â· '+(p.healthy===false?'Disconnected':'Worker status')+'</p>'+
      '<p>'+done+' complete Â· '+pending+' pending Â· '+failed+' need attention'+(p.current?' Â· Current shot '+esc(p.current):'')+'</p>'+
      '<p>Runtime '+Math.round((p.elapsed||0)/60)+' min Â· Estimated GPU spend $'+Number(p.estimate||0).toFixed(3)+(done?' Â· $'+(Number(p.estimate||0)/done).toFixed(3)+'/completed shot':'')+(p.meanSeconds?' Â· About '+Math.ceil(pending*p.meanSeconds/60)+' min remaining':'')+'</p>':'')+
      (p.warning||p.error?'<p class="danger">'+esc(p.error||p.warning)+'</p>':'')+
      (failed?'<label>Failed shot<select data-prod-field="shot">'+option('','Choose a shot',productionForm.shot)+Object.entries(p.shots||{}).filter(([,v])=>v==='error').map(([id])=>option(id,id,productionForm.shot)).join('')+'</select></label>'+action('retry','Retry failed shot'):'')+'</section>'+productionMetrics()+servicesPanel(p);
}
function rememberProduction(target){
    const key=target.dataset.prodField;
    if(!key)return;
    productionForm[key]=target.type==='checkbox'?target.checked:target.value;
    localStorage.setItem('axdar.production',JSON.stringify(productionForm));
}
document.addEventListener('input',e=>rememberProduction(e.target));
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
    if(action==='charges'){
        confirmedCharges=await post('/production',{action,instance:state.connection?.instance});
        render();return;
    }
    if(action==='drive-status'){
        driveConnected=(await(await fetch('/drive/status')).json()).connected;
        render();return;
    }
    if(action==='service-settings'){
        await post('/production',{action,webhook:$('#notificationWebhook').value,visual:$('#visualEndpoint').value});
        render();reportStatus('Endpoint settings saved.');return;
    }
    const connection=state.connection||{};
    if(action==='start'){
        await flushEdits();
        if(connection.indicators?.ssh!=='green')throw Error('Connect to Vast and verify SSH first.');
    }
    const policy=!connection.instance&&productionForm.policy==='stop-instance'?'pause':productionForm.policy;
    result=await post('/production',{...productionForm,action,episode:ep.id,archive,
        instance:connection.instance||'',useConnection:true,policy});
    state=await(await fetch('/status')).json();
    render();
    if(action==='start'&&result)reportSubmission(result);
}

document.addEventListener('change',async event=>{
    const field=event.target;
    if(field.id!=='driveClient'||!field.files.length)return;
    const file=field.files[0];
    field.value='';
    // Create the tab during the click/change gesture before the localhost request.
    const authTab=window.open('about:blank','_blank');
    try{
        const {url}=await post('/drive/connect',{client:JSON.parse(await file.text())});
        if(authTab){authTab.opener=null;authTab.location.href=url}
        else window.location.href=url;
    }catch(error){authTab?.close();reportError(error)}
});
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
