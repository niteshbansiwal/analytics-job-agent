/* Read-only workspace refreshes. Sending remains exclusively on existing endpoints. */
Object.assign(state, {page:'outbox', expanded:new Set(), detailCache:{}, logExpanded:new Set(),
  timelines:{}, logPage:1, logsSearch:'', logsJobId:'', logsBatch:'', logsStart:'', logsEnd:'', logsEvent:'', outboxSearch:'', viewToken:0});
const workspaceViews=new Map();
let workspaceContextAt=0;
let workspaceLoadedAt=0, workspacePollBusy=false, workspaceSearchTimer=null;
const oldRender=render;
render=function(){
  const position=window.scrollY;
  const opened=[...document.querySelectorAll('#main-view details[open]')].map(x=>x.dataset.key).filter(Boolean);
  oldRender();
  for(const key of opened){const el=document.querySelector('#main-view details[data-key="'+CSS.escape(key)+'"]');if(el)el.open=true;}
  window.scrollTo(0,position);
};

async function load({quiet=false,navigation=false}={}){
  if(state.refreshBusy)return;
  if(quiet&&($('form-dialog').open||document.activeElement?.matches('input,textarea,[contenteditable="true"]')))return;
  state.refreshBusy=true;
  const controller=new AbortController();state.readController=controller;
  const read=path=>api(path,{signal:controller.signal});
  const page=state.page, token=++state.viewToken;
  try{
    const params=new URLSearchParams({section:page,page:String(state.jobPage),search:state.search,status:state.filter});
    const extra=page==='outbox'?read('/api/workspace/outreach?'+new URLSearchParams({page:String(state.dashboardPage),search:state.outboxSearch,status:state.dashboardFilter})):
      page==='emails'?read('/api/email/templates'):page==='logs'?read('/api/workspace/logs?'+new URLSearchParams({page:String(state.logPage),search:state.logsSearch,batch:state.logsBatch,start:state.logsStart,end:state.logsEnd,event_type:state.logsEvent,job_id:state.logsJobId})):Promise.resolve(null);
    const reuse=navigation&&state.data&&Date.now()-workspaceContextAt<30000&&!['jobs','overview','activity'].includes(page);
    const context=reuse?Promise.resolve({...state.data,jobs:[],outreach:[],events:[]}):read('/api/overview?'+params);
    const [data,list]=await Promise.all([context,extra]);
    if(!reuse)workspaceContextAt=Date.now();
    if(token!==state.viewToken||page!==state.page)return;
    state.data=data;
    state.detailCache={};
    if(page==='outbox')await Promise.all(list.items.filter(r=>state.expanded.has(r.id)).map(async r=>{state.detailCache[r.id]=await api('/api/workspace/outreach/'+r.id)}));
    if(page==='logs')await Promise.all(list.items.filter(r=>state.logExpanded.has(r.id)).map(async r=>{state.timelines[r.id]=await api('/api/workspace/jobs/'+r.id+'/timeline')}));
    if(page==='outbox'){state.dashboard=list;state.dashboardPage=list.page;data.outreach=list.items;}
    if(page==='emails')state.emailControls=list;
    if(page==='logs'){state.logs=list;state.logPage=list.page;}
    if(page==='jobs'||page==='overview'){
      state.jobPage=data.jobs_page.page;
      if(!data.jobs.some(j=>j.id===state.selected))state.selected=data.jobs[0]?.id||'';

    }
    if(token!==state.viewToken||page!==state.page)return;
    workspaceLoadedAt=Date.now();
    workspaceViews.set(workspaceViewKey(),{at:workspaceLoadedAt,data:state.data,dashboard:state.dashboard,logs:state.logs,emailControls:state.emailControls,selected:state.selected});
    render();
  }catch(err){if(err.name==='AbortError')return;if(err.message.includes('Sign in'))showLogin();else if(!quiet)toast(err.message,true);}
  finally{state.refreshBusy=false;}
}
function workspaceViewKey(){return JSON.stringify([state.page,state.jobPage,state.search,state.filter,state.dashboardPage,state.dashboardFilter,state.outboxSearch,state.logPage,state.logsSearch,state.logsBatch,state.logsJobId,state.logsStart,state.logsEnd,state.logsEvent]);}
async function setPage(page){state.readController?.abort();state.page=page;state.viewToken++;$('app-shell').classList.remove('open');
  const cached=workspaceViews.get(workspaceViewKey());
  if(cached){Object.assign(state,{data:cached.data,dashboard:cached.dashboard,logs:cached.logs,emailControls:cached.emailControls});if(['jobs','overview'].includes(page)&&!cached.data.jobs.some(j=>j.id===state.selected))state.selected=cached.selected;render();}
  else $('main-view').innerHTML='<div class="panel empty" role="status">Loading '+escapeHtml(page==='logs'?'job logs':page)+'…</div>';
  while(state.refreshBusy)await new Promise(r=>setTimeout(r,50));
  await load({navigation:true});}

