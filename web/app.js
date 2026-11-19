'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="workspace-token"]').content;
const icon = name => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let state = null, selected = new Set(), focus = null, pendingLook = 'original', pendingStrength = 100;
let pendingEnhance=false,pendingEnhanceStrength=100,enhanceTimer,pendingEnhanceEdit=null;
const PAGE_SIZE=40;let mediaPage=0,queuePage=0,orderedJobs=[];
let mediaSignature = '', lookSignature = '', jobsSignature = '', previewSignature = '';
let compare = true, currentSecond = 0, previewGeneration = 0, imagesReady = false, initialized = false;
let selectionRevision = 0, importing = false, polling = false, connected = false, toastTimer, seekTimer, strengthTimer;
let applySerial = Promise.resolve(), editVersion = 0;
let outputPicking=false, exportPending=false, queueControlBusy=false;
const optimistic = new Map(), expandedJobs = new Set();
const browserOutput=new window.BrowserOutput(()=>{if(state)render();},async(job,name,bytes)=>{
  const result=await api('/api/browser-saved',{id:job.id,target:job.browser_target,name,bytes});
  const current=state?.jobs.find(item=>item.id===job.id);if(current){current.browser_saved=true;current.cache_removed=result.cache_removed;}
});
const prefs = (() => { try { return JSON.parse(localStorage.getItem('color-studio-prefs') || '{}'); } catch { return {}; } })();
const time = (seconds, decimal = false) => {
  seconds = Math.max(0, Number(seconds) || 0);
  const value = `${String(Math.floor(seconds / 60)).padStart(2,'0')}:${String(Math.floor(seconds % 60)).padStart(2,'0')}`;
  return decimal ? value + '.' + Math.floor(seconds % 1 * 10) : value;
};
const size = bytes => bytes >= 1073741824 ? (bytes / 1073741824).toFixed(1) + ' GB' : Math.max(.1, bytes / 1048576).toFixed(1) + ' MB';
const estimate = seconds => seconds == null || !Number.isFinite(seconds) ? '正在估算' : seconds < 60 ? `约 ${Math.max(1,Math.ceil(seconds))} 秒` : `约 ${Math.ceil(seconds/60)} 分钟`;
const currentMedia = () => state?.media.find(m => m.id === focus);
const currentLook = () => state?.looks.find(l => l.id === (currentMedia()?.filter || pendingLook)) || state?.base_look || state?.looks[0];
const settings = () => ({profile:$('profile').value,encoder:$('encoder').value,quality:Number($('quality').value),rate_mode:$('rate-mode').value,fps:$('fps').value,range:$('input-range').value,audio_aac:$('audio-aac').checked,preview:0});

