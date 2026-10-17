'use strict';
const {t} = DevHardI18n;
const $ = (selector) => document.querySelector(selector);
const token = $('meta[name="devhard-token"]').content;
const form = $('#workspace-form');
let editing = false, connected = false, submitting = false, connectionLost = false, state = null, signature = '';


function el(tag, text, cls) { const node = document.createElement(tag); if(text !== undefined) node.textContent = text; if(cls) node.className = cls; return node; }
let currentNotice = null;
function notice(key, params={}, failure=false) { currentNotice={key,params,failure}; const node=$('#notice'); node.hidden=false; node.textContent=t(key,key==='accepted'?{...params,action:t(params.action)}:params); node.className=failure?'failure':''; }
async function api(path, method='GET', data) {
  const response = await fetch(path,{method,headers:{Authorization:`Bearer ${token}`,...(data === undefined ? {} : {'Content-Type':'application/json'})},body:data === undefined ? undefined : JSON.stringify(data),signal:AbortSignal.timeout(10000)});
  let body; try { body=await response.json(); } catch { throw new Error(t('responseFailed',{status:response.status})); }
  if(!response.ok) throw new Error(body.detail || t('requestFailed',{status:response.status}));
  return body;
}
function badge(title, value) {
  const cls=['listening','succeeded'].includes(value)?'good':value==='running'?'info':['unknown','queued','partial'].includes(value)?'warn':['error','failed'].includes(value)?'bad':['stopped','exited','cancelled'].includes(value)?'idle':'';
  return el('span',`${title ? title+' · ' : ''}${t(value)}`,`badge ${cls}`);
}
function button(text, handler, cls='') { const node=el('button',text,cls); node.type='button'; node.disabled=!connected; node.addEventListener('click',handler); return node; }
async function command(name, component, action, body={}) {
  try {
    const job=await api(`/api/workspaces/${encodeURIComponent(name)}/${component}/${action}`,'POST',body);
    notice('accepted',{name,action:job.action});
    await poll();
  } catch(error) { notice('rawError',{message:error.message},true); }
}
function bookmarkField(labelKey, field, value, type='text') {
  const label=el('label'), caption=el('span',t(labelKey)); caption.dataset.i18n=labelKey;
  const input=el(type==='textarea'?'textarea':'input'); input.dataset.field=field;
  if(type==='textarea') input.rows=2; else input.type=type;
  input.value=value; if(field==='name' || field==='url') input.required=true;
  input.maxLength=field==='name'?512:field==='description'?2000:8192;
  label.append(caption,input); return label;
}
function addBookmark(bookmark={name:'',description:'',url:'',initial:false}) {
  const row=el('div',undefined,'bookmark-editor-row');
  row.append(bookmarkField('bookmarkName','name',bookmark.name),bookmarkField('bookmarkUrl','url',bookmark.url,'url'),bookmarkField('description','description',bookmark.description,'textarea'));
  const footer=el('div',undefined,'bookmark-editor-footer'), label=el('label',undefined,'checkbox');
  const check=el('input'); check.type='checkbox'; check.dataset.field='initial'; check.checked=bookmark.initial;
  const caption=el('span',t('initialOption')); caption.dataset.i18n='initialOption'; label.append(check,caption);
  const remove=button(t('remove'),()=>row.remove()); remove.dataset.i18n='remove';
  footer.append(label,remove); row.append(footer); $('#bookmark-fields').append(row);
}
function edit(config=null, locked=false) {
  editing=Boolean(config); form.reset();
  form.elements.name.readOnly=editing;
  form.elements.host.readOnly=locked; form.elements.executable.readOnly=locked; form.elements.restore.disabled=locked;
  $('#bookmark-fields').replaceChildren();
  if(config) {
    form.elements.name.value=config.name; form.elements.host.value=config.ssh.host;
    form.elements.restore.checked=config.browser.restore_session;
    form.elements.executable.value=config.browser.executable;
    config.bookmarks.forEach(addBookmark);
  }
  $('#editor-title').textContent=t(editing?'editTitle':'newTitle');
  $('#save-workspace').textContent=t(editing?'save':'register');
  $('#editor').hidden=false; (editing?$('#add-bookmark'):form.elements.name).focus();
}
function render() {
  const busyNames=new Set(state.jobs.filter(j=>['queued','running'].includes(j.state)).map(j=>j.workspace));
  const key=JSON.stringify([state.workspaces,state.jobs,connected,DevHardI18n.language]); if(key===signature) return; signature=key;
  const list=$('#workspaces'); list.replaceChildren(); $('#workspace-count').textContent=state.workspaces.length;
  if(!state.workspaces.length) list.append(el('div',t('empty'),'empty'));
  for(const workspace of state.workspaces) {
    const name=workspace.name, busy=busyNames.has(name), card=el('article',undefined,'card'); card.dataset.workspace=name;
    const top=el('div',undefined,'card-top'), title=el('div'); title.append(el('h3',name),el('div',workspace.config.ssh.host,'host'));
    const locked=Object.values(workspace.status).some(s=>!['stopped','exited','error'].includes(s));
    const editButton=button(t('settings'),()=>edit(workspace.config,locked),'edit');
    editButton.disabled=!connected || busy;
    top.append(title,editButton); const states=el('div',undefined,'states');
    states.append(badge('SSH',workspace.status.tunnel),badge('Chrome',workspace.status.browser));
    const controls=el('div',undefined,'controls');
    controls.append(button(t('startAll'),()=>command(name,'workspace','start_all'),'primary'),button(t('stopAll'),()=>command(name,'workspace','stop_all')));
    const components=el('div',undefined,'components');
    for(const [component,title] of [['tunnel','SSH'],['browser','Chrome']]) {
      const row=el('div',undefined,'component-row'), actions=el('div'); row.append(el('span',title));
      for(const [action,text] of [['start',t('start')],['stop',t('stop')],['restart',t('restart')]]) actions.append(button(text,()=>command(name,component,action)));
      row.append(actions); components.append(row);
    }
    if(busy) { for(const b of [...controls.querySelectorAll('button'),...components.querySelectorAll('button')]) b.disabled=true; }
    card.append(top,states,controls,components);
    const progress=el('div',t('busy'),'busy');
    progress.style.visibility=busy?'visible':'hidden';
    progress.setAttribute('aria-hidden',String(!busy));
    card.append(progress);
    const bookmarks=el('section',undefined,'bookmark-list'), heading=el('div',undefined,'section-heading');
    const canOpen=connected && !busy && workspace.status.browser==='running';
    const openAll=button(t('openAll'),()=>command(name,'bookmarks','open'));
    openAll.disabled=!canOpen || !workspace.config.bookmarks.length;
    openAll.title=t('openHelp'); heading.append(el('h4',t('bookmarks')),openAll); bookmarks.append(heading);
    workspace.config.bookmarks.forEach((bookmark,index)=>{
      const row=el('div',undefined,'bookmark-row'), content=el('div',undefined,'bookmark-content');
      const title=el('div',undefined,'bookmark-title'); title.append(el('strong',bookmark.name));
      if(bookmark.initial) { const tag=el('span','Initial','initial-tag'); tag.title=t('initialHelp'); title.append(tag); }
      content.append(title); if(bookmark.description) content.append(el('p',bookmark.description,'bookmark-description'));
      content.append(el('div',bookmark.url,'bookmark-url'));
      const open=button(t('open'),()=>command(name,'bookmarks','open',{index})); open.disabled=!canOpen; open.title=t('openHelp');
      row.append(content,open); bookmarks.append(row);
    });
    if(!workspace.config.bookmarks.length) bookmarks.append(el('p',t('noBookmarks'),'hint'));
    card.append(bookmarks);
    const error=workspace.observation_error || workspace.runtime?.last_error?.message;
    if(error) card.append(el('div',t('recentError',{message:error}),'error'));
    const detail=el('details'), summary=el('summary',t('connectionInfo')); detail.append(summary);
    detail.append(el('pre',`SOCKS: ${workspace.runtime?.reserved_port ? '127.0.0.1:'+workspace.runtime.reserved_port : t('unreserved')}\n${t('profile')}: ${workspace.profile_path || t('unavailable')}`)); card.append(detail);
    list.append(card);
  }
  const jobs=$('#jobs'); jobs.replaceChildren();
  for(const job of state.jobs.slice(-12).reverse()) {
    const row=el('li'); row.dataset.job=job.id;
    row.append(badge('',job.state),el('span',`${job.workspace} · ${t(job.action)}`,'job-description'),el('time',DevHardI18n.time(job.accepted_at)));
    if(job.error) row.append(el('div',job.error.message,'job-error')); jobs.append(row);
  }
  if(!state.jobs.length) jobs.append(el('li',t('noJobs'),'muted'));
  $('#manager-info').textContent=`${state.manager.api_url}\n${t('data')}: ${state.manager.data_root}\nPID: ${state.manager.pid}`;
}
let polling=false;
async function poll() {
  if(polling) return; polling=true;
  try {
    state=await api('/api/state'); connected=true;
    if(connectionLost) { $('#notice').hidden=true; currentNotice=null; }
    connectionLost=false;
    $('#connection').textContent=t('online'); $('#connection').className='online';
    $('#observed').textContent=state.observed_at?t('observed',{time:DevHardI18n.time(state.observed_at)}):'';
    if(state.observation_error) notice('observationFailed',{message:state.observation_error},true);
    render();
  } catch(error) {
    connected=false; connectionLost=true; $('#connection').textContent=t('offline'); $('#connection').className='offline';
    notice('connectionFailed',{},true);
    if(state) render();
  } finally { polling=false; $('#new-workspace').disabled=!connected; $('#save-workspace').disabled=!connected || submitting; }
}
$('#new-workspace').addEventListener('click',()=>edit());
$('#add-bookmark').addEventListener('click',()=>addBookmark());
$('#cancel-edit').addEventListener('click',()=>{$('#editor').hidden=true;});
form.addEventListener('submit',async(event)=>{
  event.preventDefault(); if(submitting) return; submitting=true; $('#save-workspace').disabled=true;
  const name=form.elements.name.value;
  const config={schema_version:1,name,ssh:{host:form.elements.host.value.trim()},bookmarks:[...$('#bookmark-fields').children].map(row=>({name:row.querySelector('[data-field=name]').value.trim(),url:row.querySelector('[data-field=url]').value.trim(),description:row.querySelector('[data-field=description]').value.trim(),initial:row.querySelector('[data-field=initial]').checked})),browser:{profile:'chrome-profile',restore_session:form.elements.restore.checked,executable:form.elements.executable.value.trim() || 'auto'}};
  try {
    await api(editing?`/api/workspaces/${encodeURIComponent(name)}`:'/api/workspaces',editing?'PUT':'POST',config);
    $('#editor').hidden=true; notice('accepted',{name,action:editing?'update':'register'}); await poll();
  } catch(error) { notice('rawError',{message:error.message},true); }
  finally { submitting=false; $('#save-workspace').disabled=!connected; }
});
function translate() {
  DevHardI18n.apply();
  $('#editor-title').textContent=t(editing?'editTitle':'newTitle');
  $('#save-workspace').textContent=t(editing?'save':'register');
  $('#connection').textContent=t(connected?'online':connectionLost?'offline':'connecting');
  $('#observed').textContent=state?.observed_at?t('observed',{time:DevHardI18n.time(state.observed_at)}):'';
  if(currentNotice) notice(currentNotice.key,currentNotice.params,currentNotice.failure);
  if(state) render();
}
$('#language').addEventListener('change',event=>{ DevHardI18n.select(event.target.value); translate(); });
window.addEventListener('storage',event=>{
  if(event.key===DevHardI18n.storageKey || event.key===null) { DevHardI18n.select(event.newValue,false); translate(); }
});
translate(); poll(); setInterval(poll,1200);