function metricCards(){const t=state.data.totals||{};return [['Imported jobs',t.jobs],['Jobs approved',t.approved],['Outreach in pipeline',t.pipeline],['Replies received',t.replied]].map(([label,n])=>`<div class="metric"><div class="metric-top">${label}</div><div class="metric-number">${n||0}</div></div>`).join('');}
function jobPanel(){const d=state.data,meta=d.jobs_page;return `<section class="panel"><div class="panel-head"><div><h2>Imported jobs</h2><small>Excel workflow · newest uploads first</small></div><button class="link-button" data-action="show-progress">Last upload progress</button></div><div class="filters">${[['all','All'],['new','New'],['approved','Approved'],['replies','Replies']].map(([k,t])=>`<button class="filter ${state.filter===k?'active':''}" data-filter="${k}">${t}</button>`).join('')}<input class="search-field" id="job-search" value="${escapeHtml(state.search)}" placeholder="Search roles" aria-label="Search roles"></div><div class="job-list">${d.jobs.map(jobCard).join('')||'<div class="empty">No matching imported jobs.</div>'}</div>${pager(meta.page,meta.total,meta.page_size,'jobs')}</section>`;}
function pager(page,total,size,prefix){const pages=Math.max(1,Math.ceil(total/size));return `<div class="workspace-pagination"><span>Page ${page} of ${pages} · ${total} records</span><button class="btn btn-small btn-ghost" data-action="${prefix}-prev" ${page<=1?'disabled':''}>Previous</button><button class="btn btn-small btn-ghost" data-action="${prefix}-next" ${page>=pages?'disabled':''}>Next</button></div>`;}
function outboxPanel(){const d=state.dashboard;if(!d)return '<div class="empty">Loading outreach…</div>';const t=d.summary;
  const filters=[['all','All'],['processing','Processing'],['review','Needs review'],['scheduled','Scheduled'],['awaiting','Awaiting reply'],['replied','Replied'],['attention','Needs attention']];
  return `<div class="section-top"><div><h2>Your outreach</h2><p>Review emails, see the next action, and track replies.</p></div><button class="btn btn-ghost" data-action="refresh">Refresh</button></div>
  <div class="workspace-counts">${[['all','Sent',t.sent],['replied','Replies',t.replied],['unread','Unread replies',t.unread],['pending','My action pending',t.action_pending]].map(([key,label,n])=>`<button class="metric" data-dashboard-filter="${key}"><strong>${n}</strong><span>${label}</span></button>`).join('')}</div>
  <details class="panel health-panel" data-key="health"><summary>System health · ${state.data.settings.scheduler_last_tick?'Worker heartbeat '+fmt(state.data.settings.scheduler_last_tick):'No heartbeat recorded'}</summary>${schedulerStatusPanel()}</details>
  ${t.action_pending?`<div class="notice"><strong>${t.action_pending} replies need your attention.</strong><button class="btn btn-small" data-dashboard-filter="pending">Review replies</button></div>`:''}
  <div class="panel"><div class="filters">${filters.map(([k,label])=>`<button class="filter ${state.dashboardFilter===k?'active':''}" data-dashboard-filter="${k}">${label}</button>`).join('')}<input id="outreach-search" class="search-field" aria-label="Search outreach" placeholder="Company, role or contact" value="${escapeHtml(state.outboxSearch)}"></div><div class="table-scroll"><table class="outreach-table"><thead><tr><th>Company / role</th><th>Contact</th><th>Status</th><th>Next action</th><th></th></tr></thead><tbody>${d.items.map(row=>`<tr><td><strong>${escapeHtml(row.company)}</strong>${escapeHtml(row.title)}</td><td>${escapeHtml(row.contact_name||'Contact unavailable')}<small>${escapeHtml(row.to_email||'Email pending')}</small></td><td><span class="status ${sClass(row.status)}">${escapeHtml(row.status.replaceAll('_',' '))}</span>${row.sent_at?`<small>Sent ${fmt(row.sent_at)}</small>`:''}</td><td>${nextAction(row)}</td><td><button class="btn btn-small btn-ghost" data-action="expand-outreach" data-id="${row.id}" aria-expanded="${state.expanded.has(row.id)}">${state.expanded.has(row.id)?'Close':'Review'}</button></td></tr>${state.expanded.has(row.id)?`<tr><td colspan="5">${outreachDetail(row)}</td></tr>`:''}`).join('')||'<tr><td colspan="5" class="empty">No outreach in this view.</td></tr>'}</tbody></table></div>${pager(d.page,d.total,d.page_size,'dashboard')}<div class="workspace-updated">Updated ${new Date(workspaceLoadedAt).toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata'})} IST · data check every 15 seconds · sending follows Vercel schedule</div></div>`;
}
function nextAction(row){if(row.last_reply_at)return `Reply detected ${fmt(row.last_reply_at)}<br><button class="btn btn-small" data-action="reply-read" data-id="${row.id}" data-value="${!row.reply_read}">${row.reply_read?'Mark unread':'Mark read'}</button> <button class="btn btn-small" data-action="reply-action" data-id="${row.id}" data-value="${!row.action_done}">${row.action_done?'Reopen action':'Mark done'}</button>`;
  if(row.due_at){const late=Date.now()-Date.parse(row.due_at)>((state.data.settings.scheduler_cadence_minutes||15)+5)*60000;return `${late?'⚠ Overdue · ':''}${fmt(row.due_at)}`;}
  return {pending_contact:'Contact enrichment pending',needs_attention:'Review failed step',draft_ready:'Review and schedule',awaiting_reply:'Awaiting reply',cancelled:'Cancelled'}[row.status]||'—';}
