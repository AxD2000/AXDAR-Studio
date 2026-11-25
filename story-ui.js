let migration=null,storyDrafts=new Map(),proposals=new Map(),selectedCharacter=null;
let preview=null, busyProject=false;

function migrationNeeded(){return migration?.required===true}
function renderMigration(){
    $('#heading').textContent='Upgrade project structure';
    $('#caption').textContent='Review the migration before editing this project.';
    $('#nav').replaceChildren();$('#episodeMenu').replaceChildren();
    $('#projectTitle').textContent='Existing project';
    const episodes=db.episodes||[],count=episodes.reduce((n,e)=>n+(e.shots||[]).length,0);
    $('#app').innerHTML='<section class="panel"><h2>Back up and migrate this project</h2><p>'+episodes.length+' episodes · '+count+' existing shots</p><p>AXDAR will save a backup before creating persistent scenes, character records and take identifiers. Existing shot IDs and production history stay linked.</p><button class="primary" data-story-action="migrate">Create backup and migrate</button></section>';
}
function reportError(err){$('#notice').textContent=err.message||String(err);$('#notice').scrollIntoView({block:'nearest'})}
function reportStatus(msg){$('#notice').textContent='';$('#saved').textContent=msg}
function reportSubmission(result){
    reportStatus(result.submitted+' shot(s) queued.');
    const skipped=Object.entries(result.blocked||{});
    if(skipped.length)$('#notice').textContent=skipped.map(([id,reasons])=>{
        const shot=ep.shots.find(s=>s.id===id);
        return (shot?titleShot(shot):id)+': '+reasons.join(' · ');
    }).join(' / ');
}
function adoptProject(project){
    const episodeId=ep?.id;db=project;ep=db.episodes.find(e=>e.id===episodeId)||db.episodes.at(-1);
    preview=null;
}
async function flushEdits(){
    if(timer){clearTimeout(timer);timer=null}
    if(editsPending)await save();
    await saveQueue;
    if(saveError)throw Error(saveError);
}
async function projectAction(path,body){
    if(busyProject)throw Error('A project change is being saved. Try again when it finishes.');
    busyProject=true;
    try{
        await flushEdits();
        const result=await post(path,{...body,edit:db._edit});
        if(result.project)adoptProject(result.project);
        return result;
    }finally{busyProject=false}
}
async function uploadContent(path,file){
    const response=await fetch(path,{method:'POST',headers:{'X-Studio':studioToken,'X-Filename':encodeURIComponent(file.name),'Content-Type':'application/octet-stream'},body:file});
    const data=await response.json().catch(()=>({}));
    if(response.status===403)throw Error('Studio restarted — reload this page (F5) and try again.');
    if(!response.ok)throw Error(localize(data.error)||response.status);
    return data;
}
function sceneShots(id){return ep.shots.filter(s=>s.scene_id===id)}
function titleShot(s){return s.sourceHeading||s.title||('Shot '+(ep.shots.indexOf(s)+1))}
function scriptPanel(){
    const proposal=proposals.get(ep.id);
    const hasStory=Boolean((storyDrafts.get(ep.id)??ep.story??'').trim()||ep.scenes?.length||ep.shots?.length||ep.story_imports?.length||proposal);
    return '<section class="panel"><div class="top"><h2>Episode script</h2><button id="newEp">New episode</button></div>'+
        '<label>Episode title<input id="title" value="'+esc(ep.title)+'"></label>'+
        '<label>Story text<textarea rows="8" id="scriptDraft" dir="auto" placeholder="Paste a story or numbered scenes and shots.">'+esc(storyDrafts.get(ep.id)??ep.story??'')+'</textarea></label>'+
        '<div class="actions"><button class="primary" data-story-action="parse">Review breakdown</button><button data-story-action="file">Import story file</button>'+
        '<button type="button" class="danger" data-story-action="story-clear" '+(hasStory?'':'disabled')+'>Delete story</button></div>'+
        '<small>TXT, Markdown, Fountain, DOCX or text-based PDF</small></section>'+
        (proposal?proposalPanel(proposal):'')+
        (ep.story_imports?.length?'<details class="panel"><summary>Original story imports ('+ep.story_imports.length+')</summary>'+ep.story_imports.map((s,i)=>'<details><summary>'+esc(s.filename||s.name||('Import '+(i+1)))+'</summary><pre dir="auto">'+esc(s.original)+'</pre></details>').join('')+'</details>':'');
}
function proposalPanel(proposal){
    return '<section class="panel"><h2>Review proposed breakdown</h2><p>Adding this breakdown appends scenes to the current episode.</p>'+
        proposal.scenes.map(scene=>'<section class="proposal-scene" data-proposed-scene="'+esc(scene.id)+'">'+
            '<label>Proposed scene title<input dir="auto" data-proposal-field="title" value="'+esc(scene.title)+'"></label>'+
            scene.shots.map((s,i)=>'<details open data-proposed-shot="'+esc(s.id)+'"><summary>Shot '+(i+1)+'</summary>'+
                '<label>Shot heading<input dir="auto" data-proposal-field="sourceHeading" value="'+esc(s.sourceHeading)+'"></label>'+
                '<label>Narrative action and dialogue<textarea rows="4" dir="auto" data-proposal-field="action">'+esc(s.action)+'</textarea></label>'+
                '<button data-story-action="proposal-remove-shot">Remove proposed shot</button></details>').join('')+
            '<button data-story-action="proposal-add-shot">Add proposed shot</button></section>').join('')+
        '<div class="actions"><button class="primary" data-story-action="commit">Add reviewed scenes</button><button data-story-action="discard">Discard proposal</button></div></section>';
}
function sceneBrowser(){
    return (ep.scenes||[]).map(scene=>'<h3 dir="auto">'+esc(scene.title)+'</h3><div class="shot-list">'+sceneShots(scene.id).map(s=>'<button data-shot="'+esc(s.id)+'" class="'+(s===selectedShot()?'on':'')+'">'+esc(titleShot(s))+'</button>').join('')+'</div>').join('');
}
function scenePanels(board){
    const scenes=ep.scenes||[];
    return '<div class="toolbar"><button data-story-action="scene-new">Add scene</button><button data-story-action="approve-all">Approve all ready shots</button><button data-page="3">Open Production</button></div>'+
        (scenes.length?scenes.map(scene=>'<section class="panel"><div class="top"><h2 dir="auto">'+esc(scene.title)+'</h2><div class="row"><button data-story-action="scene-rename" data-scene="'+esc(scene.id)+'">Rename scene</button><button data-story-action="shot-new" data-scene="'+esc(scene.id)+'">Add shot</button></div></div>'+
            '<div class="'+(board?'board':'scene-list')+'">'+sceneShots(scene.id).map(s=>'<button class="scene-card" data-open-shot="'+esc(s.id)+'">'+(board&&s.image?'<img loading="lazy" alt="" src="'+esc(src(s.image))+'">':'')+'<strong dir="auto">'+esc(titleShot(s))+'</strong><span dir="auto">'+esc((s.action||s.prompt||'').slice(0,150))+'</span><small>'+esc(s.seconds??'—')+' s · '+(s.approved?'Approved':'In review')+'</small></button>').join('')+'</div></section>').join(''):'<section class="panel">Import a story or add your first scene.</section>');
}
function choices(items,selected){
    return items.map(([id,label])=>'<option value="'+esc(id)+'" '+(selected.includes(id)?'selected':'')+'>'+esc(label)+'</option>').join('');
}
function characterRegistry(){
    const items=db.characters||[],c=items.find(c=>c.id===selectedCharacter)||null;
    const refs=(list,voice)=>choices(assets.filter(f=>audio(f)===voice).map(f=>[f,name(f)]),list||[]);
    return '<section class="panel"><div class="top"><h2>Character registry</h2><button data-story-action="character-new">New character</button></div>'+
        '<label>Edit character<select id="characterChoice">'+option('','New character',c?.id||'')+items.map(x=>option(x.id,x.name,c?.id)).join('')+'</select></label>'+
        '<form id="characterForm"><div class="form-pair"><label>Display name<input name="name" required value="'+esc(c?.name)+'"></label><label>Aliases (one per line)<textarea name="aliases">'+esc((c?.aliases||[]).join('\n'))+'</textarea></label></div>'+
        '<label>Character description<textarea name="description" dir="auto">'+esc(c?.description)+'</textarea></label>'+
        '<div class="form-pair"><label>Image references<select multiple name="images">'+refs(c?.images,false)+'</select></label><label>Voice references<select multiple name="voices">'+refs(c?.voices,true)+'</select></label></div>'+
        '<button class="primary" type="submit">Save character</button></form></section>';
}
function castEditor(s,kind,label){
    const key=kind+'_ids',overrides=s.overrides||{},manual=Array.isArray(overrides[key]);
    return '<fieldset><legend>'+label+'</legend><label><input type="checkbox" data-cast-enable="'+kind+'" '+(manual?'checked':'')+'> Assign by character ID</label>'+
        '<select multiple data-cast-list="'+kind+'" '+(manual?'':'disabled')+'>'+choices((db.characters||[]).map(c=>[c.id,c.name]),(manual?overrides[key]:s[key])||[])+'</select></fieldset>';
}
function approvalIsCurrent(shot){
    return shot?.approved && shot.workflow_version===workflowCatalog.active;
}
function shotPanel(s){
    const p=preview?.episode===ep.id&&preview.shot===s.id?preview.data:null;
    const profile=currentProfile();
    const manual=manualReferences(s);
    const dialogue=(p?.dialogue||s.dialogue||[]).map((d,i)=>'<div class="dialogue-line"><strong dir="auto">'+esc(d.speaker||'Unattributed dialogue')+'</strong><span dir="auto" class="preserve-lines">'+esc(d.text)+'</span>'+
        '<label>Speaker assignment<select data-speaker-id="'+esc(d.speaker)+'">'+option('','Match by name',s.overrides?.speakers?.[d.speaker])+((db.characters||[]).map(c=>option(c.id,c.name,s.overrides?.speakers?.[d.speaker])).join(''))+'</select></label>'+
        '<label><input type="checkbox" data-offscreen="'+i+'" '+(d.offscreen?'checked':'')+'> Off-screen dialogue</label></div>').join('');
    return '<article data-shot-editor="'+esc(s.id)+'"><div class="top"><h2 dir="auto">'+esc(titleShot(s))+'</h2><span class="pill">'+(s.approved?'Approved':'In review')+'</span></div>'+
        '<form id="shotForm"><label>Complete Prompt<textarea dir="auto" rows="16" data-shot-field="prompt">'+esc(s.prompt??s.action??'')+'</textarea></label>'+
        '<details id="shotSettings"><summary>Advanced shot settings</summary>'+
        '<label>Duration (seconds)<input type="number" min="0.01" step="any" required data-shot-field="seconds" value="'+esc(s.seconds)+'"></label>'+
        (!/^\s*subject[ _]definitions\s*:/im.test(s.prompt||'')?
          '<div class="form-pair">'+castEditor(s,'visible','Visible characters')+castEditor(s,'speaking','Speaking characters')+'</div>':'')+
        (dialogue?'<h3>Dialogue attribution</h3>'+dialogue:'')+
        '<label>Additional images<select multiple id="shotImages">'+choices(images().map(f=>[f,name(f)]),manual.images)+'</select></label>'+
        '<label>Additional voices<select multiple id="shotAudio">'+choices(assets.filter(audio).map(f=>[f,name(f)]),manual.audio)+'</select></label>'+
        (profile?.fields?.some(f=>f.roles?.includes('parameter'))?'<label>Shot runtime parameters (JSON)<textarea id="shotParameters">'+esc(JSON.stringify(s.workflowParameters||{},null,2))+'</textarea></label>':'')+
        '<div id="assignmentIssues">'+ambiguityEditor(s,p)+'</div>'+
        '<label>Retake notes<textarea dir="auto" data-shot-field="change">'+esc(s.change)+'</textarea></label>'+
        '<button type="button" data-story-action="shot-remove">Remove draft shot</button></details>'+
        '<div class="actions"><button class="primary" type="submit">Save Prompt</button></div></form>'+
        '<section id="shotReferences"><h3>Character References</h3>'+referenceContents(p,s)+'</section>'+
        '<div class="actions"><button data-story-action="approve">'+(approvalIsCurrent(s)?'Revoke approval':s.approved?'Re-approve shot':'Approve shot')+'</button><button data-story-action="approve-all">Approve all ready shots</button><button data-page="3">Generate in Production</button></div>'+results(s)+'</article>';
}
function ambiguityEditor(s,p){
    const assigned=s.overrides?.aliases||{},issues=p?.ambiguity_choices||[],labels=new Set(Object.keys(assigned));
    for(const item of issues)if(item.kind==='name')labels.add(item.label);
    return [...labels].map(label=>'<label>Resolve “'+esc(label)+'”<select data-alias="'+esc(label)+'">'+option('','Match by name',assigned[label])+(db.characters||[]).map(c=>option(c.id,c.name,assigned[label])).join('')+'</select></label>').join('');
}
function manualReferences(s){
    if(s.story_managed)return {images:s.manual_images||[],audio:s.manual_audio||[]};
    const references=[s.image,...(s.refs||[])].filter(Boolean);
    return {images:references.filter(f=>!audio(f)),audio:references.filter(audio)};
}
function referenceContents(p,s){
    const files=p?[...p.images,...p.audio]:[s.image,...(s.refs||[])].filter(Boolean);
    const refs=files.map(f=>'<figure><figcaption>'+esc(f)+'</figcaption>'+
        (audio(f)?'<audio controls preload="none" src="'+src(f)+'"></audio>':'<img loading="lazy" alt="'+esc(f)+'" src="'+src(f)+'">')+'</figure>').join('');
    return (refs?'<div class="gallery shot-pictures">'+refs+'</div>':p?'<p>No character references found.</p>':'<p role="status">Matching assets…</p>')+
        (p?.blockers?.length?'<ul class="danger">'+p.blockers.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>':'')+
        (p?.split_proposal?.length?'<button data-story-action="split">Apply proposed split</button>':'');
}
function selectedValues(t){return Array.from(t.selectedOptions,o=>o.value)}
function shotChanges(s){
    const changes={};
    for(const t of document.querySelectorAll('[data-shot-field]')){
        const key=t.dataset.shotField,val=t.type==='number'?t.valueAsNumber:t.value;
        const original=s[key]??(key==='prompt'?s.action:'')??'';
        if(typeof val==='string'&&String(original).replace(/\r\n?/g,'\n')===val)continue;
        if(original!==val)changes[key]=val;
    }
    if('prompt' in changes&&s.action===s.prompt)changes.action=changes.prompt;
    const overrides=structuredClone(s.overrides||{});
    for(const t of document.querySelectorAll('[data-cast-enable]')){
        const kind=t.dataset.castEnable,key=kind+'_ids';
        if(t.checked)overrides[key]=selectedValues($('[data-cast-list="'+kind+'"]'));else delete overrides[key];
    }
    for(const t of document.querySelectorAll('[data-speaker-id],[data-alias]')){
        const type=t.hasAttribute('data-speaker-id')?'speakers':'aliases',k=type==='speakers'?t.dataset.speakerId:t.dataset.alias;
        if(t.value)(overrides[type]??={})[k]=t.value;else if(overrides[type])delete overrides[type][k];
    }
    if(JSON.stringify(overrides)!==JSON.stringify(s.overrides||{}))changes.overrides=overrides;
    const source=preview?.episode===ep.id&&preview.shot===s.id?preview.data.dialogue:s.dialogue;
    const lines=structuredClone(source||[]);
    let dialogueChanged=false;
    for(const t of document.querySelectorAll('[data-offscreen]')){
        const line=lines[+t.dataset.offscreen];
        if(line.offscreen!==t.checked){line.offscreen=t.checked;dialogueChanged=true}
    }
    if(dialogueChanged)changes.dialogue_override=lines.map(({speaker,text,offscreen})=>({speaker,text,offscreen}));
    const manual=manualReferences(s);
    for(const [key,el] of [['images','#shotImages'],['audio','#shotAudio']]){
        const values=selectedValues($(el));
        if(values.length!==manual[key].length||values.some(v=>!manual[key].includes(v)))changes['manual_'+key]=values;
    }
    if($('#shotParameters')){
        let v;try{v=JSON.parse($('#shotParameters').value||'{}')}catch{throw Error('Shot runtime parameters require valid JSON.')}
        if(!v||typeof v!=='object'||Array.isArray(v))throw Error('Shot runtime parameters must be a JSON object keyed by field ID.');
        if(JSON.stringify(v)!==JSON.stringify(s.workflowParameters||{}))changes.workflowParameters=v;
    }
    return changes;
}
async function saveShot(){
    const form=$('#shotForm');
    if(!form)return;
    if(!form.reportValidity())throw Error('Complete the highlighted shot fields.');
    const s=selectedShot(),changes=shotChanges(s);
    if(Object.keys(changes).length)await projectAction('/shots/update',{episode:ep.id,shot:s.id,changes});
}
function loadShotReferences(){
    const shot=selectedShot(),episodeId=ep.id,panel=$('#shotReferences');
    if(!panel||(preview?.episode===episodeId&&preview.shot===shot.id))return;
    post('/shots/preview',{episode:episodeId,shot:shot.id,graph:false}).then(data=>{
        if(!panel.isConnected)return;
        preview={episode:episodeId,shot:shot.id,data};
        panel.innerHTML='<h3>Character References</h3>'+referenceContents(data,shot);
        $('#assignmentIssues').innerHTML=ambiguityEditor(shot,data);
    }).catch(err=>{
        if(panel.isConnected)panel.innerHTML='<h3>Character References</h3><p class="danger">'+esc(err.message)+'</p>';
    });
}
async function approveShots(all){
    const revoke=!all&&approvalIsCurrent(selectedShot());
    await saveShot();
    const shot=selectedShot();
    if(revoke){
        await projectAction('/shots/update',{episode:ep.id,shot:shot.id,changes:{approved:false}});
        render();reportStatus('Approval revoked.');return;
    }
    const result=await projectAction('/shots/approve',{episode:ep.id,...(all?{}:{shot:shot.id})});
    render();
    const blocked=Object.entries(result.blocked||{}),approved=Array.isArray(result.approved)?result.approved.length:result.approved;
    if(blocked.length){
        $('#notice').textContent=(approved||0)+' approved. '+blocked.map(([id,why])=>(titleShot(ep.shots.find(s=>s.id===id)||{sourceHeading:id}))+': '+(Array.isArray(why)?why.join(' · '):why)).join(' / ');
    }else reportStatus((approved??1)+' shot(s) approved.');
}
function proposalScene(element){
    return proposals.get(ep.id)?.scenes.find(s=>s.id===element.closest('[data-proposed-scene]')?.dataset.proposedScene);
}
document.addEventListener('input',e=>{
    const t=e.target,field=t.dataset.proposalField;
    if(t.id==='scriptDraft'){
        storyDrafts.set(ep.id,t.value);
        const clearBtn=$('button[data-story-action="story-clear"]');
        if(clearBtn)clearBtn.disabled=!(t.value.trim()||ep.scenes?.length||ep.shots?.length||ep.story_imports?.length||proposals.get(ep.id));
    }
    if(!field)return;
    const scene=proposalScene(t);
    if(field==='title'){scene.title=t.value;return}
    const shot=scene.shots.find(s=>s.id===t.closest('[data-proposed-shot]').dataset.proposedShot);
    if(field==='action'&&shot.action!==t.value){
        // Re-estimate edited story text during commit and refresh its derived prompt.
        delete shot.seconds;
        delete shot.prompt;
    }
    shot[field]=t.value;
});
document.addEventListener('change',async e=>{
    const t=e.target;
    if(t.dataset.castEnable){$('[data-cast-list="'+t.dataset.castEnable+'"]').disabled=!t.checked;return}
    if(t.id==='characterChoice'){selectedCharacter=t.value||null;render();return}
    if(t.id!=='storyFile'||!t.files.length)return;
    const file=t.files[0],episodeId=ep.id,quick=page===1;t.value='';
    try{
        const proposal=await uploadContent('/story/parse',file);
        if(quick){
            const current=db.episodes.find(e=>e.id===episodeId);
            const title=file.name.replace(/\.[^.]+$/,'').replaceAll('_',' ');
            const result=await projectAction('/story/commit',{episode:episodeId,proposal,
                ...(current?.shots.length?{newEpisode:title}:{})});
            ep=db.episodes.find(e=>e.id===result.episode);
            selectedId=ep.shots[0]?.id||'';
            page=1;render();reportStatus(ep.shots.length+' shots imported.');return;
        }
        proposals.set(episodeId,proposal);
        if(ep.id===episodeId){render();reportStatus('Review the proposed scenes before adding them.')}
    }catch(err){reportError(err)}
});
document.addEventListener('submit',async e=>{
    const form=e.target;
    if(!['characterForm','shotForm'].includes(form.id))return;
    e.preventDefault();
    const button=form.querySelector('[type=submit]');button.disabled=true;
    try{
        if(form.id==='shotForm'){await saveShot();render();reportStatus('Prompt saved.');return}
        const f=form.elements,id=selectedCharacter||uid();
        await projectAction('/characters/save',{character:{id,name:f.namedItem('name').value.trim(),aliases:f.aliases.value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean),description:f.description.value,images:selectedValues(f.images),voices:selectedValues(f.voices)}});
        selectedCharacter=id;render();reportStatus('Character saved.');
    }catch(err){reportError(err)}finally{if(button.isConnected)button.disabled=false}
});
document.addEventListener('click',async e=>{
    const b=e.target.closest('button[data-story-action],button[data-review-retake]');if(!b)return;
    const action=b.dataset.storyAction,episodeId=ep?.id;
    if(b.dataset.reviewRetake){selectedId=b.dataset.reviewRetake;page=1;render();$('#shotSettings').open=true;$('[data-shot-field="change"]').focus();return}
    b.disabled=true;
    try{
        switch(action){
        case 'migrate':{
            const r=await post('/migrate',{token:migration.token});
            adoptProject(r.project);migration=null;await loadWorkflows();render();reportStatus('Backup · '+(r.backup||'Created'));break;
        }
        case 'file':$('#storyFile').click();break;
        case 'parse':{
            const text=$('#scriptDraft').value;if(!text.trim())throw Error('Enter or import a story first.');
            proposals.set(episodeId,await post('/story/parse',{text}));
            render();reportStatus('Review the proposed scenes before adding them.');break;
        }
        case 'commit':{
            const proposal=proposals.get(episodeId);
            await projectAction('/story/commit',{episode:episodeId,proposal});
            proposals.delete(episodeId);storyDrafts.delete(episodeId);storyTab='Scenes';render();reportStatus('Reviewed scenes added.');break;
        }
        case 'proposal-add-shot':{
            const scene=proposalScene(b);
            const id=uid();
            scene.shots.push({id,scene_id:scene.id,sourceHeading:'',action:''});
            render();$('[data-proposed-shot="'+id+'"] textarea').focus();break;
        }
        case 'proposal-remove-shot':{
            const scene=proposalScene(b),id=b.closest('[data-proposed-shot]').dataset.proposedShot;
            scene.shots=scene.shots.filter(s=>s.id!==id);render();break;
        }
        case 'discard':proposals.delete(episodeId);render();break;
        case 'story-clear':{
            if(!confirm('Delete the story text, scenes, and shots for this episode? Saved tokens, keys, workflow, and assets will be kept.'))break;
            await projectAction('/story/clear',{episode:episodeId});
            storyDrafts.delete(episodeId);proposals.delete(episodeId);selectedId='';preview=null;
            render();rememberWorkspace();reportStatus('Story deleted.');break;
        }
        case 'scene-new':case 'scene-rename':{
            const existing=ep.scenes.find(s=>s.id===b.dataset.scene),title=prompt('Scene title',existing?.title||'');
            if(!title?.trim())break;
            await projectAction('/project/scene',{episode:episodeId,id:existing?.id,title:title.trim()});render();break;
        }
        case 'shot-new':{
            const text=prompt('Narrative action for this shot');if(!text?.trim())break;
            await projectAction('/shots/new',{episode:episodeId,scene:b.dataset.scene,text:text.trim()});
            selectedId=sceneShots(b.dataset.scene).at(-1)?.id||'';page=1;render();break;
        }
        case 'shot-remove':
            if(confirm('Remove this draft shot?')){await projectAction('/shots/remove',{episode:episodeId,shot:selectedShot().id});selectedId='';render()}break;
        case 'character-new':selectedCharacter=null;render();$('#characterForm [name="name"]').focus();break;
        case 'approve-all':case 'approve':await approveShots(action==='approve-all');break;
        case 'split':{
            const s=selectedShot();
            if(Object.keys(shotChanges(s)).length)throw Error('Save and preview your changes before applying the split.');
            if(!confirm('Apply the proposed split and review each resulting shot?'))break;
            await projectAction('/shots/split',{episode:episodeId,shot:s.id});selectedId=s.id;render();reportStatus('Split shots need review.');break;
        }
        }
    }catch(err){reportError(err)}finally{if(b.isConnected)b.disabled=false}
});
