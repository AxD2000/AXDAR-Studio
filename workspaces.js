const workspaceItems=[
    {title:'Story & Scenes',symbol:'▤',description:'Script, scene breakdown and continuity'},
    {title:'Shot Lab',symbol:'▣',description:'Prompts, character references and shot approvals'},
    {title:'Asset Vault',symbol:'▧',description:'Project reference photos and voices'},
    {title:'Production',symbol:'◈',description:'Workflow, generation queue and take history'},
    {title:'Video Editor',symbol:'▥',description:'Assemble and review the episode'},
    {title:'Economics',symbol:'∑',description:'Estimate GPU time and retry costs'},
    {title:'Settings',symbol:'⚙',description:'Local preferences and workflow configuration'},
    {title:'Workflow',symbol:'❖',description:'Install, validate and monitor ComfyUI workflows'}
];
const spaces=workspaceItems.map(item=>item.title);
const symbols=workspaceItems.map(item=>item.symbol);
const descriptions=workspaceItems.map(item=>item.description);
let storyTab='Script', selectedId='', estimateEpisode='';
let estimate={shots:1,failure:10,rate:.55,minutes:6};
function selectedShot(){return ep.shots.find(s=>s.id===selectedId)||ep.shots[0]}
function heading(s,i){return esc(s.sourceHeading||('Shot '+String(i+1).padStart(2,'0')))}
function shotList(){
    const selected=selectedShot();
    return '<div class="shot-list">'+ep.shots.map((s,i)=>'<button data-shot="'+esc(s.id)+'" class="'+(s===selected?'on':'')+'">'+heading(s,i)+'</button>').join('')+'</div>';
}
function renderStory(){
    const subnav=['Script','Scenes','Storyboard','Continuity'].map(k=>'<button class="'+(storyTab===k?'on':'')+'" data-storytab="'+k+'">'+k+'</button>').join('');
    const body=storyTab==='Script'?scriptPanel():storyTab==='Continuity'?bible():scenePanels(storyTab==='Storyboard');
    return '<div class="toolbar">'+subnav+'</div>'+body;
}
function renderShots(){
    const shot=selectedShot();
    return '<div class="lab"><section class="panel"><button class="primary" data-story-action="file">Import Episode File</button>'+
        '<h2>Shot List</h2>'+sceneBrowser()+'</section><div>'+(shot?shotPanel(shot):'<section class="panel">Import an episode to see its shots.</section>')+'</div></div>';
}
function costOutput(){
    const attempts=estimate.shots/(1-estimate.failure/100),hours=attempts*estimate.minutes/60,base=estimate.shots*estimate.minutes/60*estimate.rate;
    return '<div class="metric"><small>Estimated GPU cost</small><strong>$'+(hours*estimate.rate).toFixed(2)+'</strong></div><p>'+hours.toFixed(2)+' GPU hours · '+Math.ceil(attempts)+' expected attempts</p><p>Baseline $'+base.toFixed(2)+' · Retry overhead $'+(hours*estimate.rate-base).toFixed(2)+'</p>';
}
function renderEconomics(){
    if(estimateEpisode!==ep.id){estimate.shots=ep.shots.length;estimateEpisode=ep.id}
    const fields=[['shots','Target shots',0,10000,1],['failure','Failure probability (%)',0,95,1],['rate','GPU hourly rate ($)',0,100,.01],['minutes','Minutes per attempt',.1,1440,.1]];
    return productionMetrics()+'<div class="layout"><section class="panel"><h2>Cost parameters</h2>'+fields.map(([key,label,min,max,step])=>'<label>'+label+'<input data-cost="'+key+'" type="number" min="'+min+'" max="'+max+'" step="'+step+'" value="'+estimate[key]+'"></label>').join('')+'</section><section class="panel"><h2>GPU-only estimate</h2><div id="costOutput">'+costOutput()+'</div><small>Assumes independent retries at a fixed rate. Storage, idle time and bandwidth are excluded.</small></section></div>';
}
function renderSettings(){
    return '<section class="panel"><h2>Interface</h2><label>Color theme<select id="themeChoice">'+option('dark','Dark',document.documentElement.dataset.theme)+option('cream','Cream',document.documentElement.dataset.theme)+'</select></label></section>'+workflowPanel();
}
function render(){
    if(migrationNeeded()){renderMigration();return}
    if(!ep)return;
    $('#heading').textContent=spaces[page];$('#caption').textContent=descriptions[page];
    $('#nav').innerHTML=spaces.map((n,i)=>'<button data-page="'+i+'" aria-current="'+(page===i?'page':'false')+'" class="'+(page===i?'on':'')+'"><span aria-hidden="true">'+symbols[i]+'</span>'+n+'</button>').join('');
    $('#episodeMenu').innerHTML=db.episodes.map((e,i)=>option(e.id,(i+1)+'. '+(e.title||'Untitled episode'),ep.id)).join('');
    $('#projectTitle').textContent=ep.title||'Untitled episode';
    $('#projectCount').textContent=(ep.scenes||[]).length+' scenes · '+ep.shots.length+' shots · '+ep.shots.filter(s=>s.approved).length+' approved';
    $('#app').innerHTML=[renderStory,renderShots,()=>characterRegistry()+libraryView(),sessionView,renderEditor,renderEconomics,renderSettings,workflowWorkspace][page]();
    if(page===1&&selectedShot())loadShotReferences();
}
document.addEventListener('click',e=>{
    const b=e.target.closest('button');if(!b)return;
    if(b.dataset.storytab){storyTab=b.dataset.storytab;render()}
    if(b.dataset.shot||b.dataset.openShot){selectedId=b.dataset.shot||b.dataset.openShot;page=1;render()}
});
document.addEventListener('change',e=>{
    if(e.target.id==='episodeMenu'){ep=db.episodes.find(x=>x.id===e.target.value);selectedId='';render()}
    if(e.target.id==='themeChoice'){document.documentElement.dataset.theme=e.target.value;localStorage.setItem('axdar.theme',e.target.value);showTheme()}
});
document.addEventListener('input',e=>{
    const k=e.target.dataset.cost;if(!k)return;
    if(!e.target.value||!e.target.validity.valid){$('#costOutput').textContent='Enter a value within the indicated range.';return}
    estimate[k]=e.target.valueAsNumber;$('#costOutput').innerHTML=costOutput();
});