function outreachDetail(row){const x=state.detailCache[row.id];if(!x)return '<p role="status">Loading email and evidence…</p>';const j=x.job,o=x.outreach;
 return `<div class="outreach-detail"><div><h3>${escapeHtml(o.subject||'Draft not created')}</h3><pre class="email-body">${escapeHtml(o.body||'No saved email body.')}</pre><div class="two-buttons">${o.gmail_draft_id?`<a class="btn btn-small btn-ghost" href="https://mail.google.com/mail/u/0/#drafts" target="_blank" rel="noreferrer">Edit Gmail draft ↗</a>`:''}${['draft_ready','scheduled'].includes(o.status)&&o.gmail_draft_id?`<button class="btn btn-small btn-dark" data-action="schedule" data-id="${o.id}">${o.status==='scheduled'?'Edit schedule':'Schedule'}</button>${o.status==='scheduled'?`<button class="btn btn-small btn-ghost" data-action="unschedule" data-id="${o.id}">Unschedule</button>`:''}`:''}${['needs_attention','draft_ready','scheduled','pending_contact','followup_scheduled'].includes(o.status)?`<button class="btn btn-small btn-red" data-action="cancel" data-id="${o.id}">Cancel outreach</button>`:''}<a class="btn btn-small btn-ghost" href="/api/outreach/${o.id}/resume.pdf">Resume: ${escapeHtml(o.resume_name||'Preview')}</a></div>${o.error?`<p class="log-error">${escapeHtml(o.error)}</p>`:''}</div><div><a href="${safeUrl(j.post_url)}" target="_blank" rel="noreferrer">Original posting ↗</a><p>${escapeHtml(j.fit_why)}</p><details data-key="jd-${j.id}"><summary>Imported job description</summary><pre class="email-body">${escapeHtml(j.jd||'Not recorded')}</pre></details><p>${escapeHtml(j.verification)} · Uploaded record ${fmt(j.created_at)}</p><button class="btn btn-small btn-ghost" data-action="view-job-log" data-id="${j.id}">Open job timeline</button></div></div>`;
}

