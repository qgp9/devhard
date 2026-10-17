'use strict';
const $ = (selector) => document.querySelector(selector);
const token = $('meta[name="devhard-token"]').content;
const form = $('#workspace-form');
let editing = false, connected = false, submitting = false, connectionLost = false, state = null, signature = '';
const labels = {stopped:'종료',exited:'종료됨',running:'실행 중',listening:'연결됨',unknown:'확인 불가',error:'오류',queued:'대기 중',succeeded:'완료',partial:'부분 성공',failed:'실패',cancelled:'취소'};
const actionLabels = {register:'등록',update:'설정 저장','workspace.start_all':'전체 시작','workspace.stop_all':'전체 종료','tunnel.start':'SSH 시작','tunnel.stop':'SSH 종료','tunnel.restart':'SSH 재시작','browser.start':'Chrome 시작','browser.stop':'Chrome 종료','browser.restart':'Chrome 재시작'};
function el(tag, text, cls) { const node = document.createElement(tag); if(text !== undefined) node.textContent = text; if(cls) node.className = cls; return node; }
function notice(message, failure=false) { const node=$('#notice'); node.hidden=false; node.textContent=message; node.className=failure?'failure':''; }
async function api(path, method='GET', data) {
  const response = await fetch(path,{method,headers:{Authorization:`Bearer ${token}`,...(data === undefined ? {} : {'Content-Type':'application/json'})},body:data === undefined ? undefined : JSON.stringify(data),signal:AbortSignal.timeout(10000)});
  let body; try { body=await response.json(); } catch { throw new Error(`응답을 읽을 수 없습니다 (${response.status})`); }
  if(!response.ok) throw new Error(body.detail || `요청 실패 (${response.status})`);
  return body;
}
function badge(title, value) {
  const cls=['running','listening','succeeded'].includes(value)?'good':['unknown','queued','partial'].includes(value)?'warn':['error','failed'].includes(value)?'bad':'';
  return el('span',`${title ? title+' · ' : ''}${labels[value] || value}`,`badge ${cls}`);
}
function button(text, handler, cls='') { const node=el('button',text,cls); node.type='button'; node.disabled=!connected; node.addEventListener('click',handler); return node; }
async function command(name, component, action) {
  try {
    const job=await api(`/api/workspaces/${encodeURIComponent(name)}/${component}/${action}`,'POST',{});
    notice(`${name}: ${actionLabels[job.action]} 요청을 접수했습니다.`);
    await poll();
  } catch(error) { notice(error.message,true); }
}
function edit(config=null) {
  editing=Boolean(config); form.reset();
  form.elements.name.readOnly=editing;
  if(config) {
    form.elements.name.value=config.name; form.elements.host.value=config.ssh.host;
    form.elements.tabs.value=config.tabs.join('\n'); form.elements.restore.checked=config.browser.restore_session;
    form.elements.executable.value=config.browser.executable;
  }
  $('#editor-title').textContent=editing?'워크스페이스 설정':'새 워크스페이스';
  $('#save-workspace').textContent=editing?'저장':'등록';
  $('#editor').hidden=false; (editing?form.elements.host:form.elements.name).focus();
}
function render() {
  const busyNames=new Set(state.jobs.filter(j=>['queued','running'].includes(j.state)).map(j=>j.workspace));
  const key=JSON.stringify([state.workspaces,state.jobs,connected]); if(key===signature) return; signature=key;
  const list=$('#workspaces'); list.replaceChildren(); $('#workspace-count').textContent=state.workspaces.length;
  if(!state.workspaces.length) list.append(el('div','첫 워크스페이스를 등록해보세요.\nSSH 호스트와 열고 싶은 주소만 있으면 됩니다.','empty'));
  for(const workspace of state.workspaces) {
    const name=workspace.name, busy=busyNames.has(name), card=el('article',undefined,'card'); card.dataset.workspace=name;
    const top=el('div',undefined,'card-top'), title=el('div'); title.append(el('h3',name),el('div',workspace.config.ssh.host,'host'));
    const editButton=button('설정',()=>edit(workspace.config),'edit');
    editButton.disabled=!connected || busy || Object.values(workspace.status).some(s=>!['stopped','exited','error'].includes(s));
    top.append(title,editButton); const states=el('div',undefined,'states');
    states.append(badge('SSH',workspace.status.tunnel),badge('Chrome',workspace.status.browser));
    const controls=el('div',undefined,'controls');
    controls.append(button('전체 시작',()=>command(name,'workspace','start_all'),'primary'),button('전체 종료',()=>command(name,'workspace','stop_all')));
    const components=el('div',undefined,'components');
    for(const [component,title] of [['tunnel','SSH'],['browser','Chrome']]) {
      const row=el('div',undefined,'component-row'), actions=el('div'); row.append(el('span',title));
      for(const [action,text] of [['start','시작'],['stop','종료'],['restart','재시작']]) actions.append(button(text,()=>command(name,component,action)));
      row.append(actions); components.append(row);
    }
    if(busy) { for(const b of [...controls.querySelectorAll('button'),...components.querySelectorAll('button')]) b.disabled=true; }
    card.append(top,states,controls,components);
    if(busy) card.append(el('div','요청 처리 중 · 완료되면 상태가 갱신됩니다.','busy'));
    const tabs=el('ul',undefined,'tabs'); for(const url of workspace.config.tabs) tabs.append(el('li',url)); card.append(tabs);
    const error=workspace.observation_error || workspace.runtime?.last_error?.message;
    if(error) card.append(el('div',`최근 오류: ${error}`,'error'));
    const detail=el('details'), summary=el('summary','연결 정보'); detail.append(summary);
    detail.append(el('pre',`SOCKS: ${workspace.runtime?.reserved_port ? '127.0.0.1:'+workspace.runtime.reserved_port : '미예약'}\n프로필: ${workspace.profile_path || '확인 불가'}`)); card.append(detail);
    list.append(card);
  }
  const jobs=$('#jobs'); jobs.replaceChildren();
  for(const job of state.jobs.slice(-12).reverse()) {
    const row=el('li'); row.dataset.job=job.id;
    row.append(badge('',job.state),el('span',`${job.workspace} · ${actionLabels[job.action] || job.action}`,'job-description'),el('time',new Date(job.accepted_at).toLocaleTimeString('ko-KR')));
    if(job.error) row.append(el('div',job.error.message,'job-error')); jobs.append(row);
  }
  if(!state.jobs.length) jobs.append(el('li','아직 실행한 작업이 없습니다.','muted'));
  $('#manager-info').textContent=`${state.manager.api_url}\n데이터: ${state.manager.data_root}\nPID: ${state.manager.pid}`;
}
let polling=false;
async function poll() {
  if(polling) return; polling=true;
  try {
    state=await api('/api/state'); connected=true;
    if(connectionLost) $('#notice').hidden=true;
    connectionLost=false;
    $('#connection').textContent='● Manager 연결됨'; $('#connection').className='online';
    $('#observed').textContent=state.observed_at?`${new Date(state.observed_at).toLocaleTimeString('ko-KR')} 확인`:'';
    if(state.observation_error) notice(`상태 확인 실패: ${state.observation_error}`,true);
    render();
  } catch(error) {
    connected=false; connectionLost=true; $('#connection').textContent='● 연결 끊김'; $('#connection').className='offline';
    notice('Manager에 연결할 수 없습니다. 페이지를 새로고침하거나 다시 실행한 Manager의 관리 주소를 열어주세요. 실행 중인 Chrome과 SSH는 유지됩니다.',true);
    if(state) render();
  } finally { polling=false; $('#new-workspace').disabled=!connected; $('#save-workspace').disabled=!connected || submitting; }
}
$('#new-workspace').addEventListener('click',()=>edit());
$('#cancel-edit').addEventListener('click',()=>{$('#editor').hidden=true;});
form.addEventListener('submit',async(event)=>{
  event.preventDefault(); if(submitting) return; submitting=true; $('#save-workspace').disabled=true;
  const name=form.elements.name.value;
  const config={schema_version:1,name,ssh:{host:form.elements.host.value.trim()},tabs:form.elements.tabs.value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean),browser:{profile:'chrome-profile',restore_session:form.elements.restore.checked,executable:form.elements.executable.value.trim() || 'auto'}};
  try {
    await api(editing?`/api/workspaces/${encodeURIComponent(name)}`:'/api/workspaces',editing?'PUT':'POST',config);
    $('#editor').hidden=true; notice(`${name}: ${editing?'설정 저장':'등록'} 요청을 접수했습니다.`); await poll();
  } catch(error) { notice(error.message,true); }
  finally { submitting=false; $('#save-workspace').disabled=!connected; }
});
poll(); setInterval(poll,1200);
