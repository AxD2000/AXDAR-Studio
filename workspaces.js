const workspaceItems=[
    {title:'Story & Scenes',symbol:'▤',description:'Script, scenes and storyboard'},
    {title:'Shot Lab',symbol:'▣',description:'Prompts, character references and shot approvals'},
    {title:'Asset Vault',symbol:'▧',description:'Project reference photos and voices'},
    {title:'Production',symbol:'◈',description:'Vast connection and direct ComfyUI submission'},
    {title:'Workflow',symbol:'❖',description:'Install, validate and monitor ComfyUI workflows'}
];
const spaces=workspaceItems.map(item=>item.title);
const symbols=workspaceItems.map(item=>item.symbol);
const descriptions=workspaceItems.map(item=>item.description);
let storyTab='Script', selectedId='';
function selectedShot(){return ep.shots.find(s=>s.id===selectedId)||ep.shots[0]}
function heading(s,i){return esc(s.sourceHeading||('Shot '+String(i+1).padStart(2,'0')))}
function renderStory(){
    const subnav=['Script','Scenes','Storyboard'].map(k=>'<button class="'+(storyTab===k?'on':'')+'" data-storytab="'+k+'">'+k+'</button>').join('');
    const body=storyTab==='Script'?scriptPanel():scenePanels(storyTab==='Storyboard');
    return '<div class="toolbar">'+subnav+'</div>'+body;
}
function renderShots(){
    const shot=selectedShot();
    return '<div class="lab"><section class="panel"><button class="primary" data-story-action="file">Import Episode File</button>'+
        '<h2>Shot List</h2>'+sceneBrowser()+'</section><div>'+(shot?shotPanel(shot):'<section class="panel">Import an episode to see its shots.</section>')+'</div></div>';
}
function render(){
    if(migrationNeeded()){renderMigration();return}
    if(!ep)return;
    $('#heading').textContent=spaces[page];$('#caption').textContent=descriptions[page];
    $('#nav').innerHTML=spaces.map((n,i)=>'<button data-page="'+i+'" aria-current="'+(page===i?'page':'false')+'" class="'+(page===i?'on':'')+'"><span aria-hidden="true">'+symbols[i]+'</span>'+n+'</button>').join('');
    $('#episodeMenu').innerHTML=db.episodes.map((e,i)=>option(e.id,(i+1)+'. '+(e.title||'Untitled episode'),ep.id)).join('');
    $('#projectTitle').textContent=ep.title||'Untitled episode';
    $('#projectCount').textContent=(ep.scenes||[]).length+' scenes · '+ep.shots.length+' shots · '+ep.shots.filter(s=>s.approved).length+' approved';
    $('#app').innerHTML=[renderStory,renderShots,()=>characterRegistry()+libraryView(),sessionView,workflowWorkspace][page]();
    if(page===1&&selectedShot())loadShotReferences();
}
document.addEventListener('click',e=>{
    const b=e.target.closest('button');if(!b)return;
    if(b.dataset.storytab){storyTab=b.dataset.storytab;render()}
    if(b.dataset.shot||b.dataset.openShot){selectedId=b.dataset.shot||b.dataset.openShot;page=1;render()}
});
document.addEventListener('change',e=>{
    if(e.target.id==='episodeMenu'){ep=db.episodes.find(x=>x.id===e.target.value);selectedId='';render()}
});
