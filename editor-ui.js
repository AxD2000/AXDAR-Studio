let editorState=null,editorLoading='',editorClip='',editorMonitor='',editorBusy=false;
const editorActions=()=>editorState?.episode===ep?.id?editorState:null;

function editButton(action,label,disabled=false){
    return '<button type="button" data-edit-action="'+action+'" '+(disabled?'disabled':'')+'>'+label+'</button>';
}
function editTime(seconds){
    const n=Math.max(0,seconds||0), minutes=Math.floor(n/60);
    return minutes+':'+(n%60).toFixed(2).padStart(5,'0');
}
function formNumber(name,label,value,min,max,step){
    return '<label>'+label+'<input name="'+name+'" type="number" min="'+min+'" max="'+max+'" step="'+step+'" value="'+esc(value)+'" required></label>';
}
function editorShot(d,id){return d.shots.find(s=>s.id===id)}
function editorScene(d,id){return d.scenes.find(s=>s.id===id)?.title||''}

async function getEdit(episode=ep.id){
    const result=await post('/editor',{action:'get',episode});
    if(ep?.id===episode){
        editorState=result;
        if(page===4)render();
    }
}
function renderEditor(){
    const d=editorActions();
    if(!d){
        if(editorLoading!==ep.id){
            const current=ep.id;
            editorLoading=current;
            getEdit(current).catch(error=>{
                if(page===4&&ep.id===current)$('#app').innerHTML='<section class="panel"><p class="danger">'+esc(error.message)+'</p>'+editButton('refresh','Reload editor')+'</section>';
            }).finally(()=>{if(editorLoading===current)editorLoading='' });
        }
        return '<section class="panel"><p role="status">Loading the episode’s Takes…</p></section>';
    }
    const clip=d.clips.find(c=>c.id===editorClip)||d.clips[0];
    editorClip=clip?.id||'';
    const selected=clip&&editorShot(d,clip.shot);
    const take=selected?.takes.find(t=>t.version===clip.version);
    const final=d.finals.find(job=>job.id===editorMonitor);
    const source=editorMonitor==='source'&&take?.video;
    const program=source?take.video:final?.video||d.draft?.video||take?.video;
    const track=!source&&(final?.track||(!final&&d.draft?.track));
    const rendering=currentEditorJob(d);
    let previousScene;
    const bin=d.shots.map(s=>{
        const scene=s.scene!==previousScene?'<h3 dir="auto">'+esc(editorScene(d,s.scene))+'</h3>':'';
        previousScene=s.scene;
        return scene+'<details '+(selected===s?'open':'')+'><summary dir="auto">'+esc(s.title)+' · '+s.takes.filter(t=>t.ready).length+' available</summary>'+
            (s.takes.length?s.takes.map(t=>'<div class="row"><span>'+esc(t.version)+' · '+(t.ready?editTime(t.media.duration):esc(t.issue))+'</span>'+
                (t.ready?'<button type="button" data-edit-action="add" data-bin-shot="'+esc(s.id)+'" data-version="'+esc(t.version)+'">Add take to timeline</button>'+
                    '<button type="button" data-edit-action="reject" data-bin-shot="'+esc(s.id)+'" data-version="'+esc(t.version)+'">Reject for this edit</button>':
                    t.editorRejected?'<button type="button" data-edit-action="restore" data-bin-shot="'+esc(s.id)+'" data-version="'+esc(t.version)+'">Restore Take</button>':'')+'</div>').join(''):
                '<p>No completed Take saved locally for this shot.</p>')+
            '<button type="button" data-open-shot="'+esc(s.id)+'">Open Shot Lab</button></details>';
    }).join('');
    return '<div class="toolbar">'+editButton('refresh','Refresh Takes')+editButton('assemble','Assemble first cut',!!rendering)+
        editButton('available','Assemble available footage',!!rendering)+
        editButton('draft',d.reviewCut?'Generate Draft Review Cut':'Generate Draft Episode',!!rendering||!d.clips.length)+
        editButton('approve',d.reviewCut?'Approve Review Cut':'Approve Final Episode',!!rendering||!d.currentDraft||d.approved)+
        (d.approved?'<strong class="status">'+(d.reviewCut?'Review cut approved':'Final episode approved')+'</strong>':'')+'</div>'+
        '<div class="lab"><section class="panel"><h2>Media bin</h2><div class="editor-bin">'+(bin||'<p>Approve shots and load completed Takes from Production.</p>')+'</div>'+
        (d.archives.length?'<details><summary>Recover archived Takes</summary>'+d.archives.map(a=>'<p dir="auto">'+esc(a.title)+' · '+a.takes+' completed Takes</p>'+
            '<button type="button" data-edit-action="recover" data-archive-episode="'+esc(a.episode)+'">Recover archived episode</button>').join('')+'</details>':'')+'</section>'+
        '<section class="panel"><div class="top"><h2>Program monitor</h2>'+editButton('source','View source',!take?.video)+editButton('monitor','View draft',!d.draft?.video)+'</div>'+
        (program?'<video id="editorMonitor" controls preload="metadata" src="'+esc(program)+'">'+(track?'<track kind="subtitles" label="Original script" src="'+esc(track)+'" default>':'')+'</video>':'<div class="monitor">Generate a draft or choose a completed Take.</div>')+
        '<p id="editorPlaybackLabel" role="status">'+esc(source?'Source • '+selected.title:final?'Final export':d.draft?.video?(d.currentDraft?'Current draft':'Previous draft · regenerate after edits'):'Source Take')+'</p>'+
        (d.draft?.srt?'<a href="'+esc(d.draft.srt)+'" download>Download draft SRT</a>':'')+
        '<div id="editorProgress">'+(rendering?renderProgress(rendering):'')+'</div>'+
        (d.draft?.status==='error'?'<p class="danger">'+esc(d.draft.message)+'</p>':'')+'</section></div>'+
        '<section class="panel"><h2>Timeline</h2><p>'+editTime(d.duration)+' · '+d.clips.length+' clips</p><div class="timeline">'+
        d.clips.map((c,i)=>'<button data-edit-action="select" type="button" data-clip="'+esc(c.id)+'" class="'+(c===clip?'on':'')+'">'+
            esc(editorScene(d,editorShot(d,c.shot)?.scene))+'<br>'+esc(editorShot(d,c.shot)?.title||'Missing shot')+
            '<small>Clip '+(i+1)+' · '+editTime(c.end-c.start)+'</small></button>').join('')+'</div>'+
        (clip?clipEditor(d,clip,selected,take):'')+'</section>'+
        editorContinuity(d)+editorSettings(d)+exportEditor(d,!!rendering);
}
function clipEditor(d,clip,shot,take){
    const ready=shot?.takes.filter(t=>t.ready)||[];
    const i=d.clips.indexOf(clip);
    const duration=take?.media.duration||clip.end;
    const settings='<form id="clipEdit"><div class="form-pair">'+
        formNumber('start','Trim start (source seconds)',clip.start,0,duration,'any')+
        formNumber('end','Trim end (source seconds)',clip.end,0,duration+.001,'any')+
        formNumber('fade','Fade through black to next clip (seconds)',clip.fade,0,2,.01)+
        '</div><button type="submit">Save trim and transition</button></form>';
    const manual=(d.subtitleSources[clip.shot+':'+clip.version]||[]).map((s,n)=>
        '<form class="subtitleEdit" data-line="'+n+'"><p dir="auto"><strong>'+esc(s.speaker||'Dialogue')+'</strong> · '+esc(s.method)+'</p>'+
        '<blockquote dir="auto">'+esc(s.text)+'</blockquote><div class="form-pair">'+
        formNumber('start','Cue start in source seconds',s.start.toFixed(3),0,duration+.001,.001)+
        formNumber('end','Cue end in source seconds',s.end.toFixed(3),0,duration+.001,.001)+
        '</div><button type="submit">Save cue timing</button></form>').join('');
    return '<div class="clip-tools"><div class="row">'+editButton('seek','Play from this clip',!d.currentDraft||d.offsets[clip.id]===undefined)+editButton('up','Move earlier',i===0)+
        editButton('down','Move later',i===d.clips.length-1)+editButton('remove','Remove from timeline')+
        editButton('flag',d.flags[clip.shot]!=null?'Clear regeneration flag':'Flag for regeneration')+
        '<button type="button" data-open-shot="'+esc(clip.shot)+'">Open Shot Lab</button></div>'+
        (ready.length?'<form id="takeReplace"><div class="row"><label>Take<select name="version">'+ready.map(t=>option(t.version,t.version+' · '+editTime(t.media.duration),clip.version)).join('')+
        '</select></label><button type="submit">Use selected Take</button></div></form>':'')+
        settings+'<form id="clipSplit"><div class="row">'+formNumber('at','Split at source seconds',((clip.start+clip.end)/2).toFixed(3),clip.start,clip.end,.001)+
        '<button type="submit">Split clip</button></div></form>'+
        '<details><summary>Subtitle cues ('+(d.subtitleSources[clip.shot+':'+clip.version]||[]).length+')</summary>'+
        (manual||'<p>No original dialogue is available for this shot.</p>')+'</details></div>';
}
function editorContinuity(d){
    const blocked=d.issues.filter(x=>x.blocking);
    const rows=d.issues.slice(0,100).map(x=>{
        const shot=editorShot(d,x.shot);
        return '<li class="'+(x.blocking?'danger':'')+'">'+(shot?'<strong>'+esc(shot.title)+'</strong> · ':'')+esc(x.message)+
            (x.blocking&&shot?' <button type="button" data-open-shot="'+esc(shot.id)+'">Open Shot Lab</button>':'')+'</li>';
    }).join('');
    return '<details class="panel" '+(blocked.length?'open':'')+'><summary>Continuity review · '+blocked.length+' blocking · '+(d.issues.length-blocked.length)+' notes</summary>'+
        (rows?'<ul>'+rows+'</ul>':'<p>No issues found for the current timeline.</p>')+(d.issues.length>100?'<p>Showing the first 100 issues.</p>':'')+'</details>';
}
function outputFields(prefix,opts){
    const horizontal=opts.width>opts.height;
    return '<label>Aspect ratio<select data-edit-orientation="'+prefix+'">'+option('16:9','16:9 horizontal',horizontal?'16:9':'9:16')+
        option('9:16','9:16 vertical',horizontal?'16:9':'9:16')+'</select></label>'+
        formNumber('width','Width',opts.width,144,3840,2)+formNumber('height','Height',opts.height,144,3840,2)+
        formNumber('fps','Frames per second',opts.fps,12,60,1);
}
function editorSettings(d){
    const o=d.options;
    return '<details class="panel"><summary>Draft framing, audio and subtitles</summary><form id="draftOptions"><div class="form-pair">'+outputFields('draft',o)+
        '<label>Aspect fit<select name="fit">'+option('pad','Pad to keep full source',o.fit)+option('crop','Crop to fill frame',o.fit)+'</select></label>'+
        formNumber('cropX','Horizontal crop position (0–1)',o.cropX,0,1,.01)+formNumber('cropY','Vertical crop position (0–1)',o.cropY,0,1,.01)+'</div>'+
        '<label><input name="normalize" type="checkbox" '+(o.normalize?'checked':'')+'> Normalize episode audio</label>'+
        '<label><input name="burn" type="checkbox" '+(o.burn?'checked':'')+'> Burn subtitles into the picture</label>'+
        '<div class="form-pair"><label>Font family<input name="font" maxlength="60" value="'+esc(o.font)+'" required></label>'+
        formNumber('fontSize','Font size',o.fontSize,10,100,1)+
        '<label>Placement<select name="placement">'+option('bottom','Bottom',o.placement)+option('top','Top',o.placement)+'</select></label>'+
        '<label>Text color<input name="color" type="color" value="'+esc(o.color)+'"></label>'+
        formNumber('outline','Outline',o.outline,0,8,1)+formNumber('margin','Vertical margin',o.margin,0,100,1)+'</div>'+
        '<button type="submit">Save draft settings</button></form></details>';
}
function exportEditor(d,busy){
    return '<section class="panel"><h2>Final exports</h2>'+
        '<form id="finalExport"><div class="form-pair">'+outputFields('export',d.options)+'</div>'+
        '<button type="submit" '+(!d.approved||busy?'disabled':'')+'>Export approved MP4</button></form>'+
        (!d.approved?'<p>Approve the current verified draft to unlock export.</p>':'')+
        (d.finals.map(j=>'<div class="row"><strong>'+j.options.width+'×'+j.options.height+' · '+j.options.fps+' fps</strong>'+
            (j.status==='done'?'<span>'+editTime(j.media.duration)+'</span><button type="button" data-edit-action="view-final" data-job="'+esc(j.id)+'">Preview final</button>'+
                '<a href="'+esc(j.download)+'" download>Download MP4</a> <a href="'+esc(j.srt)+'" download>Download SRT</a>':
                '<span class="'+(j.status==='error'?'danger':'')+'">'+esc(j.message)+'</span>')+(j.reviewCut?' <small>Review cut</small>':'')+'</div>').join(''))+'</section>';
}
function currentEditorJob(d){
    return [d.draft,...d.finals].find(j=>j?.status==='rendering');
}
function renderProgress(job){
    return '<progress aria-label="Episode render progress" max="100" value="'+job.progress+'"></progress> <span role="status">'+esc(job.message)+' · '+job.progress+'%</span>';
}
async function editorPost(action,extra={}){
    const requestEpisode=ep.id;
    await flushEdits();
    const result=await post('/editor',{action,episode:requestEpisode,rev:editorActions()?.rev,...extra});
    if(action==='recover'){
        adoptProject(result.project);
        ep=db.episodes.find(item=>item.id===result.episode);
        editorState=null;editorMonitor='';editorClip='';
        render();
        return;
    }
    if(ep?.id===requestEpisode){
        editorState=result;
        if(action==='draft'||action==='export')editorMonitor='';
        if(page===4)render();
    }
}
function seekEditClip(d,id){
    const target=d.clips.findIndex(c=>c.id===id);
    if(target<0||!d.currentDraft)return;
    const seek=d.offsets[id];
    if(seek===undefined)return;
    editorMonitor='';
    render();
    const video=$('#editorMonitor');
    if(video){
        const play=()=>{video.currentTime=seek;video.play().catch(()=>{})};
        if(video.readyState>=1)play();else video.addEventListener('loadedmetadata',play,{once:true});
    }
}
document.addEventListener('click',async event=>{
    const button=event.target.closest('button[data-edit-action]');
    if(!button||editorBusy)return;
    const action=button.dataset.editAction, d=editorActions(), clip=d?.clips.find(c=>c.id===editorClip);
    if(!d&&action!=='refresh')return;
    button.disabled=true;
    editorBusy=true;
    try{
        if(action==='refresh'){await getEdit();return}
        if(action==='select'){editorClip=button.dataset.clip;editorMonitor='';render();return}
        if(action==='source'||action==='monitor'||action==='view-final'){
            editorMonitor=action==='source'?'source':button.dataset.job||'';render();return;
        }
        if(action==='seek'){seekEditClip(d,editorClip);return}
        if(['assemble','available'].includes(action)&&d.clips.length&&!confirm('Replace the edit decisions with a new first cut? Saved media and exports are kept.'))return;
        if(action==='available'){await editorPost('assemble',{available:true});return}
        if(action==='recover'){
            if(confirm('Recover these archives into the episode list using their original shot IDs?'))
                await editorPost(action,{episode:button.dataset.archiveEpisode});
            return;
        }
        if(['add','reject','restore'].includes(action)){
            await editorPost(action,{shot:button.dataset.binShot,version:button.dataset.version});return;
        }
        if(action==='flag'){
            const clear=d.flags[clip.shot]!=null;
            const note=clear?'':prompt('Regeneration note for this Shot');
            if(note!==null)await editorPost(action,{shot:clip.shot,note,clear});
            return;
        }
        if(action==='approve'&&!confirm('Approve this exact draft for final export?'))return;
        if(action==='up'||action==='down'){await editorPost('move',{clip:editorClip,step:action==='up'?-1:1});return}
        await editorPost(action,{clip:editorClip});
    }catch(error){reportError(error)}finally{editorBusy=false;if(button.isConnected)button.disabled=false}
});
document.addEventListener('submit',async event=>{
    const form=event.target;
    const types={clipEdit:'trim',clipSplit:'split',takeReplace:'replace',draftOptions:'options',finalExport:'export'};
    const action=types[form.id]||(form.classList.contains('subtitleEdit')?'subtitle':null);
    if(!action)return;
    event.preventDefault();
    if(editorBusy||!form.reportValidity())return;
    editorBusy=true;
    const f=Object.fromEntries(new FormData(form));
    let body={...f,clip:editorClip};
    if(action==='options')body={options:{...f,burn:form.elements.burn.checked,normalize:form.elements.normalize.checked}};
    if(action==='export')body={output:{width:f.width,height:f.height,fps:f.fps}};
    if(action==='subtitle')body.line=Number(form.dataset.line);
    try{await editorPost(action,body)}catch(error){reportError(error)}finally{editorBusy=false}
});
document.addEventListener('change',event=>{
    const control=event.target;
    if(!control.dataset.editOrientation)return;
    const f=control.form.elements, flip=(Number(f.width.value)>Number(f.height.value))!==(control.value==='16:9');
    if(flip){[f.width.value,f.height.value]=[f.height.value,f.width.value]}
});
setInterval(async()=>{
    const d=editorActions(), job=d&&currentEditorJob(d);
    if(!job||editorBusy||page!==4)return;
    try{
        const updated=await post('/editor',{action:'poll',episode:d.episode,job:job.id});
        if(editorState!==d||page!==4)return;
        if(updated.status==='rendering'){
            const panel=$('#editorProgress');
            if(panel)panel.innerHTML=renderProgress(updated);
        }else await getEdit(d.episode);
    }catch(error){reportError(error)}
},1250);