function toast(message, error = false) {
  clearTimeout(toastTimer); $('toast').textContent = message; $('toast').classList.toggle('error',error); $('toast').hidden = false;
  toastTimer = setTimeout(() => $('toast').hidden = true, error ? 6500 : 3500);
}
async function api(route, body = {}) {
  const response = await fetch(route,{method:'POST',headers:{'Content-Type':'application/json','X-Workspace-Token':token},body:JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '操作未完成');
  return result;
}
async function act(callback) { try { return await callback(); } catch(error) { toast(error.message,true); } }
function previewUrl(media, look = media.filter, strength = media.strength, before = false, width = 1280, second = currentSecond) {
  return '/api/preview?' + new URLSearchParams({id:media.id,filter:look,strength,enhance:media.enhance_enabled?'1':'0',enhance_strength:media.enhance_strength??100,look_rev:state?.looks.find(item=>item.id===look)?.revision||0,profile:$('profile').value,range:$('input-range').value,before:before?'1':'0',width,time:second.toFixed(2)});
}
function connection(ok, text) {
  $('connection').className = 'connection' + (!ok ? ' offline' : state?.ready === 'loading' ? ' loading' : '');
  $('connection').lastChild.textContent = text;
  connected = ok;
}
async function refresh(force=false) {
  if (polling) {if(force){await new Promise(resolve=>setTimeout(resolve,50));return refresh(true);}return;}
  polling = true;
  try {
    const params=new URLSearchParams({compact:'1'});if(state?.instance){params.set('instance',state.instance);params.set('since',state.media_revision);}
    const response = await fetch('/api/state?'+params,{cache:'no-store'});
    if (!response.ok) throw new Error('无法连接本地服务');
    const incoming=await response.json();
    if(incoming.media_unchanged&&state)incoming.media=state.media;
    state=incoming;
    for (const media of state.media) if (optimistic.has(media.id)) Object.assign(media,optimistic.get(media.id).values);
    if (!initialized) {
      const initial = {...state.settings,...prefs};
      const versionParts=state.version.split('.').map(Number);
      if((versionParts[0]>1||(versionParts[0]===1&&versionParts[1]>=5))&&!localStorage.getItem('action4-performance-v1.5')){
        initial.encoder='auto';initial.rate_mode='source';Object.assign(prefs,{encoder:'auto',rate_mode:'source'});
        localStorage.setItem('color-studio-prefs',JSON.stringify(prefs));localStorage.setItem('action4-performance-v1.5','1');
      }
      for (const [id,key] of [['profile','profile'],['encoder','encoder'],['quality','quality'],['rate-mode','rate_mode'],['fps','fps'],['input-range','range']]) {
        if ([...$(id).options].some(x=>x.value===String(initial[key]))) $(id).value = initial[key];
      }
      $('audio-aac').checked = Boolean(initial.audio_aac);
      selected = new Set(state.media.map(m=>m.id)); focus = state.media[0]?.id || null;
      initialized = true;
    }
    selected = new Set([...selected].filter(id=>state.media.some(m=>m.id===id)));
    if (!state.media.some(m=>m.id===focus)) focus = state.media[0]?.id || null;
    connection(true,state.ready==='ready'?'本地运行':state.ready==='error'?'引擎需要检查':'引擎准备中');
    $('engine-banner').hidden = state.ready === 'ready';
    $('engine-banner').classList.toggle('error',state.ready==='error');
    if (state.ready === 'loading') $('engine-banner').textContent = '正在准备本地处理引擎，首次启动可能需要片刻…';
    if (state.ready === 'error') $('engine-banner').innerHTML = `${esc(state.setup_error)} <button id="retry-setup">重试</button>`;
    render();
    browserOutput.flush(state.jobs).catch(()=>{});
  } catch(error) {
    connection(false,'服务未连接');
    $('start-export').disabled = $('preview-export').disabled = true;
  } finally { polling = false; }
}

function render() { renderEncoder(); renderMedia(); renderLooks(); renderEnhancement(); renderFocus(); renderJobs(); renderSummary(); }
function renderEncoder() {
  const capability=state.nvenc;
  const unavailable=capability?.available===false;
  const option=$('encoder').querySelector('option[value="nvenc"]');
  option.disabled=unavailable;
  option.textContent=unavailable?'NVIDIA H.265 · 当前不可用':'NVIDIA H.265 · 10-bit · 加速';
  if(unavailable && $('encoder').value==='nvenc') {
    $('encoder').value='hevc';prefs.encoder='hevc';localStorage.setItem('color-studio-prefs',JSON.stringify(prefs));
  }
  const relevant=unavailable && (state.settings.encoder==='nvenc'||state.jobs.some(j=>j.notice));
  $('encoder-warning').hidden=!relevant;
  $('encoder-warning').textContent=relevant?capability.reason+'，自动使用 CPU 10-bit 编码。':'';
}
function renderMedia() {
  const signature = JSON.stringify([state.media.slice(mediaPage*PAGE_SIZE,(mediaPage+1)*PAGE_SIZE).map(m=>[m.id,m.status,m.filter,m.strength,m.enhance_enabled,m.enhance_strength,m.duration,m.error]),state.media.length,mediaPage,[...selected],focus,selectionRevision]);
  $('media-count').textContent = state.media.length;
  const all = $('select-all'); all.checked = state.media.length > 0 && selected.size === state.media.length; all.indeterminate = selected.size > 0 && selected.size < state.media.length;
  $('remove-media').disabled = !selected.size;
  if (signature === mediaSignature) return;
  mediaSignature = signature;
  if (!state.media.length) {
    $('media-pages').hidden=true;
    $('media-list').innerHTML = `<div class="library-empty">${icon('film')}<p>还没有素材</p><span>支持 MP4 / MOV / M4V<br>可批量选择，也可拖入</span></div>`;
    return;
  }
  const totalPages=Math.ceil(state.media.length/PAGE_SIZE);mediaPage=Math.min(mediaPage,totalPages-1);
  $('media-pages').hidden=totalPages<=1;$('media-prev').disabled=mediaPage===0;$('media-next').disabled=mediaPage>=totalPages-1;
  $('media-page-label').textContent=`${mediaPage+1} / ${totalPages} · 共 ${state.media.length} 段`;
  const visibleMedia=state.media.slice(mediaPage*PAGE_SIZE,(mediaPage+1)*PAGE_SIZE);
  const scroll = $('media-list').scrollTop;
  $('media-list').innerHTML = visibleMedia.map(media=>{
    const look = state.looks.find(l=>l.id===media.filter);
    const checked = selected.has(media.id), ready = media.status==='ready';
    return `<article class="media-row ${checked?'selected':''} ${focus===media.id?'focused':''} ${['error','missing'].includes(media.status)?'error':''}" data-media="${media.id}" tabindex="0" aria-label="预览 ${esc(media.name)}" title="${esc(media.path)}">
      <label class="media-check"><input type="checkbox" data-select="${media.id}" aria-label="选择 ${esc(media.name)}" ${checked?'checked':''}></label>
      <div class="media-thumb">${ready?`<img src="${esc(previewUrl(media,'original',100,true,320,0))}" alt="" loading="lazy"><span class="thumb-time">${time(media.duration)}</span>`:media.status==='checking'?'<span class="spinner"></span>':icon('film')}</div>
      <div class="media-info"><strong>${esc(media.name)}</strong><span>${ready?`${media.width} × ${media.height} · ${size(media.bytes)}`:media.status==='checking'?'正在读取素材':esc(media.error||'文件不可用')}</span><span class="media-look">${ready?esc(look?.name||'官方还原')+(media.enhance_enabled&&media.filter!=='dji_reference'?' · 增强':''):''}</span></div></article>`;
  }).join('');
  $('media-list').scrollTop = scroll;
}
function renderLooks() {
  const media = currentMedia(), look = currentLook();
  const signature = JSON.stringify([focus,media?.status,media?.filter,media?.strength,media?.enhance_enabled,media?.enhance_strength,pendingLook,$('profile').value,$('input-range').value,state.looks.map(l=>[l.id,l.revision,l.name,l.description,l.default_strength])]);
  $('apply-count').textContent = selected.size ? `应用到 ${selected.size} 段` : '未选素材';
  if (signature === lookSignature) return;
  lookSignature = signature;
  $('restore-looks').hidden=!state.removed_looks?.length;
  if(!state.looks.length){$('looks-grid').innerHTML='<div class="looks-empty">还没有滤镜，点击上方“导入 LUT”添加。</div>';}
  else $('looks-grid').innerHTML = state.looks.map(item=>{
    const name = item.id==='original' && $('profile').value==='rec709' ? '原色保留' : item.name;
    return `<div class="look-card"><button class="look-option ${look?.id===item.id?'active':''}" data-id="${item.id}" aria-pressed="${look?.id===item.id}" title="${esc(item.description)}"><span class="look-image">${media?.status==='ready'?`<img src="${esc(previewUrl(media,item.id,100,false,320,0))}" alt="" loading="lazy">`:'<span class="look-placeholder"><i></i><i></i><i></i></span>'}<span class="look-tag">${esc(item.tag)}</span></span><span class="look-caption"><span class="look-name">${esc(name)}</span>${look?.id===item.id?icon('check'):''}</span></button><button type="button" class="look-remove" data-remove-look="${item.id}" aria-label="移除 ${esc(name)}" title="从列表移除">${icon('close')}</button></div>`;
  }).join('');
  $('look-description').textContent = look?.id==='original' && $('profile').value==='rec709' ? '保留原有色彩，不应用技术还原' : look?.description || '';
  const disabled = !look || look.id==='original' || look.stage==='technical';
  $('strength').disabled = disabled;
  $('strength').closest('section').classList.toggle('inactive',disabled);
  const amount = media?.strength ?? 100;
  if (document.activeElement !== $('strength')) $('strength').value = amount;
  $('reset-strength').textContent = `${amount}%`;
  $('strength-note').textContent = disabled ? '技术还原始终以 100% 应用' : '仅调整风格强度，基础还原保持完整';
  $('pipeline-note').textContent = look?.stage==='technical'?'使用自定义技术 LUT 完成色彩还原':$('profile').value==='dlogm'?'先完整还原，再叠加风格滤镜':'保留标准色彩，仅应用风格调整';
}
function renderEnhancement() {
  const media=currentMedia(),look=currentLook();
  const preset=look?.id==='dji_reference'||look?.includes_reference;
  const identity=look?.id==='original'&&$('profile').value==='rec709';
  const enabled=media?.enhance_enabled??pendingEnhance;
  const amount=document.activeElement===$('enhance-strength')&&pendingEnhanceEdit?Number($('enhance-strength').value):(media?.enhance_strength??pendingEnhanceStrength);
  $('enhance-enabled').checked=!preset&&!identity&&enabled;
  $('enhance-enabled').disabled=preset||identity;
  $('enhance-strength').hidden=preset||identity;
  $('enhance-strength').disabled=preset||identity||!enabled;
  if(document.activeElement!==$('enhance-strength'))$('enhance-strength').value=amount;
  $('enhance-value').textContent=preset?'预设自带':identity?'—':amount+'%';
  $('enhance-note').textContent=preset?'此预设已包含增强，使用上方风格强度调节，不会重复叠加。':identity?'当前为原色保留，选择滤镜后可开启增强。':'再次应用当前 LUT 的曲线，强度单独调节，建议先预览。';
}
function updateCompareVisibility() {
  const available = imagesReady && Boolean(currentMedia());
  $('before-image').hidden = !available || !compare;
  for (const id of ['compare-divider','compare-slider','before-label']) $(id).hidden = !available || !compare;
  $('after-image').hidden = $('after-label').hidden = $('frame-time').hidden = !available;
  $('view-compare').classList.toggle('active',compare); $('view-compare').setAttribute('aria-pressed',compare);
  $('view-after').classList.toggle('active',!compare); $('view-after').setAttribute('aria-pressed',!compare);
}
function renderFocus() {
  const media = currentMedia();
  $('focus-name').textContent = media?.name || '画面预览';
  $('source-meta').textContent = media?.status==='ready' ? `${media.width} × ${media.height}  /  ${media.fps.toFixed(2)} fps  /  ${media.pix_fmt.includes('10')?'10-bit':'8-bit'}  /  ${media.audio?'有音轨':'无音轨'}` : media?.status==='checking'?'正在读取视频信息…':'选择素材后显示画面信息';
  $('seek').disabled = media?.status!=='ready';
  if (!media) {
    ++previewGeneration; previewSignature=''; imagesReady=false;
    $('viewer-empty').hidden=false; $('preview-loading').hidden=$('preview-error').hidden=true;
    updateCompareVisibility(); return;
  }
  $('viewer-empty').hidden=true;
  if (media.status==='checking' || state.ready!=='ready') {
    $('preview-loading').hidden=false; imagesReady=false; updateCompareVisibility(); return;
  }
  if (media.status!=='ready') {
    $('preview-loading').hidden=true; $('preview-error').hidden=false; imagesReady=false; updateCompareVisibility(); return;
  }
  currentSecond=Math.max(0,Math.min(currentSecond,Math.max(0,media.duration-.05)));
  $('seek').max=Math.max(.05,media.duration-.05); $('seek').value=currentSecond;
  $('current-time').textContent=$('frame-time').textContent=time(currentSecond,true);
  $('duration-time').textContent=time(media.duration,true);
  const signature=JSON.stringify([media.id,media.filter,state.looks.find(l=>l.id===media.filter)?.revision,media.strength,media.enhance_enabled,media.enhance_strength,currentSecond,$('profile').value,$('input-range').value]);
  if (signature!==previewSignature) { previewSignature=signature; requestPreview(media); }
}
function loadImage(url) {
  return new Promise((resolve,reject)=>{const img=new Image();img.onload=()=>resolve(url);img.onerror=()=>reject(new Error('此画面暂时无法预览，可换一个时间点或重试'));img.src=url;});
}
async function requestPreview(media) {
  const generation=++previewGeneration;
  $('preview-loading').hidden=false; $('preview-error').hidden=true;
  try {
    const [before,after]=await Promise.all([loadImage(previewUrl(media,'original',100,true)),loadImage(previewUrl(media))]);
    if (generation!==previewGeneration) return;
    $('before-image').src=before; $('after-image').src=after;
    $('after-label').textContent=media.filter==='original' && $('profile').value==='rec709' ? '原色保留' : state.looks.find(l=>l.id===media.filter)?.name||'Rec.709';
    $('preview-loading').hidden=true; imagesReady=true; updateCompareVisibility();
  } catch(error) {
    if (generation!==previewGeneration) return;
    $('preview-loading').hidden=true; $('preview-error').hidden=false;
    $('preview-error').firstElementChild.textContent=error.message;
  }
}
function renderJobs() {
  const jobs=state.jobs, active=jobs.filter(j=>['queued','running'].includes(j.status));
  const paused=Boolean(state.queue_paused);
  $('pause-queue').textContent=paused?'继续处理':'全部暂停';
  $('pause-queue').disabled=queueControlBusy||(!active.length&&!paused);
  $('pause-queue').setAttribute('aria-pressed',String(paused));
  $('cancel-queue').disabled=queueControlBusy||!active.length;
  $('queue-status').textContent=paused?'队列已暂停，点击继续处理可恢复':'任务会在后台继续处理';
  $('queue-dialog').classList.toggle('queue-paused',paused);
  $('queue-count').textContent=jobs.length;
  const pending=jobs.filter(j=>['queued','running'].includes(j.status)||(j.status==='done'&&j.browser_target&&!j.browser_saved)).length;
  $('queue-badge').textContent=pending;$('queue-badge').hidden=!pending;$('open-queue').classList.toggle('busy',pending>0&&!paused);
  $('clear-jobs').disabled=!jobs.some(j=>['done','failed','cancelled','interrupted'].includes(j.status)&&(!j.temporary_output||j.status!=='done'||j.cache_removed));
  const running=jobs.find(j=>j.status==='running');
  const signature=JSON.stringify([$('queue-dialog').open,queuePage,paused,jobs.map(j=>[j.id,j.status,Math.round(j.progress*1000),j.speed,j.eta,j.notice,j.encoder_used,browserOutput.status(j),expandedJobs.has(j.id)])]);
  if (signature!==jobsSignature && $('queue-dialog').open) {
    jobsSignature=signature;
    if (!jobs.length) {$('queue-pages').hidden=true; $('job-list').innerHTML=`<div class="queue-empty">${icon('clock')}<div><p>准备好，就开始吧</p><span>选择素材与滤镜，处理任务会显示在这里</span></div></div>`;}
    else {
      orderedJobs=[...jobs].sort((a,b)=>({running:0,queued:1}[a.status]??2)-({running:0,queued:1}[b.status]??2) || (a.status==='queued'?a.created-b.created:b.created-a.created));
      const totalPages=Math.ceil(orderedJobs.length/PAGE_SIZE);queuePage=Math.min(queuePage,totalPages-1);
      $('queue-pages').hidden=totalPages<=1;$('queue-prev').disabled=queuePage===0;$('queue-next').disabled=queuePage>=totalPages-1;$('queue-page-label').textContent=`${queuePage+1} / ${totalPages} · 共 ${jobs.length} 项`;
      const scroll=$('job-list').scrollTop;
      $('job-list').innerHTML=orderedJobs.slice(queuePage*PAGE_SIZE,(queuePage+1)*PAGE_SIZE).map(job=>{
        const percent=Math.round(job.progress*100), running=job.status==='running', transfer=browserOutput.status(job);
        const labels={queued:paused?'已暂停 · 等待继续':'等待处理',running:paused?'已暂停 · 进度已保留':job.eta==null?'正在准备编码':estimate(job.eta)+'剩余',done:!transfer?'已完成':transfer.status==='saved'?'已保存 · '+job.browser_destination:transfer.status==='saving'?'正在保存成片':transfer.status==='failed'?'保存失败':'待保存到所选目录',failed:'处理失败',cancelled:'已取消',interrupted:'上次处理已中止'};
        let actions='';
        if (['queued','running'].includes(job.status)) actions=`<button type="button" class="icon-button" data-cancel="${job.id}" aria-label="取消 ${esc(job.name)}" title="取消任务">${icon('close')}</button>`;
        if (job.status==='done'&&!job.cache_removed) actions=`${transfer&&['failed','waiting'].includes(transfer.status)?`<button type="button" class="icon-button" data-save="${job.id}" aria-label="重试保存成片" title="重试保存成片">${icon('retry')}</button>`:''}<a class="icon-button" href="/api/download/${job.id}" aria-label="下载 ${esc(job.name)}" title="下载成片">${icon('download')}</a>`;
        if (['failed','cancelled','interrupted'].includes(job.status)) actions=`<button type="button" class="icon-button" data-retry="${job.id}" aria-label="重试 ${esc(job.name)}" title="重试">${icon('retry')}</button>`;
        return `<div class="job-wrapper"><div class="job-row ${job.status}"><span class="job-icon">${running?'<span class="spinner"></span>':icon(job.status==='done'?'check':job.status==='failed'?'close':'clock')}</span><div class="job-info"><strong>${esc(job.name)}</strong><div class="job-meta"><span>${esc(job.filter_name)}</span><span>·</span><span>${esc(labels[job.status])}</span>${running&&job.speed>0?`<span>· ${job.speed.toFixed(1)}×</span>`:''}${job.status==='failed'?`<button type="button" class="text-button error-toggle" data-error="${job.id}">查看原因</button>`:''}</div>${job.notice?`<div class="job-notice">${esc(job.notice)}</div>`:''}${transfer?.status==='failed'?`<div class="job-notice">${esc(transfer.error)}</div>`:''}${transfer?.status==='saving'?`<div class="progress-track"><span class="running" style="width:${Math.round(transfer.progress*100)}%"></span></div>`:''}${running?`<div class="progress-track"><span class="running" style="width:${percent}%"></span></div>`:''}</div>${running?`<span class="job-percent">${percent}%</span>`:''}<div class="job-actions">${actions}</div></div>${expandedJobs.has(job.id)?`<pre class="job-error">${esc(job.error)}</pre>`:''}</div>`;
      }).join('');
      $('job-list').scrollTop=scroll;
    }
  }
  $('queue-flow').hidden=!paused&&!state.flow?.paused;$('queue-flow').textContent=paused?'当前转码和后续任务已暂停，点击“继续处理”后接着处理。已完成成片仍会继续保存。':state.flow?.paused?state.flow.reason+'；条件恢复后队列会自动继续。':'';
  $('queue-summary').hidden=!active.length;
  if (active.length) {
    const batch=(running||active[0]).batch;
    const currentBatch=jobs.filter(j=>j.batch===batch);
    const done=currentBatch.filter(j=>j.status==='done').length;
    const totalWeight=currentBatch.reduce((s,j)=>s+Math.max(.1,j.duration||0),0);
    const completed=currentBatch.reduce((s,j)=>s+Math.max(.1,j.duration||0)*(j.status==='done'?1:j.status==='running'?j.progress:0),0);
    const percent=Math.round(completed/totalWeight*100);
    $('queue-description').textContent=`本批完成 ${done} / ${currentBatch.length} 段 · ${paused?'队列已暂停':active.length+' 段处理中或等待'}`;
    $('queue-percent').textContent=percent+'%'; $('queue-progress').style.width=percent+'%'; $('queue-progress').classList.toggle('running',!!running&&!paused);
  }
  $('background-status').innerHTML=paused?'<span class="pause-mark" aria-hidden="true">Ⅱ</span><span>队列已暂停</span>':state.flow?.paused?`<span class="spinner"></span><span>${esc(state.flow.reason)}</span>`:running?`<span class="spinner"></span><span>后台处理中 · ${Math.round(running.progress*100)}%</span>`:`<span class="small-dot"></span><span>完成后自动保存到所选文件夹</span>`;
}

async function controlQueue(action) {
  if(queueControlBusy)return;
  queueControlBusy=true;renderJobs();
  try {
    if(action==='pause') {
      const result=await api('/api/queue/pause',{paused:!state.queue_paused});
      state.queue_paused=result.paused;
      toast(result.paused?'已暂停当前转码和后续任务':'队列已继续');
    } else {
      const result=await api('/api/queue/cancel');
      toast(result.count?`已取消 ${result.count} 个未完成任务`:'没有需要取消的任务');
    }
    await refresh(true);
  }catch(error){toast(error.message,true);}
  finally{queueControlBusy=false;renderJobs();}
}
function renderSummary() {
  const chosen=state.media.filter(m=>selected.has(m.id)), ready=chosen.filter(m=>m.status==='ready'),checking=chosen.filter(m=>m.status==='checking');
  $('selection-title').textContent=chosen.length?`已选 ${chosen.length} 段视频`:'等待选择素材';
  const duration=chosen.reduce((s,m)=>s+m.duration,0), bytes=chosen.reduce((s,m)=>s+m.bytes,0);
  $('selection-detail').textContent=chosen.length?`${time(duration)} 总时长 · ${size(bytes)}${checking.length?' · 正在读取 '+checking.length+' 段':ready.length<chosen.length?' · 不可用 '+(chosen.length-ready.length)+' 段':''}`:'勾选后可批量应用同一滤镜';
  const disabled=!connected || state.ready!=='ready' || !ready.length || chosen.some(m=>m.status==='checking') || outputPicking || exportPending || !browserOutput.current;
  $('start-export').disabled=$('preview-export').disabled=disabled;
  $('start-export').firstElementChild.textContent=checking.length?`读取素材中 · ${chosen.length-checking.length}/${chosen.length}`:state.jobs.some(j=>['queued','running'].includes(j.status))?`加入队列 · ${ready.length} 段`:ready.length?`开始处理 · ${ready.length} 段`:'开始处理';
  renderOutputEditor();
  $('quality-options').hidden=$('rate-mode').value==='source';
  const fps=$('fps').value, sourceFps=currentMedia()?.fps||0;
  const targetFps=fps.includes('/')?Number(fps.split('/')[0])/Number(fps.split('/')[1]):Number(fps);
  $('fps-note').textContent=fps==='source'?(sourceFps?'保留原片 '+sourceFps.toFixed(2)+' fps，不改变播放速度。':'保持原片帧率与播放速度。'):sourceFps&&targetFps>sourceFps+.05?'高于原片时采用重复帧，不会增加真实运动细节。':'按所选帧率导出，播放速度与时长保持不变。';
  const gpu=$('encoder').value==='nvenc'||($('encoder').value==='auto'&&state.nvenc?.available);
  const bitrate=Number(currentMedia()?.source_bitrate||currentMedia()?.video?.bit_rate||0);
  $('encode-note').textContent=(gpu?'显卡编码 · 10-bit':$('encoder').value==='h264'?'H.264 · 8-bit':'CPU 编码 · 10-bit')+($('rate-mode').value==='source'&&bitrate?' · 目标 '+(bitrate/1e6).toFixed(1)+' Mbps':'');
}

function renderOutputEditor() {
  $('choose-output').disabled=outputPicking;
  $('output-name').textContent=browserOutput.current?.name||'选择文件夹';
  $('output-name').title=browserOutput.current?.name||'';
  $('output-state').textContent=outputPicking?'选择中…':browserOutput.current?(browserOutput.current.permission==='granted'?'已选择':'待授权'):'未选择';
  $('output-note').textContent=browserOutput.current?(browserOutput.current.permission==='granted'?'成片会自动保存到此文件夹；保存时请保持页面打开。':'请再次选择此文件夹，授权保存成片。'):'点击选择保存文件夹。支持 Chrome / Edge。';
}
async function chooseOutputDirectory() {
  outputPicking=true;renderSummary();
  try {
    await browserOutput.choose();toast('已选择保存文件夹');
    browserOutput.flush(state.jobs).catch(()=>{});
  } catch(error) {if(error.name!=='AbortError')toast(error.message,true);}
  finally {outputPicking=false;renderSummary();}
}

function focusMedia(identity) {
  if (focus!==identity) {focus=identity;currentSecond=0;imagesReady=false;previewSignature='';previewGeneration++;}
  const index=state.media.findIndex(m=>m.id===identity);if(index>=0)mediaPage=Math.floor(index/PAGE_SIZE);
  render();
}
function applyLook(identity, strength = null) {
  if(strength==null)strength=state.looks.find(l=>l.id===identity)?.default_strength??100;
  pendingLook=identity;
  pendingStrength=strength;
  let ids=[...selected];
  if (!ids.length && focus) {ids=[focus];selected.add(focus);}
  if (!ids.length) {lookSignature='';renderLooks();return Promise.resolve();}
  if (!ids.includes(focus)) {focus=ids[0];currentSecond=0;imagesReady=false;previewSignature='';previewGeneration++;}
  const look=state.looks.find(l=>l.id===identity);
  const values={filter:identity,strength:look?.stage==='technical'?100:strength};
  return queueMediaPatch(ids,values,'/api/apply',values);
}
function queueMediaPatch(ids,values,route,payload) {
  const version=++editVersion;
  for (const id of ids) {
    optimistic.set(id,{version,values:{...optimistic.get(id)?.values,...values}});
    const media=state.media.find(m=>m.id===id); if(media)Object.assign(media,values);
  }
  render();
  applySerial=applySerial.catch(()=>{}).then(()=>api(route,{ids,...payload})).then(()=>{
    for(const id of ids) if(optimistic.get(id)?.version===version) optimistic.delete(id);
  }).catch(error=>{for(const id of ids)if(optimistic.get(id)?.version===version)optimistic.delete(id);toast(error.message,true);}).finally(()=>refresh());
  return applySerial;
}
function applyEnhancement(enabled,strength,explicitIds=null) {
  pendingEnhance=enabled;pendingEnhanceStrength=strength;
  let ids=explicitIds||[...selected];
  if(!ids.length&&focus){ids=[focus];selected.add(focus);}
  if(!ids.length){if(state)renderEnhancement();return Promise.resolve();}
  if(!ids.includes(focus)){focus=ids[0];currentSecond=0;imagesReady=false;previewSignature='';previewGeneration++;}
  return queueMediaPatch(ids,{enhance_enabled:enabled,enhance_strength:strength},'/api/enhance',{enabled,strength});
}
function commitEnhancement() {
  clearTimeout(enhanceTimer);
  const edit=pendingEnhanceEdit;pendingEnhanceEdit=null;
  return edit?applyEnhancement(edit.enabled,edit.strength,edit.ids):Promise.resolve();
}
function rememberPrefs() { Object.assign(prefs,settings());localStorage.setItem('color-studio-prefs',JSON.stringify(prefs));lookSignature='';previewSignature='';render(); }
async function acceptImport(result) {
  if (!result.ids?.length) return;
  await refresh(true);
  selected = new Set(result.ids);
  focusMedia(result.ids[0]);
  if(pendingLook!=='original') await applyLook(pendingLook,pendingStrength);
  if(pendingEnhance||pendingEnhanceStrength!==100) await applyEnhancement(pendingEnhance,pendingEnhanceStrength);
  toast(result.added?`已添加 ${result.added} 段视频`:'这些视频已在素材库中');
  render();
}
async function revealOutput(id) {
  const jobs=state.jobs.filter(job=>job.status==='done'&&(!id||job.id===id));
  $('results-reason').textContent='已完成的成片可以在这里下载。选择保存文件夹后会自动写入该目录。';
  $('output-files').innerHTML=jobs.length?jobs.map(job=>`<div class="output-file">${icon('film')}<div><strong>${esc(job.browser_filename||job.filename||job.output.split(/[\\/]/).pop())}</strong><span>${size(job.bytes||0)}${job.browser_saved?' · 已保存':''}</span></div>${job.cache_removed?'<span class="subtle">已保存</span>':`<a class="button button-light" href="/api/download/${job.id}">${icon('download')}下载</a>`}</div>`).join(''):'<div class="output-files-empty">暂时没有已完成的成片。</div>';
  $('results-dialog').showModal();
}
async function removeLook(id) {
  const look=state.looks.find(item=>item.id===id);if(!look)return;
  await act(async()=>{
    await api('/api/looks/delete',{id});if(pendingLook===id)pendingLook='original';
    lookSignature='';previewSignature='';await refresh();toast('已移除 '+look.name);
    const undo=document.createElement('button');undo.textContent='撤销';undo.type='button';
    undo.onclick=()=>act(async()=>{await api('/api/looks/restore',{id});lookSignature='';await refresh();toast('已恢复 '+look.name);});
    $('toast').appendChild(undo);
  });
}
async function pick(kind) {
  if(kind==='output')return chooseOutputDirectory();
  const button=$(kind==='files'?'add-files':'add-folder');
  button.disabled=true;
  try {
    const result=await api('/api/picker',{kind});
    await acceptImport(result);
  } catch(error) {
    toast(error.message,true);
    $('path-dialog').showModal();
  } finally {button.disabled=false;}
}
async function exportVideos(preview) {
  exportPending=true;renderSummary();
  try {
    clearTimeout(strengthTimer); await commitEnhancement(); await applySerial;
    const target=await browserOutput.target();
    if(state.media.some(m=>selected.has(m.id)&&m.status==='checking'))throw new Error('请等待选中的素材全部读取完成后再开始处理');
    const ids=state.media.filter(m=>selected.has(m.id)&&m.status==='ready').map(m=>m.id);
    const unavailable=state.media.filter(m=>selected.has(m.id)&&['error','missing'].includes(m.status));if(unavailable.length)toast(`${unavailable.length} 段素材不可用，将只处理可用视频`,true);
    const options=settings(); if(preview)options.preview=8;
    const result=await api('/api/jobs',{ids,settings:options,browser_target:target.id,browser_destination:target.name,browser_stream_version:2});
    toast(`已加入 ${result.ids.length} 个${preview?'试片':'处理'}任务`);
    await refresh();
    if(!$('queue-dialog').open){$('queue-dialog').showModal();queuePage=0;jobsSignature='';renderJobs();}
  } catch(error) {toast(error.message,true);} finally {exportPending=false;renderSummary();}
}
async function uploadFiles(files) {
  if(importing)return toast('请等待当前导入完成');
  files=[...files].filter(f=>/\.(mp4|mov|m4v)$/i.test(f.name));
  if(!files.length)return toast('请选择 MP4、MOV 或 M4V 视频',true);
  importing=true;$('import-status').hidden=false;
  let completed=0,total=files.reduce((s,f)=>s+f.size,0),ids=[],added=0;
  try {
    for(const file of files) {
      $('import-label').textContent='导入 '+file.name;
      const result=await new Promise((resolve,reject)=>{
        const xhr=new XMLHttpRequest();xhr.open('POST','/api/upload');xhr.setRequestHeader('X-Workspace-Token',token);xhr.setRequestHeader('X-File-Name',encodeURIComponent(file.name));xhr.setRequestHeader('Content-Type','application/octet-stream');
        xhr.upload.onprogress=event=>{const percent=Math.round((completed+event.loaded)/Math.max(1,total)*100);$('import-progress').style.width=percent+'%';$('import-percent').textContent=percent+'%';};
        xhr.onload=()=>{try{const result=JSON.parse(xhr.responseText);xhr.status===200?resolve(result):reject(new Error(result.error));}catch{reject(new Error('导入失败'));}};
        xhr.onerror=()=>reject(new Error('本地传输中断'));xhr.send(file);
      });
      ids.push(...result.ids);added+=result.added;completed+=file.size;
    }
    await acceptImport({ids,added});
  }catch(error){toast(error.message,true);await refresh();}finally{importing=false;$('import-status').hidden=true;}
}

$('add-files').onclick=$('viewer-add').onclick=()=>pick('files');$('add-folder').onclick=()=>pick('folder');$('choose-output').onclick=()=>pick('output');
$('path-button').onclick=()=>{$('path-dialog').showModal();$('path-input').focus();};
$('browser-pick').onclick=()=>{$('path-dialog').close();$('fallback-files').click();};
$('submit-path').onclick=()=>act(async()=>{const paths=$('path-input').value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean);if(!paths.length)throw new Error('请输入视频或文件夹路径');$('submit-path').disabled=true;try{await acceptImport(await api('/api/import',{paths}));$('path-dialog').close();$('path-input').value='';}finally{$('submit-path').disabled=false;}});
$('reveal-output').onclick=()=>act(()=>revealOutput());
$('media-prev').onclick=()=>{mediaPage=Math.max(0,mediaPage-1);mediaSignature='';$('media-list').scrollTop=0;renderMedia();};
$('media-next').onclick=()=>{mediaPage=Math.min(Math.ceil(state.media.length/PAGE_SIZE)-1,mediaPage+1);mediaSignature='';$('media-list').scrollTop=0;renderMedia();};
$('queue-prev').onclick=()=>{queuePage=Math.max(0,queuePage-1);jobsSignature='';$('job-list').scrollTop=0;renderJobs();};
$('queue-next').onclick=()=>{queuePage=Math.min(Math.ceil(state.jobs.length/PAGE_SIZE)-1,queuePage+1);jobsSignature='';$('job-list').scrollTop=0;renderJobs();};
$('select-all').onchange=event=>{selected=event.target.checked?new Set(state.media.map(m=>m.id)):new Set();render();};
$('media-list').onclick=event=>{const row=event.target.closest('[data-media]');if(!row)return;const checkbox=event.target.closest('[data-select]');if(checkbox){checkbox.checked?selected.add(checkbox.dataset.select):selected.delete(checkbox.dataset.select);render();}else if(!event.target.closest('label'))focusMedia(row.dataset.media);};
$('media-list').onkeydown=event=>{if(event.key==='Enter'&&event.target.dataset.media)focusMedia(event.target.dataset.media);};
$('remove-media').onclick=()=>act(async()=>{await api('/api/remove',{ids:[...selected]});selected.clear();await refresh();});
$('looks-grid').onclick=event=>{const remove=event.target.closest('[data-remove-look]');if(remove){removeLook(remove.dataset.removeLook);return;}const card=event.target.closest('[data-id]');if(card)applyLook(card.dataset.id);};
$('restore-looks').onclick=()=>act(async()=>{await api('/api/looks/restore');lookSignature='';await refresh();toast('已恢复移除的滤镜');});
$('open-queue').onclick=()=>{if(!$('queue-dialog').open){$('queue-dialog').showModal();jobsSignature='';renderJobs();}};
$('strength').oninput=()=>{$('reset-strength').textContent=$('strength').value+'%';clearTimeout(strengthTimer);strengthTimer=setTimeout(()=>applyLook(currentLook().id,Number($('strength').value)),300);};
$('strength').onchange=()=>{clearTimeout(strengthTimer);applyLook(currentLook().id,Number($('strength').value));};
$('reset-strength').onclick=()=>applyLook(currentLook().id,currentLook().default_strength??100);
$('enhance-enabled').onchange=()=>{clearTimeout(enhanceTimer);pendingEnhanceEdit=null;applyEnhancement($('enhance-enabled').checked,Number($('enhance-strength').value));};
$('enhance-strength').oninput=()=>{
  $('enhance-value').textContent=$('enhance-strength').value+'%';clearTimeout(enhanceTimer);
  pendingEnhanceEdit={enabled:$('enhance-enabled').checked,strength:Number($('enhance-strength').value),ids:[...selected]};
  enhanceTimer=setTimeout(commitEnhancement,300);
};
$('enhance-strength').onchange=()=>{if(!pendingEnhanceEdit)pendingEnhanceEdit={enabled:$('enhance-enabled').checked,strength:Number($('enhance-strength').value),ids:[...selected]};commitEnhancement();};
for(const id of ['profile','encoder','quality','rate-mode','fps','input-range','audio-aac'])$(id).onchange=rememberPrefs;
$('compare-slider').oninput=()=>{$('viewer').style.setProperty('--split',$('compare-slider').value+'%');};
$('compare-slider').onchange=$('compare-slider').oninput;
$('view-compare').onclick=()=>{compare=true;updateCompareVisibility();};$('view-after').onclick=()=>{compare=false;updateCompareVisibility();};
$('seek').oninput=()=>{currentSecond=Number($('seek').value);$('current-time').textContent=time(currentSecond,true);clearTimeout(seekTimer);seekTimer=setTimeout(()=>renderFocus(),350);};
$('seek').onchange=()=>{clearTimeout(seekTimer);renderFocus();};
$('retry-preview').onclick=()=>{previewSignature='';renderFocus();};
$('start-export').onclick=()=>exportVideos(false);$('preview-export').onclick=()=>exportVideos(true);
$('job-list').onclick=event=>{const button=event.target.closest('[data-cancel],[data-retry],[data-reveal],[data-error],[data-save]');if(!button)return;if(button.dataset.save){const job=state.jobs.find(j=>j.id===button.dataset.save);if(job){browserOutput.retry(job);browserOutput.flush(state.jobs).catch(()=>{});}return;}if(button.dataset.error){expandedJobs.has(button.dataset.error)?expandedJobs.delete(button.dataset.error):expandedJobs.add(button.dataset.error);jobsSignature='';renderJobs();return;}if(button.dataset.reveal){act(()=>revealOutput(button.dataset.reveal));return;}const route=button.dataset.cancel?'cancel':'retry';act(async()=>{await api('/api/'+route,{id:button.dataset.cancel||button.dataset.retry});await refresh();});};
$('clear-jobs').onclick=()=>act(async()=>{await api('/api/clear-jobs');await refresh();});
$('pause-queue').onclick=()=>controlQueue('pause');
$('cancel-queue').onclick=()=>controlQueue('cancel');
$('help-button').onclick=()=>$('help-dialog').showModal();$('settings-button').onclick=()=>$('settings-dialog').showModal();
$('shutdown').onclick=()=>act(async()=>{await api('/api/shutdown');$('settings-dialog').close();connection(false,'服务已关闭');toast('服务已退出，双击启动入口可重新打开');});
$('engine-banner').onclick=event=>{if(event.target.id==='retry-setup')act(async()=>{await api('/api/setup');await refresh();});};
$('import-lut').onclick=()=>$('lut-dialog').showModal();
$('submit-lut').onclick=()=>act(async()=>{
  const files=[...$('lut-file').files];if(!files.length)throw new Error('请选择一个或多个 .cube 文件');
  const added=[],errors=[];$('submit-lut').disabled=true;
  try{
    for(let i=0;i<files.length;i++){
      const file=files[i];$('submit-lut').textContent=`导入 ${i+1}/${files.length}`;
      try{const response=await fetch('/api/luts?stage='+$('lut-stage').value,{method:'POST',headers:{'X-Workspace-Token':token,'X-File-Name':encodeURIComponent(file.name)},body:file});const result=await response.json();if(!response.ok)throw new Error(result.error);added.push(result);}
      catch(error){errors.push(file.name+': '+error.message);}
    }
    lookSignature='';await refresh();
    if(added.length){$('lut-dialog').close();$('lut-file').value='';await applyLook(added[0].id,100);}
    toast(errors.length?`已导入 ${added.length} 个，失败 ${errors.length} 个：`+errors.join('；'):`已导入 ${added.length} 个 LUT`,Boolean(errors.length));
  }finally{$('submit-lut').disabled=false;$('submit-lut').textContent='导入滤镜';}
});
let dragDepth=0;
document.addEventListener('dragenter',event=>{if([...event.dataTransfer.types].includes('Files')){event.preventDefault();dragDepth++;$('drop-overlay').hidden=false;}});
document.addEventListener('dragover',event=>{if([...event.dataTransfer.types].includes('Files'))event.preventDefault();});
document.addEventListener('dragleave',event=>{event.preventDefault();dragDepth=Math.max(0,dragDepth-1);if(!dragDepth)$('drop-overlay').hidden=true;});
document.addEventListener('drop',event=>{event.preventDefault();dragDepth=0;$('drop-overlay').hidden=true;uploadFiles(event.dataTransfer.files);});
$('fallback-files').onchange=()=>uploadFiles($('fallback-files').files);
document.addEventListener('keydown',event=>{if(event.key==='Escape'){$('drop-overlay').hidden=true;dragDepth=0;}});
refresh();setInterval(refresh,2000);
