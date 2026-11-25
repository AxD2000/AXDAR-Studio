let workflowCatalog={active:null,profiles:[]};
let editingProfile='';

async function loadWorkflows(){
    workflowCatalog=await post('/workflow/list',{});
    if(!workflowCatalog.profiles.some(p=>p.version===editingProfile))
        editingProfile=workflowCatalog.active||workflowCatalog.profiles[0]?.version||'';
}
function currentProfile(version=workflowCatalog.active){return workflowCatalog.profiles.find(p=>p.version===version)}
function profileLabel(p){return p.name+(p.packaged?' [Package]':' [Legacy]')+' · '+String(p.version).slice(0,12)}

function workflowPanel(){
    const p=currentProfile(editingProfile),active=currentProfile();
    const gb=p?.packaged?((p.totalModelBytes||0)/1e9).toFixed(2):'';
    return '<section class="panel" id="wmPackageBox">'+
        '<h2>Workflow Package</h2>'+
        '<div id="wmDropZone" class="wm-dropzone" tabindex="0" role="button" aria-label="Drop or choose Workflow Package (.zip)">'+
        '<strong>Drop Workflow Package (.zip) here or click to select</strong>'+
        '<p class="muted">Automatically uploads to /workspace, extracts, checks dependencies in the background, and installs.</p>'+
        '<button type="button" class="primary" data-wf="import">Import Workflow Package (.zip)</button>'+
        '</div>'+
        (workflowCatalog.profiles.length?
            '<label>Active package<select id="profileChoice">'+workflowCatalog.profiles.map(x=>option(x.version,profileLabel(x),(p||active)?.version)).join('')+'</select></label>':'')+
        (p?'<p class="muted"><strong>'+esc(p.name)+'</strong>'+(p.packaged?' · '+(p.packageNodes||[]).length+' nodes · '+(p.packageModels||[]).length+' models'+((p.totalModelBytes||0)>0?' ('+gb+' GB)':''):' · Legacy profile')+'</p>'+
            (p.issues?.length?'<ul class="danger">'+p.issues.map(i=>'<li>'+esc(i)+'</li>').join('')+'</ul>':'')+
            (p.version!==workflowCatalog.active&&p.executable&&!p.issues?.length?'<div class="row"><button type="button" data-wf="select">Use this package</button></div>':''):'')+
        '</section>';
}

async function importWorkflowFile(file){
    await flushEdits();
    const response=await uploadContent('/workflow/import',file);
    editingProfile=response.profile.version;
    await loadWorkflows();
    const info=await(await fetch('/api')).json();adoptProject(info.project);
    render();
    reportStatus('Workflow Package imported.');
    if(typeof queueAutoDeploy==='function'&&response.profile?.packaged){
        queueAutoDeploy(response.profile.version);
    }
}

document.addEventListener('change',async e=>{
    const t=e.target;
    try{
        if(t.id==='profileChoice'){
            editingProfile=t.value;
            const chosen=currentProfile(editingProfile);
            if(chosen?.executable&&!chosen.issues?.length&&chosen.version!==workflowCatalog.active){
                await flushEdits();
                await post('/workflow/select',{version:chosen.version});
                await loadWorkflows();
            }
            render();
            if(typeof pollWorkflow==='function')pollWorkflow();
            return;
        }
        if(t.id!=='workflowInput'||!t.files.length)return;
        const file=t.files[0];t.value='';
        await importWorkflowFile(file);
    }catch(err){reportError(err)}
});

document.addEventListener('click',async e=>{
    const drop=e.target.closest('#wmDropZone');
    const b=e.target.closest('button[data-wf]');
    if(drop&&!b){$('#workflowInput').click();return}
    if(!b)return;
    const action=b.dataset.wf,version=editingProfile;
    if(action==='import'){$('#workflowInput').click();return}
    b.disabled=true;
    try{
        if(action==='select'){
            await flushEdits();
            await post('/workflow/select',{version});
            await loadWorkflows();
            render();
            reportStatus('Workflow package selected.');
            if(typeof pollWorkflow==='function')pollWorkflow();
        }
    }catch(err){reportError(err)}finally{if(b.isConnected)b.disabled=false}
});

document.addEventListener('dragover',e=>{
    const zone=e.target.closest?.('#wmDropZone');
    if(!zone)return;
    e.preventDefault();
    zone.classList.add('wm-dragover');
});
document.addEventListener('dragleave',e=>{
    const zone=e.target.closest?.('#wmDropZone');
    if(zone)zone.classList.remove('wm-dragover');
});
document.addEventListener('drop',async e=>{
    const zone=e.target.closest?.('#wmDropZone');
    if(!zone)return;
    e.preventDefault();
    zone.classList.remove('wm-dragover');
    const file=e.dataTransfer?.files?.[0];
    if(!file)return;
    try{await importWorkflowFile(file)}catch(err){reportError(err)}
});