function logsPanel(){const d=state.logs;if(!d)return '<div class="empty">Loading job logs…</div>';const time=v=>v?fmt(v):'<span class="muted">Not recorded</span>';
 return `<div class="section-top"><div><h2>Job logs</h2><p>Read-only history · ${state.logsJobId?'Selected job · apply filters to view all jobs · ':''}each contact and processing step · all times IST</p></div><button class="btn btn-ghost" data-action="refresh">Refresh</button></div>
 <form id="logs-filter-form" class="panel log-filters"><label>Search company, role or contact<input class="form-input" name="search" value="${escapeHtml(state.logsSearch)}"></label><label>Upload batch ID<input class="form-input" name="batch" maxlength="32" value="${escapeHtml(state.logsBatch)}"></label><label>From (IST)<input class="form-input" name="start" type="date" value="${state.logsStart}"></label><label>Through (IST)<input class="form-input" name="end" type="date" value="${state.logsEnd}"></label><label>Event<select class="form-input" name="event_type">${[['','All events'],['uploaded_at','Upload'],['enriched_at','Enrichment'],['drafted_at','Draft created'],['first_sent_at','Email sent'],['reply_detected_at','Reply detected']].map(([v,l])=>`<option value="${v}" ${v===state.logsEvent?'selected':''}>${l}</option>`).join('')}</select></label><button class="btn btn-dark">Apply filters</button></form>
 <div class="panel table-scroll"><table class="outreach-table"><thead><tr><th>Job</th><th>Uploaded</th><th>First contact email available</th><th>First draft created</th><th>First email sent</th><th>First reply detected</th><th></th></tr></thead><tbody>${d.items.map(j=>`<tr><td><strong>${escapeHtml(j.company)}</strong>${escapeHtml(j.title)}</td><td>${time(j.uploaded_at)}</td><td>${time(j.enriched_at)}</td><td>${time(j.drafted_at)}</td><td>${time(j.first_sent_at)}</td><td>${time(j.reply_detected_at)}</td><td><button class="btn btn-small btn-ghost" data-action="expand-log" data-id="${j.id}" aria-expanded="${state.logExpanded.has(j.id)}">Timeline</button></td></tr>${state.logExpanded.has(j.id)?`<tr><td colspan="7">${timelinePanel(j.id)}</td></tr>`:''}`).join('')||'<tr><td colspan="7" class="empty">No matching jobs.</td></tr>'}</tbody></table>${pager(d.page,d.total,d.page_size,'logs')}<p class="hint" style="padding:16px">Missing historical milestones are not inferred from record creation. Reply receipt time, when supplied by Gmail, is shown separately from detection time inside the timeline.</p></div>`;
}
function timelinePanel(id){const d=state.timelines[id];if(!d)return '<p role="status">Loading timeline…</p>';return `<ol class="job-timeline">${d.items.map(e=>`<li><strong>${escapeHtml(e.action.replaceAll('_',' '))}</strong> · ${fmt(e.occurred_at)}${e.contact_name?`<span class="timeline-contact">${escapeHtml(e.contact_name)}</span>`:''}${e.details.received_at?`<p>Received by Gmail: ${fmt(e.details.received_at)} · Detected by Atlas: ${fmt(e.details.detected_at||e.at)}</p>`:''}<p>${escapeHtml(Object.entries(e.details).filter(([k])=>!['received_at','detected_at'].includes(k)).map(([k,v])=>k.replaceAll('_',' ')+': '+String(v)).join(' · '))}</p></li>`).join('')||'<li>No historical events recorded.</li>'}</ol>${d.page*d.page_size<d.total?`<button class="btn btn-small" data-action="timeline-more" data-id="${id}">Load more events</button>`:''}`;}

document.addEventListener('click',async e=>{
 const b=e.target.closest('[data-action]');if(!b)return;const id=b.dataset.id,a=b.dataset.action;
 if(a==='expand-outreach'){if(state.expanded.has(id)){state.expanded.delete(id);render();return;}state.expanded.add(id);render();try{state.detailCache[id]=await api('/api/workspace/outreach/'+id);render();}catch(err){state.expanded.delete(id);toast(err.message,true);render();}}
 if(a==='expand-log'||a==='timeline-more'){if(a==='expand-log'&&state.logExpanded.has(id)){state.logExpanded.delete(id);render();return;}state.logExpanded.add(id);render();try{const previous=state.timelines[id];const d=await api('/api/workspace/jobs/'+id+'/timeline?page='+(a==='timeline-more'?(previous?.page||1)+1:1));if(a==='timeline-more'&&previous)d.items=[...previous.items,...d.items];state.timelines[id]=d;render();}catch(err){toast(err.message,true);}}
 if(a==='view-job-log'){state.logsSearch='';state.logsBatch='';state.logsStart='';state.logsEnd='';state.logsEvent='';state.logsJobId=id;state.logPage=1;state.logExpanded.add(id);await setPage('logs');state.timelines[id]=await api('/api/workspace/jobs/'+id+'/timeline');render();}
 if(a==='logs-prev'||a==='logs-next'){state.logPage=Math.max(1,state.logPage+(a==='logs-next'?1:-1));await load();}
});
document.addEventListener('submit',async e=>{if(e.target.id!=='logs-filter-form')return;e.preventDefault();const d=Object.fromEntries(new FormData(e.target));Object.assign(state,{logsJobId:'',logsSearch:d.search,logsBatch:d.batch,logsStart:d.start,logsEnd:d.end,logsEvent:d.event_type,logPage:1});await load();});
document.addEventListener('input',e=>{if(!['outreach-search','job-search'].includes(e.target.id))return;const id=e.target.id,v=e.target.value;clearTimeout(workspaceSearchTimer);workspaceSearchTimer=setTimeout(async()=>{if(id==='outreach-search'){state.outboxSearch=v;state.dashboardPage=1;}else{state.search=v;state.jobPage=1;}await load();const el=$(id);if(el){el.focus();el.setSelectionRange(v.length,v.length);}},350);});
async function pollWorkspace(){if(document.hidden||workspacePollBusy||state.refreshBusy||$('form-dialog').open||document.activeElement?.matches('input,textarea,select,[contenteditable="true"]'))return;workspacePollBusy=true;try{const change=await api('/api/workspace/changes');if(change.revision!==state.data?.revision)await load({quiet:true});}catch{}finally{workspacePollBusy=false;}}
setInterval(pollWorkspace,15000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)load({quiet:true});});
load();
