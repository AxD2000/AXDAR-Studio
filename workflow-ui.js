let workflowCatalog={active:null,profiles:[]};
let editingProfile='', mappingOpen=false;

async function loadWorkflows(){
    workflowCatalog=await post('/workflow/list',{});
    if(!workflowCatalog.profiles.some(p=>p.version===editingProfile))
        editingProfile=workflowCatalog.profiles.find((p,i)=>i===0&&p.format==='ComfyUI UI JSON'&&!p.executable)?.version||
            workflowCatalog.active||workflowCatalog.profiles[0]?.version||'';
}
function currentProfile(version=workflowCatalog.active){return workflowCatalog.profiles.find(p=>p.version===version)}
function profileLabel(p){return p.name+' · '+String(p.version).slice(0,12)}
function mappingField(field){return field.label+' ('+field.id+')'}
function workflowPanel(){
    const p=currentProfile(editingProfile),active=currentProfile();
    return '<section class="panel"><h2>ComfyUI workflow</h2><p>Active workflow · <strong>'+esc(active?profileLabel(active):'None selected')+'</strong></p>'+
        '<div class="row"><button data-wf="import">Import Workflow JSON</button><button data-tools="open">Terminal &amp; HF Token</button></div>'+
        (p?'<label>Stored workflow version<select id="profileChoice">'+workflowCatalog.profiles.map(x=>option(x.version,profileLabel(x),p.version)).join('')+'</select></label>'+
        '<p>'+esc(p.format)+' · '+p.nodeCount+' nodes · '+(p.issues.length?'Needs review':'Ready')+'</p>'+
        (p.executable?'':'<p>'+esc(p.issues.join(' '))+'</p>'+
            '<button data-wf="prepare" '+(state.connection?.state==='Ready'?'':'disabled')+'>Prepare workflow</button>')+
        '<div class="row"><button data-wf="select" '+(!p.executable||p.issues.length||p.version===workflowCatalog.active?'disabled':'')+'>Use this version</button></div>'+
        (p.executable?'<details id="workflowFields" '+(mappingOpen?'open':'')+'><summary>Review input and output mapping</summary>'+workflowMappingForm(p)+'</details>':'' ):'')+'</section>';
}
const roleLabels={positive:'Positive prompt',negative:'Negative prompt',duration:'Duration in seconds',frames:'Frame count'};
const numberLabels={fps:'Frames per second',fixedSeconds:'Fixed duration (seconds)',minSeconds:'Minimum seconds',maxSeconds:'Maximum seconds',minFrames:'Minimum frames',maxFrames:'Maximum frames',frameStep:'Frame count step',frameBase:'Frame count base'};
function workflowMappingForm(p){
    const m=p.mapping||{}, fields=p.fields||[];
    const select=(key,label,rows)=>'<label>'+label+'<select data-map-role="'+key+'">'+option('','Unassigned',m[key])+rows.map(([id,label])=>option(id,label,m[key])).join('')+'</select></label>';
    const multi=(role,label)=>{
        const chosen=m[role]||[], candidates=fields.filter(f=>f.roles?.includes(role));
        if(!candidates.length)return '';
        return '<fieldset><legend>'+label+'</legend><small>Number the mapped inputs in the order the workflow receives references. Leave unused inputs blank.</small>'+candidates.map(f=>{
            const i=chosen.indexOf(f.id);
            return '<label>'+esc(mappingField(f))+'<input type="number" min="1" step="1" data-map-order="'+role+'" data-field="'+esc(f.id)+'" value="'+(i<0?'':i+1)+'"></label>';
        }).join('')+'</fieldset>';
    };
    return '<p class="muted">Version '+esc(p.version)+'</p>'+
        (p.issues?.length?'<ul class="danger">'+p.issues.map(i=>'<li>'+esc(i)+'</li>').join('')+'</ul>':'<p>Mapping is ready.</p>')+
        '<label>ComfyUI base URL for node metadata<input id="inspectionUrl" type="url" placeholder="http://127.0.0.1:8188" value="'+esc(state.connection?.url||'')+'"></label><div class="row"><button data-wf="inspect">Read /object_info</button><button data-wf="metadata">Load object_info JSON file</button></div>'+
        Object.entries(roleLabels).map(([key,label])=>select(key,label,fields.filter(f=>f.roles?.includes(key)).map(f=>[f.id,mappingField(f)]))).join('')+
        select('output','Video output node',(p.outputs||[]).map(o=>[o.id,o.label+' ('+o.id+')']))+
        multi('images','Ordered image inputs')+multi('audio','Ordered audio inputs')+
        '<details><summary>Duration and frame constraints</summary><div class="form-pair">'+Object.entries(numberLabels).map(([key,label])=>'<label>'+label+'<input type="number" step="any" data-map-number="'+key+'" value="'+esc(m[key]??'')+'"></label>').join('')+'</div></details>'+
        '<details><summary>Runtime parameters and graph defaults</summary>'+fields.filter(f=>f.roles?.includes('parameter')).map(f=>'<label>'+esc(mappingField(f))+(f.required?' · required':'')+'<input data-map-param="'+esc(f.id)+'" placeholder="'+esc(JSON.stringify(f.value)??'JSON value')+'" value="'+esc(Object.hasOwn(m.parameters||{},f.id)?JSON.stringify(m.parameters[f.id]):'')+'"></label>').join('')+'</details>'+
        '<button class="primary" data-wf="map">Save version mapping</button>';
}
function readMapping(p){
    const mapping={...p.mapping,images:[],audio:[],parameters:{}};
    for(const t of document.querySelectorAll('[data-map-role]')){
        mapping[t.dataset.mapRole]=t.value;
    }
    for(const group of ['images','audio']){
        const ordered=[...document.querySelectorAll('[data-map-order="'+group+'"]')].filter(t=>t.value).map(t=>[t.valueAsNumber,t.dataset.field]);
        if(ordered.some(([v])=>!Number.isInteger(v)||v<1)||new Set(ordered.map(x=>x[0])).size!==ordered.length)throw Error('Reference positions must be unique positive integers.');
        mapping[group]=ordered.sort((a,b)=>a[0]-b[0]).map(x=>x[1]);
    }
    for(const t of document.querySelectorAll('[data-map-number]')){
        if(!t.value)delete mapping[t.dataset.mapNumber];else if(t.validity.valid)mapping[t.dataset.mapNumber]=t.valueAsNumber;else throw Error('Invalid frame or duration constraint.');
    }
    for(const t of document.querySelectorAll('[data-map-param]'))if(t.value.trim()){
        try{mapping.parameters[t.dataset.mapParam]=JSON.parse(t.value)}catch{throw Error('Enter valid JSON for '+t.dataset.mapParam+' (wrap text in double quotes).')}
    }
    return mapping;
}
async function updateWorkflow(path,payload,file){
    await flushEdits();
    const response=file?await uploadContent(path,file):await post(path,payload);
    editingProfile=response.profile.version;
    await loadWorkflows();
    const info=await(await fetch('/api')).json();adoptProject(info.project);
    render();
    if(path==='/workflow/import'&&response.profile.format==='ComfyUI UI JSON')prepareOriginalWorkflow();
}

