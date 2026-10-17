'use strict';
const {t} = DevHardI18n;
const $ = (selector) => document.querySelector(selector);
const token = $('meta[name="devhard-token"]').content;
const form = $('#workspace-form');
let editing = false, connected = false, submitting = false, stoppingAll = false, connectionLost = false, state = null, signature = '';


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
    updateControls();
  } catch(error) { notice('rawError',{message:error.message},true); }
}
async function submitStopAll() {
  stoppingAll=true; updateControls();
  try {
    const job=await api('/api/workspaces/stop_all','POST',{});
    notice('accepted',{name:t('allWorkspaces'),action:job.action});
    updateControls();
  } catch(error) { notice('rawError',{message:error.message},true); }
  finally { stoppingAll=false; updateControls(); }
}
let currentConfirm=null;
function renderConfirm() {
  if(!currentConfirm) return;
  $('#confirm-title').textContent=t(currentConfirm.titleKey);
  $('#confirm-message').textContent=t(currentConfirm.messageKey,currentConfirm.params);
  $('#confirm-ok').textContent=t(currentConfirm.okKey);
  $('#confirm-cancel').textContent=t('cancel');
}
function openConfirm(descriptor) { currentConfirm=descriptor; renderConfirm(); showDialog($('#confirm')); $('#confirm-cancel').focus(); }
function closeConfirm() { $('#confirm').close(); }
function showDialog(dialog) { if(!dialog.open) dialog.showModal(); }
function dismissableDialog(dialog) {
  let armed=false;
  dialog.addEventListener('mousedown',event=>{ armed=event.target===dialog; });
  dialog.addEventListener('mouseup',event=>{ if(event.target!==dialog) armed=false; });
  dialog.addEventListener('click',event=>{ if(armed && event.target===dialog) dialog.close(); armed=false; });
}
function stopAllWorkspaces() {
  const active=(state?.workspaces||[]).filter(w=>['running','listening'].includes(w.status.tunnel) || w.status.browser==='running').length;
  if(active) return openConfirm({titleKey:'stopAllTitle',messageKey:'stopAllBody',params:{count:active},okKey:'stopAll',onConfirm:submitStopAll});
  submitStopAll();
}
let bookmarkDraft=[], bookmarkEditIndex=null;
function renderBookmarkDraft() {
  const list=$('#bookmark-fields'); list.replaceChildren();
  if(!bookmarkDraft.length) { list.append(el('p',t('noBookmarks'),'hint')); return; }
  bookmarkDraft.forEach((bookmark,index)=>{
    const row=el('div',undefined,'bookmark-editor-item'), content=el('div',undefined,'bookmark-content');
    const title=el('div',undefined,'bookmark-title'); title.append(el('strong',bookmark.name));
    if(bookmark.initial) { const tag=el('span',t('initialTag'),'initial-tag'); tag.title=t('initialHelp'); title.append(tag); }
    content.append(title);
    if(bookmark.description) content.append(el('p',bookmark.description,'bookmark-description'));
    content.append(el('div',bookmark.url,'bookmark-url'));
    const actions=el('div',undefined,'bookmark-editor-actions');
    const up=button('↑',()=>moveBookmark(index,-1),'icon'); up.disabled=!connected || index===0;
    up.title=t('moveUp'); up.setAttribute('aria-label',t('moveUp'));
    const down=button('↓',()=>moveBookmark(index,1),'icon'); down.disabled=!connected || index===bookmarkDraft.length-1;
    down.title=t('moveDown'); down.setAttribute('aria-label',t('moveDown'));
    const editButton=button(t('edit'),()=>openBookmarkModal(index),'edit');
    const removeButton=button(t('remove'),()=>removeBookmark(index),'edit');
    actions.append(up,down,editButton,removeButton); row.append(content,actions); list.append(row);
  });
}
function moveBookmark(index,delta) {
  const target=index+delta;
  if(target<0 || target>=bookmarkDraft.length) return;
  [bookmarkDraft[index],bookmarkDraft[target]]=[bookmarkDraft[target],bookmarkDraft[index]];
  renderBookmarkDraft();
}
function removeBookmark(index) { bookmarkDraft.splice(index,1); renderBookmarkDraft(); }
function openBookmarkModal(index=null) {
  bookmarkEditIndex=index;
  const bookmark=index===null?{name:'',url:'',description:'',initial:false}:bookmarkDraft[index];
  $('#bookmark-modal-title').textContent=t(index===null?'newBookmark':'editBookmark');
  const fields=$('#bookmark-form').elements;
  fields.name.value=bookmark.name; fields.url.value=bookmark.url;
  fields.description.value=bookmark.description; fields.initial.checked=bookmark.initial;
  showDialog($('#bookmark-modal')); fields.name.focus();
}
function closeBookmarkModal() { $('#bookmark-modal').close(); }
function edit(config=null, locked=false) {
  editing=Boolean(config); form.reset();
  form.elements.name.readOnly=editing;
  form.elements.host.readOnly=locked; form.elements.executable.readOnly=locked; form.elements.restore.disabled=locked;
  bookmarkDraft=config?config.bookmarks.map(bookmark=>({...bookmark})):[];
  renderBookmarkDraft();
  if(config) {
    form.elements.name.value=config.name; form.elements.host.value=config.ssh.host;
    form.elements.restore.checked=config.browser.restore_session;
    form.elements.executable.value=config.browser.executable;
  }
  $('#editor-title').textContent=t(editing?'editTitle':'newTitle');
  $('#save-workspace').textContent=t(editing?'save':'register');
  $('#editor').hidden=false; (editing?$('#add-bookmark'):form.elements.name).focus();
}
function render() {
  const busyNames=new Set(state.jobs.filter(j=>['queued','running'].includes(j.state)).map(j=>j.workspace));
  const globalBusy=busyNames.has('*');
  const key=JSON.stringify([state.workspaces,state.jobs,connected,DevHardI18n.language]); if(key===signature) return; signature=key;
  const list=$('#workspaces'); list.replaceChildren(); $('#workspace-count').textContent=state.workspaces.length;
  if(!state.workspaces.length) list.append(el('div',t('empty'),'empty'));
  for(const workspace of state.workspaces) {
    const name=workspace.name, busy=busyNames.has(name) || globalBusy, card=el('article',undefined,'card'); card.dataset.workspace=name;
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
    row.append(badge('',job.state),el('span',`${job.workspace==='*'?'':job.workspace+' · '}${t(job.action)}`,'job-description'),el('time',DevHardI18n.time(job.accepted_at)));
    if(job.error) row.append(el('div',job.error.message,'job-error')); jobs.append(row);
  }
  if(!state.jobs.length) jobs.append(el('li',t('noJobs'),'muted'));
  $('#manager-info').textContent=`${state.manager.api_url}\n${t('data')}: ${state.manager.data_root}\nPID: ${state.manager.pid}`;
}
function updateControls() {
  const globalBusy=Boolean(state?.jobs?.some(j=>j.workspace==='*' && ['queued','running'].includes(j.state)));
  $('#new-workspace').disabled=!connected;
  $('#save-workspace').disabled=!connected || submitting;
  $('#stop-all-workspaces').disabled=!connected || !state?.workspaces?.length || globalBusy || stoppingAll;
}
function receiveState(value) {
  state=value; connected=true;
  if(connectionLost) { $('#notice').hidden=true; currentNotice=null; }
  connectionLost=false;
  $('#connection').textContent=t('online'); $('#connection').className='online';
  $('#observed').textContent=state.observed_at?t('observed',{time:DevHardI18n.time(state.observed_at)}):'';
  if(state.observation_error) notice('observationFailed',{message:state.observation_error},true);
  render(); updateControls();
}
function disconnected() {
  connected=false; connectionLost=true;
  $('#connection').textContent=t('offline'); $('#connection').className='offline';
  notice('connectionFailed',{},true);
  if(state) render();
  updateControls();
}
async function watchState() {
  let retry=1000;
  while(true) {
    const controller=new AbortController();
    let idle, reader, unauthorized=false;
    const touch=()=>{ clearTimeout(idle); idle=setTimeout(()=>controller.abort(),35000); };
    try {
      touch();
      const response=await fetch('/api/events',{headers:{Authorization:`Bearer ${token}`},signal:controller.signal});
      unauthorized=response.status===401 || response.status===403;
      if(!response.ok || !response.headers.get('content-type')?.startsWith('text/event-stream')) throw new Error('State stream unavailable');
      reader=response.body.getReader();
      const decoder=new TextDecoder(); let buffer='';
      while(true) {
        const {done,value}=await reader.read();
        if(done) throw new Error('State stream closed');
        touch();
        buffer=(buffer+decoder.decode(value,{stream:true})).replace(/\r\n/g,'\n');
        let boundary;
        while((boundary=buffer.indexOf('\n\n'))!==-1) {
          const frame=buffer.slice(0,boundary); buffer=buffer.slice(boundary+2);
          const lines=frame.split('\n');
          if(!lines.includes('event: state')) continue; // Includes heartbeat comments.
          const data=lines.filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trimStart()).join('\n');
          receiveState(JSON.parse(data)); retry=1000;
        }
      }
    } catch(error) {
      disconnected();
    } finally {
      clearTimeout(idle); controller.abort();
      if(reader) { try { await reader.cancel(); } catch {} reader.releaseLock(); }
    }
    // A restarted Manager has a new token; only a page refresh can bootstrap it.
    if(unauthorized) return;
    await new Promise(resolve=>setTimeout(resolve,retry)); retry=Math.min(retry*2,10000);
  }
}
$('#new-workspace').addEventListener('click',()=>edit());
$('#stop-all-workspaces').addEventListener('click',stopAllWorkspaces);
$('#confirm-cancel').addEventListener('click',closeConfirm);
$('#confirm-ok').addEventListener('click',()=>{ const action=currentConfirm?.onConfirm; closeConfirm(); action?.(); });
dismissableDialog($('#confirm'));
$('#confirm').addEventListener('close',()=>{ currentConfirm=null; });
$('#add-bookmark').addEventListener('click',()=>openBookmarkModal());
$('#bookmark-cancel').addEventListener('click',closeBookmarkModal);
dismissableDialog($('#bookmark-modal'));
$('#bookmark-modal').addEventListener('close',()=>{ bookmarkEditIndex=null; });
$('#bookmark-form').addEventListener('submit',event=>{
  event.preventDefault();
  const fields=event.target.elements;
  const bookmark={name:fields.name.value.trim(),url:fields.url.value.trim(),description:fields.description.value.trim(),initial:fields.initial.checked};
  if(bookmarkEditIndex===null) bookmarkDraft.push(bookmark); else bookmarkDraft[bookmarkEditIndex]=bookmark;
  closeBookmarkModal(); renderBookmarkDraft();
});
$('#cancel-edit').addEventListener('click',()=>{$('#editor').hidden=true;});
form.addEventListener('submit',async(event)=>{
  event.preventDefault(); if(submitting) return; submitting=true; $('#save-workspace').disabled=true;
  const name=form.elements.name.value;
  const config={schema_version:1,name,ssh:{host:form.elements.host.value.trim()},bookmarks:bookmarkDraft.map(bookmark=>({...bookmark})),browser:{profile:'chrome-profile',restore_session:form.elements.restore.checked,executable:form.elements.executable.value.trim() || 'auto'}};
  try {
    await api(editing?`/api/workspaces/${encodeURIComponent(name)}`:'/api/workspaces',editing?'PUT':'POST',config);
    $('#editor').hidden=true; notice('accepted',{name,action:editing?'update':'register'}); updateControls();
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
  renderConfirm();
  renderTheme();
  if(!$('#editor').hidden) renderBookmarkDraft();
  if($('#bookmark-modal').open) $('#bookmark-modal-title').textContent=t(bookmarkEditIndex===null?'newBookmark':'editBookmark');
  if(state) render();
}
$('#language').addEventListener('change',event=>{ DevHardI18n.select(event.target.value); translate(); });
let dropdownSeq=0;
function createDropdown(config) {
  const trigger=config.trigger, popup=config.popup;
  popup.id=popup.id || 'dropdown-'+(++dropdownSeq);
  const options=()=>[...popup.querySelectorAll('[role="option"]')];
  trigger.setAttribute('aria-haspopup','listbox');
  trigger.setAttribute('aria-controls',popup.id);
  trigger.setAttribute('aria-expanded','false');
  let typeBuffer='', typeTimer=null;
  function close(focusTrigger) {
    if(popup.hidden) return;
    popup.hidden=true; trigger.setAttribute('aria-expanded','false');
    if(focusTrigger) trigger.focus();
  }
  function focusIndex(index) {
    const list=options(); if(!list.length) return;
    list[Math.max(0,Math.min(index,list.length-1))].focus();
  }
  function open() {
    const current=String(config.value());
    popup.hidden=false; trigger.setAttribute('aria-expanded','true');
    focusIndex(options().findIndex(option=>option.dataset.value===current));
  }
  function select(option) { if(option) { config.onSelect(option.dataset.value); close(true); } }
  function render() {
    const current=String(config.value());
    if(config.name) popup.setAttribute('aria-label',config.name());
    popup.replaceChildren();
    let label='';
    for(const item of config.items()) {
      const option=el('div',undefined,'menu-option'), selected=String(item.value)===current;
      option.setAttribute('role','option'); option.tabIndex=-1;
      option.dataset.value=item.value; option.setAttribute('aria-selected',String(selected));
      const check=el('span','✓','menu-check'); check.setAttribute('aria-hidden','true');
      option.append(check,el('span',item.label));
      option.addEventListener('click',()=>select(option));
      popup.append(option);
      if(selected) label=item.label;
    }
    if(config.showLabel) trigger.replaceChildren(el('span',label));
  }
  trigger.addEventListener('click',()=>{ if(popup.hidden) open(); else close(true); });
  trigger.addEventListener('keydown',event=>{
    if(event.key==='ArrowDown'||event.key==='ArrowUp') { event.preventDefault(); if(popup.hidden) open(); }
    else if(event.key==='Escape') close(true);
  });
  popup.addEventListener('keydown',event=>{
    const list=options(), index=list.indexOf(document.activeElement);
    if(event.key==='ArrowDown') { event.preventDefault(); focusIndex(index+1); }
    else if(event.key==='ArrowUp') { event.preventDefault(); focusIndex(index-1); }
    else if(event.key==='Home') { event.preventDefault(); focusIndex(0); }
    else if(event.key==='End') { event.preventDefault(); focusIndex(list.length-1); }
    else if(event.key==='Enter'||event.key===' ') { event.preventDefault(); select(list[index]); }
    else if(event.key==='Escape') { event.preventDefault(); close(true); }
    else if(event.key==='Tab') close(false);
    else if(event.key.length===1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
      typeBuffer+=event.key.toLowerCase();
      clearTimeout(typeTimer); typeTimer=setTimeout(()=>{ typeBuffer=''; },500);
      for(let step=1; step<=list.length; step++) {
        const candidate=list[(index+step)%list.length];
        if(candidate.lastElementChild.textContent.trim().toLowerCase().startsWith(typeBuffer)) { focusIndex((index+step)%list.length); break; }
      }
    }
  });
  document.addEventListener('click',event=>{
    if(!popup.hidden && !popup.contains(event.target) && !trigger.contains(event.target)) close(false);
  });
  render();
  return {render, close:()=>close(false)};
}
const themeToggle=$('#theme-toggle'), themeMenuButton=$('#theme-menu-button');
const modeDropdown=createDropdown({
  trigger:themeMenuButton, popup:$('#theme-menu'), showLabel:false,
  name:()=>t('themeOptions'),
  items:()=>[['system','themeSystem'],['light','themeLight'],['dark','themeDark']].map(([value,key])=>({value,label:t(key)})),
  value:()=>DevHardTheme.readMode(),
  onSelect:value=>{ DevHardTheme.setMode(value); renderTheme(); }
});
const schemeDropdown=createDropdown({
  trigger:$('#scheme-trigger'), popup:$('#scheme-menu'), showLabel:true,
  name:()=>t('colorTheme'),
  items:()=>DevHardTheme.schemesFor(DevHardTheme.effectiveAppearance()).map(scheme=>({value:scheme.id,label:scheme.label})),
  value:()=>DevHardTheme.readScheme(DevHardTheme.effectiveAppearance()),
  onSelect:value=>{ DevHardTheme.setScheme(DevHardTheme.effectiveAppearance(),value); renderTheme(); }
});
const styleDropdown=createDropdown({
  trigger:$('#style-trigger'), popup:$('#style-menu'), showLabel:true,
  name:()=>t('styleLabel'),
  items:()=>[['round','styleRound'],['flat','styleFlat'],['terminal','styleTerminal']].map(([value,key])=>({value,label:t(key)})),
  value:()=>DevHardTheme.readStyle(),
  onSelect:value=>{ DevHardTheme.setStyle(value); renderTheme(); }
});
function renderTheme() {
  const mode=DevHardTheme.readMode(), appearance=DevHardTheme.effectiveAppearance();
  themeToggle.textContent=mode==='system'?'◐':(appearance==='dark'?'☾':'☀︎');
  themeToggle.title=themeToggle.ariaLabel=t(appearance==='dark'?'switchToLight':'switchToDark');
  themeMenuButton.title=themeMenuButton.ariaLabel=t('themeOptions');
  modeDropdown.render(); schemeDropdown.render(); styleDropdown.render();
}
renderTheme();
themeToggle.addEventListener('click',()=>{ DevHardTheme.setMode(DevHardTheme.effectiveAppearance()==='dark'?'light':'dark'); renderTheme(); });
window.addEventListener('devhard:appearance',renderTheme);
window.addEventListener('storage',event=>{
  if(event.key===DevHardTheme.modeKey || event.key===DevHardTheme.styleKey || event.key===DevHardTheme.schemeKey('light') || event.key===DevHardTheme.schemeKey('dark') || event.key===null) { DevHardTheme.apply(); DevHardTheme.applyStyle(); renderTheme(); }
  if(event.key===DevHardI18n.storageKey || event.key===null) { DevHardI18n.select(event.newValue,false); translate(); }
});
translate(); watchState();