let preparingOriginal=false,prepareKey='';
async function prepareOriginalWorkflow(force=false){
    const profile=currentProfile(editingProfile);
    if(!profile||profile.format!=='ComfyUI UI JSON'||state.connection?.state!=='Ready'||preparingOriginal)return;
    if(profile.executable&&!force)return;
    const key=profile.version+'|'+(state.connection.instance||state.connection.url);
    if(!force&&key===prepareKey)return;
    prepareKey=key;
    preparingOriginal=true;
    try{
        reportStatus('Preparing the original JSON on ComfyUI…');
        const result=await post('/workflow-manager',{action:'prepare',version:profile.version});
        if(editingProfile===profile.version)editingProfile=result.profile.version;
        await loadWorkflows();
        render();
        reportStatus(result.message||(result.profile.issues.length?'Review workflow mapping.':'Workflow prepared. Check Dependencies for the selected instance.'));
    }catch(error){reportError(error)}
    finally{preparingOriginal=false}
}
setInterval(()=>{
    if((page===7||page===6)&&typeof state!=='undefined'&&state?.connection&&!state.running)prepareOriginalWorkflow();
},3000);

document.addEventListener('toggle',e=>{if(e.target.id==='workflowFields')mappingOpen=e.target.open},true);
document.addEventListener('change',async e=>{
    const t=e.target;
    try{
        if(t.id==='profileChoice'){editingProfile=t.value;mappingOpen=true;render();return}
        if(!['workflowInput','metadataFile'].includes(t.id)||!t.files.length)return;
        const file=t.files[0];t.value='';mappingOpen=true;
        if(t.id==='workflowInput')await updateWorkflow('/workflow/import',{},file);
        else await updateWorkflow('/workflow/inspect',{version:editingProfile,metadata:JSON.parse((await file.text()).replace(/^\uFEFF/,''))});
        if(!preparingOriginal)reportStatus('Workflow version loaded.');
    }catch(err){reportError(err)}
});
document.addEventListener('click',async e=>{
    const b=e.target.closest('button[data-wf]');if(!b)return;
    const action=b.dataset.wf,version=editingProfile;
    if(action==='import'){$('#workflowInput').click();return}
    if(action==='metadata'){$('#metadataFile').click();return}
    if(action==='prepare'){await prepareOriginalWorkflow(true);return}
    b.disabled=true;
    try{
        if(action==='select')await updateWorkflow('/workflow/select',{version});
        if(action==='map')await updateWorkflow('/workflow/map',{version,mapping:readMapping(currentProfile(version))});
        if(action==='inspect')await updateWorkflow('/workflow/inspect',{version,url:$('#inspectionUrl').value.trim()});
        reportStatus(action==='select'?'Workflow selected.':action==='map'?'Mapping saved. Select the version to use it.':'Workflow metadata reviewed.');
    }catch(err){reportError(err)}finally{if(b.isConnected)b.disabled=false}
});
