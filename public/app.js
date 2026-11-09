const state = { data:null, page:'overview', selected:'', filter:'all', search:'' };
let rrAutoTimer=null;
let rrBusy=false;
const $ = id => document.getElementById(id);
const escapeHtml = v => String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const safeUrl = v => {try{const u=new URL(v);return ['http:','https:'].includes(u.protocol)?escapeHtml(u.href):''}catch{return ''}};
const fmt = s => {if(!s)return 'Not scheduled';try{return new Date(s.endsWith('Z')||s.includes('+')?s:s+'Z').toLocaleString('en-IN',{dateStyle:'medium',timeStyle:'short',timeZone:'Asia/Kolkata'})+' IST'}catch{return s}};
const initials = s => String(s||'?').split(/[ \-]+/).filter(Boolean).slice(0,2).map(x=>x[0].toUpperCase()).join('');
const sClass = s => String(s||'').replace(/[^a-z_]/ig,'');
const jobOf = id => state.data?.jobs.find(j=>j.id===id);
const api = async (path,opts={}) => {const r=await fetch(path,{headers:{'Content-Type':'application/json'},...opts});const d=await r.json().catch(()=>({}));if(!r.ok)throw Error(d.detail||`Request failed (${r.status})`);return d};
function toast(message,error=false){const n=document.createElement('div');n.className='toast'+(error?' error':'');n.textContent=message;$('toasts').appendChild(n);setTimeout(()=>n.remove(),5100)}
async function load(){try{state.data=await api('/api/overview');if(!state.selected)state.selected=state.data.jobs[0]?.id||'';render()}catch(err){if(err.message.includes('Sign in')){showLogin()}else if(err.message.includes('PostgreSQL')||err.message.includes('DATABASE_URL')){showSetup()}else toast(err.message,true)}}
function showLogin(){$('page-content').innerHTML=`<section class="panel" style="margin:70px auto;max-width:520px;padding:40px;text-align:center"><div style="font-size:35px">✳</div><h1>Welcome to Atlas</h1><p class="summary-mini">A private workspace for research, approvals and Gmail outreach. Sign in with the Google account configured as the owner.</p><a class="btn btn-dark" href="/api/auth/google/start">Sign in with Google ↗</a></section>`}
function showSetup(){$('page-content').innerHTML=`<section class="panel" style="margin:70px auto;max-width:650px;padding:40px;text-align:center"><div style="font-size:35px">✳</div><h1>One more step: connect the database</h1><p class="summary-mini">Your Google sign-in worked, but Atlas needs persistent PostgreSQL storage before real job search and approvals can be saved. No data has been lost or emailed.</p><a class="btn btn-dark" href="https://vercel.com/dashboard/stores" target="_blank" rel="noopener noreferrer">Set up PostgreSQL ↗</a> &nbsp; <a class="btn btn-ghost" href="/">View setup checklist</a></section>`}
function setPage(page){state.page=page;render();window.scrollTo({top:0,behavior:'smooth'});$('app-shell').classList.remove('open')}
function render(){if(!state.data)return;const d=state.data;document.querySelectorAll('.nav-item').forEach(x=>x.classList.toggle('active',x.dataset.page===state.page));const labels={overview:'Overview',jobs:'Imported jobs',outbox:'Outreach queue',activity:'Activity log',settings:'Configuration'};$('breadcrumb-page').textContent=labels[state.page];$('nav-job-count').textContent=d.jobs.filter(j=>j.status==='discovered').length;
$('run-note').textContent=d.integrations.gemini?'Gemini application review · Sending disabled':'ChatGPT approvals → Excel → Atlas';
$('demo-banner').innerHTML=d.demo?`<div class="notice"><i>!</i><div><strong>DEMO ENVIRONMENT</strong> — All five sample companies and openings are fictional. No real jobs, contacts, messages or emails are being sent. Connect integrations for live use.</div></div>`:(!d.jobs.length)?`<div class="notice"><i>⇧</i><div><strong>READY FOR YOUR FIRST IMPORT</strong> — Review and approve jobs in ChatGPT, then upload the approved Excel/CSV file. No paid AI API is needed.</div></div>`:'';
const connections=[['Google Gmail','G',d.integrations.gmail],['Spreadsheet import','▤',true],['RocketReach','R',d.integrations.rocketreach],['Gemini API','✦',d.integrations.gemini]];
$('sidebar-connections').innerHTML=connections.map(x=>`<div class="side-connection"><span class="service-icon">${x[1]}</span><span>${x[0]}</span><span class="service-status ${x[2]?'connected':''}" title="${x[2]?'Configured':'Not configured'}"></span></div>`).join('');
$('metrics').innerHTML=metricCards();$('metrics').style.display=['overview','jobs'].includes(state.page)?'grid':'none';
const view=$('main-view');if(state.page==='overview'||state.page==='jobs'){view.innerHTML=`<div class="content-grid">${jobPanel()}${detailPanel()}</div>`}else if(state.page==='outbox'){view.innerHTML=outboxPanel()}else if(state.page==='activity'){view.innerHTML=activityPanel()}else{view.innerHTML=settingsPanel()}
 scheduleRocketReachRecheck();
}
function metricCards(){const d=state.data;const approved=d.jobs.filter(j=>['approved','reply_received'].includes(j.status)).length;const scheduled=d.outreach.filter(o=>['draft_ready','scheduled','followup_scheduled','demo_draft','awaiting_gmail','pending_contact'].includes(o.status)).length;const replies=d.outreach.filter(o=>o.last_reply_at).length;
return [
 ['Opportunities found',d.jobs.length,'Search results in workspace','▤',''],
 ['Jobs approved',approved,'Approved after your review','✓','green'],
 ['Outreach in pipeline',scheduled,'Drafted, pending or scheduled','↗','orange'],
 ['Recruiter replies',replies,'Automatically logged to tracker','✉','purple']
].map(x=>`<div class="metric"><div class="metric-top"><span>${x[0]}</span><span class="metric-icon ${x[4]}">${x[3]}</span></div><div class="metric-number">${x[1]}</div><div class="metric-trend">${x[2]}</div></div>`).join('')}
function jobsFiltered(){let jobs=state.data.jobs.filter(j=>state.filter==='all'||(state.filter==='new'&&j.status==='discovered')||(state.filter==='approved'&&j.status==='approved')||(state.filter==='replies'&&j.status==='reply_received'));
if(state.search){const q=state.search.toLowerCase();jobs=jobs.filter(j=>(j.company+' '+j.title+' '+j.location).toLowerCase().includes(q))}return jobs}
function jobPanel(){let jobs=jobsFiltered();return `<section class="panel"><div class="panel-head"><div><h2>${state.page==='overview'?'Recommended opportunities':'Job discovery'}</h2><small>Ranked by skills, leadership fit and contact accessibility</small></div><button class="link-button" data-action="import">+ Add job</button></div><div class="filters">${[['all','All roles'],['new','New'],['approved','Approved'],['replies','Replies']].map(([k,t])=>`<button class="filter ${state.filter===k?'active':''}" data-filter="${k}">${t}</button>`).join('')}<input class="search-field" id="job-search" value="${escapeHtml(state.search)}" placeholder="⌕ Search roles..." aria-label="Search roles"></div><div class="job-list">${jobs.length?jobs.map(jobCard).join(''):`<div class="empty"><div class="empty-icon">⌕</div>No matching jobs in this view.</div>`}</div></section>`}
function jobCard(j){return `<div class="job-card ${j.id===state.selected?'selected':''}" data-job="${j.id}" tabindex="0"><div class="company-icon">${escapeHtml(initials(j.company))}</div><div class="job-summary"><div class="job-row-top"><div><div class="job-title">${escapeHtml(j.title)}</div><div class="job-company">${escapeHtml(j.company)} · ${escapeHtml(j.location)}</div></div><div class="mini-score">${Number(j.fit_score).toFixed(1)} <small>/10</small></div></div><div class="job-tags"><span class="tag soft">${escapeHtml(j.arrangement)}</span><span class="tag ${j.is_demo?'demo':''}">${j.is_demo?'DEMO':escapeHtml(j.source)}</span>${j.status==='discovered'?'':`<span class="tag green">${escapeHtml(j.status.replaceAll('_',' '))}</span>`}</div><div class="job-bottom"><span>◷ ${escapeHtml(j.post_age)}</span><span><strong>${j.contacts.length}/2</strong> contacts identified &nbsp;›</span></div></div></div>`}
function contactCard(c,index){if(!c)return `<div class="contact-unknown">Contact ${index} — Not identified. No hiring relationship or email is assumed.</div>`;
const rr=c.rocketreach_status||'not_started';
const canLookup=state.data?.integrations.rocketreach&&c.linkedin_url&&!c.email;
const recheck=['pending','awaiting_email','pending_manual'].includes(rr);
const exhausted=Number(c.rocketreach_lookup_count||0)>=2;
const action=recheck?'rocketreach-check':'rocketreach';
const label=rr==='pending_manual'?'↻ Check existing lookup manually':recheck?'↻ Recheck existing lookup (no new lookup)':rr==='not_started'?'⌕ Find work email via RocketReach':exhausted?'Maximum lookups reached':'↻ Retry paid RocketReach lookup';
const lookupButton=canLookup?`<button class="btn btn-small btn-ghost" style="margin-top:9px" data-action="${action}" data-id="${c.id}" data-retry="${rr!=='not_started'}" ${exhausted&&!recheck?'disabled':''}>${label}</button>`:'';
const next=c.rocketreach_next_check_at?new Date(c.rocketreach_next_check_at).toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour:'numeric',minute:'2-digit'}):'';
const statusText=rr==='awaiting_email'?'No email yet — automatically rechecking the same lookup':rr==='pending'?'RocketReach is enriching this contact':rr==='pending_manual'?'Still processing — automatic checks paused; you can check again manually':rr==='not_found'?'No professional email after available checks; manual retry may consume another credit':rr.replaceAll('_',' ');
const lookupInfo=rr!=='not_started'&&!c.email?`<p class="hint">RocketReach: ${escapeHtml(statusText)}${next&&recheck?` · Next check around ${escapeHtml(next)} IST`:''}</p>`:'';
return `<div class="contact-box"><div class="contact-top"><div class="contact-avatar">${escapeHtml(initials(c.name))}</div><div class="contact-info"><strong>${escapeHtml(c.name)}</strong><small>${escapeHtml(c.category)} · ${escapeHtml(c.relationship)}</small></div>${c.email?'<span class="status scheduled">Email found</span>':''}</div><p>${escapeHtml(c.reason||'Connection to this opening requires verification.')}</p>${c.email?`<p><b>Email:</b> ${escapeHtml(c.email)} <span class="muted">(${escapeHtml(c.email_status)})</span></p>`:''}${c.linkedin_url?`<a class="contact-link" href="${safeUrl(c.linkedin_url)}" target="_blank" rel="noreferrer">View LinkedIn profile ↗</a>`:''}${lookupInfo}${lookupButton}</div>`}
function detailPanel(){const j=jobOf(state.selected);if(!j)return `<section class="panel"><div class="empty">Choose a job to view hiring contacts and fit.</div></section>`;
const o=j.outreach;return `<section class="panel"><div class="panel-head"><div><h2>Opportunity details</h2><small>Evidence first · No invented contacts</small></div><span class="status ${sClass(j.status)}">${escapeHtml(j.status.replaceAll('_',' '))}</span></div><div class="detail-body"><div class="detail-hero"><div class="detail-logo">${escapeHtml(initials(j.company))}</div><div class="detail-hero-main"><h3 class="detail-title">${escapeHtml(j.title)}</h3><p class="detail-company">${escapeHtml(j.company)} · ${escapeHtml(j.location)}</p><span class="status ${j.is_demo?'demo_draft':'pending_contact'}">${j.is_demo?'DEMO SAMPLE':escapeHtml(j.verification.replaceAll('_',' '))}</span></div></div><div class="detail-stat-row"><span class="detail-pill">◷ ${escapeHtml(j.post_age)}</span><span class="detail-pill">${escapeHtml(j.experience)}</span><span class="detail-pill">${escapeHtml(j.leadership)}</span></div><div class="section-title">WHY YOU MATCH</div><div class="fit-box"><div class="fit-bubble">✓</div><div><strong>Preliminary fit · ${Number(j.fit_score).toFixed(1)}/10</strong><p>${escapeHtml(j.fit_why)}</p></div></div><div class="section-title">HIRING-SIDE CONTACTS <span class="muted">(${j.contacts.length}/2)</span></div>${contactCard(j.contacts[0],1)}${contactCard(j.contacts[1],2)}<button class="action-inline" data-action="contact" data-id="${j.id}">+ Add a contact or verified professional email</button><div class="section-title">POSTING EVIDENCE</div>${j.post_url?`<a class="source-link" target="_blank" rel="noreferrer" href="${safeUrl(j.post_url)}">↗ Open original source (${escapeHtml(j.source)})</a>`:`<p class="summary-mini">No real source exists for this fictional demonstration vacancy.</p>`}<p class="summary-mini">${escapeHtml(j.jd).slice(0,280)}</p><p class="hint">Posting status: ${escapeHtml(j.verification)}. Date source: ${escapeHtml(j.posting_date_kind)}. Gaps: ${escapeHtml(j.gaps)}</p><div class="detail-actions">${!j.is_demo?(state.data?.resumes?.some(r=>r.configured)?`<button class="btn btn-ghost" data-action="gemini-preview" data-id="${j.id}">Review email + choose resume</button>`:`<button class="btn btn-ghost" data-page="settings">Upload a resume first</button>`):''}${j.status==='discovered'?`${state.data?.demo?`<button class="btn btn-green" data-action="approve" data-id="${j.id}">✓ Approve demo job</button>`:''}<button class="btn btn-red" data-action="reject" data-id="${j.id}">× Pass</button>`:o?`<button class="btn btn-dark" data-action="outreach" data-id="${o.id}">↗ View outreach</button>`:''}</div><div class="detail-note">Choose your Analytics or Data Science resume. The uploaded PDF is attached unchanged. Atlas never sends from this review flow.</div></div></section>`}
function outboxPanel(){const rows=state.data.outreach;return `<div class="section-top"><div><h2>Reviewed outreach drafts</h2><p>Review saved drafts and their original attachments. Automatic sending is disabled.</p></div><button class="btn btn-ghost" data-action="refresh">↻ Refresh</button></div><div class="panel wide-panel"><table class="outreach-table"><thead><tr><th>Opportunity</th><th>Recipient</th><th>Status</th><th>Next action</th><th>Resume / Controls</th></tr></thead><tbody>${rows.length?rows.map(o=>{const j=jobOf(o.job_id);return `<tr><td><strong>${escapeHtml(j?.company||'—')}</strong>${escapeHtml(j?.title||'')}</td><td>${escapeHtml(o.to_email||'Pending discovery')}<small style="display:block;margin-top:5px">${o.gmail_draft_id?'Gmail draft available':'No Gmail draft created'}</small></td><td><span class="status ${sClass(o.status)}">${escapeHtml(o.status.replaceAll('_',' '))}</span></td><td>${fmt(o.due_at)}<small style="display:block;margin-top:5px">Follow-ups: ${o.followup_count}</small></td><td><div class="two-buttons"><a class="btn btn-small btn-ghost" href="/api/outreach/${o.id}/resume.pdf">↓ Resume</a>${o.gmail_draft_id?`<a class="btn btn-small btn-ghost" target="_blank" href="https://mail.google.com/mail/u/0/#drafts">Open Gmail drafts ↗</a>`:''}${['draft_ready','scheduled','followup_scheduled','demo_draft','awaiting_gmail','pending_contact','needs_attention'].includes(o.status)?`<button class="btn btn-small btn-red" data-action="cancel" data-id="${o.id}">Cancel</button>`:''}${state.data.demo&&['awaiting_gmail','pending_contact','needs_attention'].includes(o.status)?`<button class="btn btn-small btn-green" data-action="prepare" data-id="${o.id}">Prepare draft</button>`:''}</div>${o.error?`<small style="display:block;color:#b96364;margin-top:7px">${escapeHtml(o.error)}</small>`:''}</td></tr>`}).join(''):`<tr><td colspan="5"><div class="empty">No outreach yet. Approve a suitable job to create one.</div></td></tr>`}</tbody></table></div><div class="settings-banner" style="margin-top:17px">Reviewed drafts can be edited in Gmail. Atlas does not send or schedule drafts created through the review workflow. Follow-ups are disabled.</div>`}
function activityPanel(){const ev=state.data.events;return `<div class="section-top"><div><h2>Activity & audit trail</h2><p>Track every approval, draft, scheduled send and reply.</p></div></div><div class="panel">${ev.length?ev.map(e=>`<div class="activity-row"><div class="activity-dot"></div><div><strong>${escapeHtml(e.action.replaceAll('_',' '))}</strong><p>${e.job_id?escapeHtml(jobOf(e.job_id)?.company||''):'System event'} · ${escapeHtml(Object.keys(e.details||{}).slice(0,2).map(k=>k+': '+String(e.details[k]).slice(0,60)).join(' · '))}</p></div><time>${fmt(e.at)}</time></div>`).join(''):`<div class="empty">No activity recorded yet.</div>`}</div>`}
function settingsPanel(){const d=state.data,i=d.integrations,s=d.settings;const row=(a,b)=>`<div class="config-row"><span>${escapeHtml(a)}</span><strong>${escapeHtml(b)}</strong></div>`;return `<div class="section-top"><div><h2>Atlas configuration</h2><p>Research and job approvals happen in ChatGPT. Atlas stores only the jobs you choose to import.</p></div></div><div class="settings-banner">Your ChatGPT daily job-search task is separate from Atlas. Open the ChatGPT task notification, approve jobs in that conversation, and upload the resulting Excel file here. Atlas does not call an AI search API or a Telegram bot.</div><div class="config-grid"><div class="panel config-panel"><h3>Approved-job import</h3>${row('Source','ChatGPT-approved Excel / CSV')}${row('Maximum import','250 rows per file')}${row('Duplicate detection','By original job URL')}${row('Original source verification','Required before outreach')}${row('Job search API','Disabled')}</div><div class="panel config-panel"><h3>Email calendar</h3>${row('Allowed days','Monday–Friday')}${row('Sending window',s.send_start+'–'+s.send_end+' IST')}${row('Preferred time',s.send_time+' IST')}${row('Scheduled sending',s.send_enabled?'Enabled':'OFF – safety lock')}${row('Follow-ups',s.followup_enabled?'Enabled':'Disabled')}${row('Follow-up interval',s.followup_hours+' elapsed hours')}${row('Maximum attempts',s.max_followups)}</div><div class="panel config-panel"><h3>Connections</h3>${row('Gmail OAuth',i.gmail?'Connected':'Not connected')}${row('Spreadsheet upload','Ready · no paid AI API')}${row('Optional RocketReach',i.rocketreach?'API key configured · lookup unverified':'Not configured')}${row('Gemini API',i.gemini?'Key configured · click Test API':'Not configured')}${row('Gemini model',i.gemini_model||'gemini-3.8-flash')}${i.gemini?'<button class="btn btn-small btn-ghost" style="margin-top:12px" data-action="gemini-test">Test Gemini API connection</button>':''}<a class="btn btn-green" href="/api/auth/gmail/start" style="margin-top:16px">Connect Gmail separately ↗</a><p class="hint">Gmail access is requested only when you explicitly connect outreach.</p></div><div class="panel config-panel"><h3>Safety & privacy</h3>${row('Execution mode',d.demo?'Local demonstration':'Live environment')}${row('Database',i.database)}${row('ChatGPT job approval','Before import')}${row('Outreach approval','Separate step in Atlas')}${row('OAuth token storage','Encrypted')}${row('Failed/ambiguous send','Held for review')}<p class="hint">Imported posting dates and contact relationships remain unverified until you check original sources.</p></div><div class="panel config-panel"><h3>Your two resumes</h3>
<p class="hint">Upload your final PDFs once. Atlas previews and attaches the selected file unchanged — no rewriting, reformatting or project selection.</p>
${(d.resumes||[]).map(r=>`<form class="fixed-resume-form" data-kind="${r.id}" style="margin-top:18px">
<strong>${escapeHtml(r.label)}</strong><p class="hint">${r.configured?escapeHtml(r.filename)+' · '+r.page_count+' page(s)':'Not uploaded'}</p>
<label class="form-label">${escapeHtml(r.label)} PDF (maximum 4 MB)</label>
<input class="form-input" type="file" name="file" accept="application/pdf,.pdf" required>
${r.configured?'<label class="hint"><input type="checkbox" name="replace" required> Replace the saved PDF for future drafts</label>':''}
<button type="submit" class="btn btn-dark" style="margin-top:10px">${r.configured?'Replace':'Save'} PDF</button></form>`).join('')}
<p class="hint">Existing Gmail drafts keep their original attachment until you explicitly replace the draft.</p></div></div>`}
function modal(html){$('form-dialog').classList.remove('review-modal');$('modal-inner').innerHTML=html;$('form-dialog').showModal()}
function closeModal(){$('form-dialog').close()}
function importDialog(){modal(`<h2>Add a job posting</h2><p>Paste a real job link and the information visible on the page. Imported records are marked unverified until investigated.</p><form id="import-form"><label class="form-label">Company *</label><input class="form-input" name="company" required maxlength="180" placeholder="Company name"><label class="form-label">Job title *</label><input class="form-input" name="title" required maxlength="220" placeholder="Analytics Manager"><label class="form-label">Original post URL *</label><input class="form-input" name="post_url" type="url" pattern="https://.*" required placeholder="https://..."><label class="form-label">Location</label><input class="form-input" name="location" placeholder="Gurugram"><label class="form-label">Job description (optional)</label><textarea class="form-input form-textarea" name="jd" maxlength="6000" placeholder="Paste job description..."></textarea><button class="btn btn-dark" type="submit">Add to pipeline ↗</button></form>`)}

function sheetDialog(){
 if(state.data?.demo){modal('<h2>Spreadsheet import preview</h2><p>The live Atlas workspace accepts Excel and CSV files, validates original job links, and saves contacts to Neon without a paid AI API. This browser-only demo contains fictional data and cannot import real jobs.</p><a class="btn btn-dark" href="/">Go to Atlas sign-in ↗</a>');return;}
 modal(`<h2>Upload approved jobs</h2>
 <p>Approve jobs in your ChatGPT daily digest, then upload the Excel (.xlsx) or CSV containing only those approved jobs. Atlas saves the links and up to two potential hiring contacts per job, and skips duplicate URLs. <strong>No OpenAI API key required.</strong></p>
 <p><a class="btn btn-ghost" href="/api/spreadsheets/template.xlsx" download>↓ Download Excel template</a></p>
 <form id="sheet-import-form"><label class="form-label">Choose completed Excel or CSV file</label>
 <input class="form-input" type="file" name="file" accept=".xlsx,.csv" required>
 <p class="hint">Maximum 2 MB or 250 rows. Accepts the Atlas Jobs template or ChatGPT's Approved Jobs sheet (Company, Role, Job Source URL). Only rows marked Approved are imported when an Approval column exists.</p>
 <button class="btn btn-dark" type="submit">Import and validate jobs ↗</button></form>
 <div style="border-top:1px solid #e5ebf2;margin:22px 0 18px"></div>
 <h3>Or paste CSV directly</h3><p>Useful if a scheduled task provides CSV text instead of an attached spreadsheet.</p>
 <form id="csv-import-form"><textarea class="form-input form-textarea" name="csv_text" rows="5" maxlength="400000" required placeholder="company,title,location,post_url,..."></textarea>
 <button class="btn btn-ghost" type="submit" style="margin-top:10px">Import pasted CSV ↗</button></form>
 <p class="hint">Imported posting dates, job details and hiring relationships are marked unverified until you check the original sources. Import never sends emails.</p>`);
}
function sheetResult(result){
 const details=(result.errors||[]).slice(0,20).map(e=>'<li>'+escapeHtml(e)+'</li>').join('');
 modal(`<h2>Import complete</h2><p><strong>${result.created} new jobs</strong> saved in Atlas. ${result.duplicates} duplicate links skipped; ${result.skipped} invalid rows skipped; ${result.contacts_created} potential hiring contacts imported.</p>
 ${details?'<p>Items to review:</p><ul style="max-height:220px;overflow:auto">'+details+'</ul>':''}
 <p class="hint">Every imported job remains unverified until you inspect its original posting. No emails were sent.</p>
 <button class="btn btn-dark" type="button" id="import-done">View imported jobs ↗</button>`);
}
async function handleSheetSubmit(form){
 const submit=form.querySelector('button[type="submit"]');
 submit.disabled=true;
 try{
  let result;
  if(form.id==='sheet-import-form'){
   const file=form.querySelector('input[type="file"]').files[0];
   if(!file)throw Error('Select a spreadsheet first.');
   if(file.size>2000000)throw Error('File must be 2 MB or smaller.');
   const response=await fetch('/api/jobs/import-spreadsheet',{method:'POST',body:new FormData(form),credentials:'same-origin'});
   result=await response.json().catch(()=>({detail:'Unexpected server response'}));
   if(!response.ok)throw Error(result.detail||'Spreadsheet upload failed.');
  }else{
   result=await api('/api/jobs/import-csv',{method:'POST',body:JSON.stringify({csv_text:form.querySelector('textarea').value})});
  }
  await load();
  sheetResult(result);
  toast(result.created+' jobs imported; '+result.duplicates+' duplicates skipped');
 }catch(err){
  const message=escapeHtml(err.message||'Unable to import this file.');
  modal(`<h2>Import validation failed</h2><p>${message}</p><p>Atlas accepts both the official <strong>Jobs</strong> template and ChatGPT's <strong>Approved Jobs</strong> export. Check that each row has a company, role and original HTTPS job link.</p><p><a class="btn btn-ghost" href="/api/spreadsheets/template.xlsx" download>↓ Download compatible Excel template</a></p><button class="btn btn-dark" type="button" id="retry-sheet-import">Try another file</button>`);
  toast('Import failed — see validation details.',true);
 }
 finally{submit.disabled=false}
}

async function promptDialog(){
 try{
  const result=await api('/api/search/prompt');
  modal('<h2>Your job-search research prompt</h2><p>Copy this research brief into your scheduled ChatGPT task. Editing it here saves a copy in Atlas but does not automatically update your ChatGPT task.</p><form id="prompt-form"><label class="form-label">Research instructions</label><textarea class="form-input form-textarea" name="prompt" rows="17" minlength="100" maxlength="24000" required style="min-height:300px;width:100%;resize:vertical">'+escapeHtml(result.prompt)+'</textarea><p class="hint">Search engine sources must be cited. No vacancy, date or contact should be invented.</p><button class="btn btn-ghost" type="button" id="copy-research-prompt">Copy prompt for scheduled ChatGPT task</button> <button class="btn btn-dark" type="submit">Save prompt copy ↗</button></form>');
 }catch(err){toast(err.message,true)}
}
function contactDialog(id){modal(`<h2>Add hiring-side contact</h2><p>Only add a person whose identity you can support with an original post or professional profile. Email is optional.</p><form id="contact-form" data-jobid="${id}"><label class="form-label">Name *</label><input class="form-input" name="name" required placeholder="Contact's verified name"><label class="form-label">Current role</label><input class="form-input" name="title" placeholder="Talent Acquisition / Director of Analytics"><label class="form-label">LinkedIn profile URL</label><input class="form-input" name="linkedin_url" type="url" placeholder="https://www.linkedin.com/in/..."><label class="form-label">Professional email (optional)</label><input class="form-input" name="email" type="email" placeholder="Only a genuinely sourced address"><label class="form-label">Evidence URL</label><input class="form-input" name="evidence_url" type="url" placeholder="Link to job announcement/profile"><button type="submit" class="btn btn-dark">Save contact</button></form>`)}
function scheduleRocketReachRecheck(){
 if(rrAutoTimer)clearTimeout(rrAutoTimer);
 if(!state.data||!['overview','jobs'].includes(state.page))return;
 const job=jobOf(state.selected);
 const contact=job?.contacts?.find(c=>!c.email&&['pending','awaiting_email'].includes(c.rocketreach_status));
 if(!contact)return;
 const due=Date.parse(contact.rocketreach_next_check_at||'')||Date.now()+15000;
 const delay=Math.min(180000,Math.max(1000,due-Date.now()+1000));
 rrAutoTimer=setTimeout(()=>{if(!rrBusy&&jobOf(state.selected)?.contacts?.some(c=>c.id===contact.id))rocketreachAction(contact.id,true,false,true)},delay);
}
async function rocketreachAction(id,check=false,retry=false,automatic=false){
 if(rrBusy)return;
 if(!check){
  const warning=retry?'Retry RocketReach lookup? This may use ANOTHER paid credit. Atlas first checks the previous lookup and enforces a cooldown.':
    'Start a RocketReach person lookup? This may use a credit. Atlas will recheck this same lookup automatically; no email will be sent.';
  if(!confirm(warning))return;
 }
 rrBusy=true;
 try{
  const endpoint=`/api/contacts/${id}/rocketreach/${check?'check':'lookup'}`;
  const result=await api(endpoint,{method:'POST',body:JSON.stringify({retry:!!retry})});
  await load();
  if(result.status==='returned')toast('Work email found and saved. Verify the hiring relationship before outreach.');
  else if(result.status==='already_found'&&!automatic)toast('Email already exists; no new lookup made.');
  else if(result.status==='not_found'&&!automatic)toast('No work email after rechecks. A paid retry may be available after the cooldown.');
  else if(result.status==='api_error')toast('RocketReach: '+(result.detail||'API error'),true);
  else if(!automatic&&!check)toast('Lookup started. Atlas will recheck this same RocketReach request automatically.');
  else if(!automatic&&check)toast(result.detail||'RocketReach status checked.');
 }catch(e){if(!automatic)toast(e.message,true)}
 finally{rrBusy=false;}
}

async function geminiTest(){
 try{
  toast('Testing Gemini model access…');
  const result=await api('/api/integrations/gemini/test',{method:'POST'});
  toast('Gemini API connected and responding: '+result.model);
 }catch(err){toast('Gemini API test failed: '+err.message,true)}
}
let reviewedJobId = '';
let reviewedPdfPreviewed = false;

async function uploadFixedResume(form){
 const file=form.querySelector('input[type=file]')?.files?.[0];
 if(!file){toast('Select your final PDF.',true);return;}
 const button=form.querySelector('button[type=submit]');
 if(button)button.disabled=true;
 try{
  const data=new FormData();data.append('file',file);
  const current=state.data.resumes.find(r=>r.id===form.dataset.kind);
  if(current?.configured){if(!form.querySelector('[name=replace]')?.checked)throw Error('Confirm replacement first.');data.append('expected_sha256',current.sha256);}
  const response=await fetch('/api/resumes/'+form.dataset.kind,{method:'POST',body:data});
  const result=await response.json().catch(()=>({}));
  if(!response.ok)throw Error(result.detail||'Resume upload failed.');
  await load();
  toast('PDF saved unchanged. Choose this version when reviewing an application.');
 }catch(e){toast(e.message,true)}
 finally{if(button)button.disabled=false;}
}

let reviewResumes=[];
function selectedResume(){
 const selected=reviewResumes.find(r=>r.id===$('reviewed-resume')?.value);
 if(!selected)throw Error('Choose an uploaded resume.');
 return {resume_id:selected.id,resume_sha256:selected.sha256};
}

async function geminiPreview(id){
 try{
  toast('Gemini is selecting relevant achievements from your approved resume…');
  const r=await api('/api/jobs/'+id+'/gemini-preview',{method:'POST'});
  if(!r.resumes?.length)throw Error('Resume selection was not returned. Please refresh Atlas.');
  const j=jobOf(id);reviewedJobId=id;reviewedPdfPreviewed=false;reviewResumes=r.resumes;
  const status=r.mode==='gemini'?'Gemini drafted the email':r.mode==='gemini_backup'?'Backup Gemini drafted the email':'Facts-only email fallback';
  const warning=r.warning?'<p class="hint" style="color:#a34e3b">'+escapeHtml(r.warning)+'</p>':'';
  const resumePicker='<label class="form-label" for="reviewed-resume">Resume version</label><select class="form-input" id="reviewed-resume">'+r.resumes.map(v=>'<option value="'+v.id+'">'+escapeHtml(v.label+' — '+v.filename)+'</option>').join('')+'</select>';
  const contacts=(j?.contacts||[]).filter(c=>c.email&&c.id);
  const contactPicker=contacts.length?'<label class="form-label" style="margin-top:16px">Hiring contact / Gmail recipient</label><select class="form-input" id="reviewed-contact">'+contacts.map(c=>'<option value="'+escapeHtml(c.id)+'">'+escapeHtml(c.name+' — '+c.email)+'</option>').join('')+'</select>':'<p class="hint" style="color:#a34e3b">No contact email yet. Add a verified hiring contact or use RocketReach before creating a Gmail draft.</p>';
  const replace=j?.outreach?.gmail_draft_id?'<label style="display:block;margin-top:12px;font-size:13px"><input type="checkbox" id="replace-reviewed-draft"> Replace existing Gmail draft (overwrites any edits already made in Gmail)</label>':'';
  modal('<h2>Review Gemini application</h2><p><strong>'+escapeHtml(status)+'</strong> · '+escapeHtml(r.model)+'</p>'+
   '<p class="hint">Review the email claims and choose one of your unchanged PDFs. Nothing is sent.</p>'+warning+
   '<label class="form-label">Subject</label><input id="gemini-subject" class="form-input" maxlength="200" value="'+escapeHtml(r.subject)+'">'+
   '<label class="form-label" style="margin-top:12px">Email body</label><textarea id="gemini-body" class="form-input form-textarea" style="min-height:260px" rows="12">'+escapeHtml(r.body)+'</textarea>'+
   '<button type="button" class="btn btn-ghost" id="copy-gemini-preview" style="margin-top:12px">Copy edited email</button>'+
   '<h3 style="margin-top:22px">Choose your resume</h3><p class="hint">Atlas attaches the exact uploaded file without editing it.</p>'+resumePicker+
   '<button type="button" class="btn btn-ghost" id="preview-reviewed-resume" style="margin-top:16px">Preview selected resume PDF</button>'+
   contactPicker+replace+
   '<label style="display:block;margin-top:14px;font-size:13px"><input type="checkbox" id="review-confirm"> I have reviewed the recipient, email and selected PDF.</label>'+
   '<div id="draft-error" role="alert" style="display:none;margin-top:12px;padding:10px;border:1px solid #efd3d3;border-radius:8px;color:#a24d55"></div>'+
   '<button type="button" class="btn btn-dark" id="create-reviewed-draft" '+(!contacts.length?'disabled':'')+' style="margin-top:14px;width:100%">Create verified Gmail draft with PDF — do not send</button>'+
   '<p class="hint">Gmail draft creation requires explicit confirmation. Atlas does not send or schedule this draft.</p>');
  $('form-dialog').classList.add('review-modal');
 }catch(err){toast('Gemini preview failed: '+err.message,true)}
}

async function previewReviewedResume(){
 let win=null;
 try{
  const selection=selectedResume();
  win=window.open('','_blank');
  if(win)win.document.write('<p>Opening your original PDF…</p>');
  const response=await fetch('/api/jobs/'+reviewedJobId+'/reviewed-resume.pdf',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(selection)});
  if(!response.ok){const error=await response.json().catch(()=>({}));throw Error(error.detail||'PDF preview failed');}
  const blob=await response.blob();
  if(blob.type!=='application/pdf')throw Error('Unexpected PDF response');
  const url=URL.createObjectURL(blob);
  if(win){win.location.href=url;}else{
   const a=document.createElement('a');a.href=url;a.download='Atlas_Reviewed_Resume.pdf';a.click();
  }
  reviewedPdfPreviewed=JSON.stringify(selectedResume())===JSON.stringify(selection);
  if(!reviewedPdfPreviewed)throw Error('Selection changed. Preview the selected resume again.');
  setTimeout(()=>URL.revokeObjectURL(url),120000);
 }catch(e){if(win)win.close();toast(e.message,true)}
}

async function createReviewedDraft(){
 const button=$('create-reviewed-draft');
 try{
  const error=$('draft-error');if(error)error.style.display='none';
  const selection=selectedResume();
  if(!reviewedPdfPreviewed)throw Error('Click Preview selected resume PDF, review the opened file, then click Create draft again.');
  if(!$('review-confirm')?.checked)throw Error('Confirm you reviewed the recipient, email and resume.');
  const job=jobOf(reviewedJobId);
  const contact=job?.contacts.find(c=>c.id===$('reviewed-contact')?.value);
  if(!contact?.email)throw Error('Select a hiring contact with a professional email.');
  if(job?.outreach?.gmail_draft_id&&!$('replace-reviewed-draft')?.checked)
   throw Error('Confirm replacement of the existing Gmail draft before continuing.');
  const subject=$('gemini-subject')?.value?.trim()||'';
  const body=$('gemini-body')?.value?.trim()||'';
  if(!subject||!body)throw Error('Enter a subject and email body.');
  if(button)button.disabled=true;
  const result=await api('/api/jobs/'+reviewedJobId+'/reviewed-draft',{
   method:'POST',body:JSON.stringify({
    contact_id:contact.id,recipient_email:contact.email,subject,body,
    ...selection,reviewed:true,
    replace_existing_draft:!!$('replace-reviewed-draft')?.checked})});
  closeModal();
  await load();
  modal('<h2>Gmail draft verified</h2><p>Gmail confirmed your reviewed email and attached PDF.</p>'+
   '<p><strong>Recipient:</strong> '+escapeHtml(result.recipient_email)+'</p>'+
   '<p><strong>Attachment:</strong> '+escapeHtml(result.resume_filename)+'</p>'+
   '<p class="hint">No email was sent or scheduled. Open Gmail to review the draft before sending it manually.</p>'+
   '<a class="btn btn-dark" href="'+safeUrl(result.gmail_url)+'" target="_blank" rel="noreferrer">Open Gmail drafts ↗</a>');
 }catch(e){const error=$('draft-error');if(error){error.textContent='Draft not created: '+e.message;error.style.display='block';error.scrollIntoView({block:'nearest'});}else toast('Draft not created: '+e.message,true)}
 finally{if(button)button.disabled=false;}
}
async function mutate(path){try{await api(path,{method:'POST'});await load();toast('Updated successfully.')}catch(e){toast(e.message,true)}}
document.addEventListener('submit',async e=>{if(e.target.classList.contains('fixed-resume-form')){e.preventDefault();return uploadFixedResume(e.target);}if(['sheet-import-form','csv-import-form'].includes(e.target.id)){e.preventDefault();await handleSheetSubmit(e.target)}});
document.addEventListener('click',e=>{if(e.target.closest('#preview-reviewed-resume'))return previewReviewedResume();if(e.target.closest('#create-reviewed-draft'))return createReviewedDraft();if(e.target.closest('#copy-gemini-preview')){const subject=$('gemini-subject')?.value||'';const body=$('gemini-body')?.value||'';navigator.clipboard.writeText('Subject: '+subject+'\n\n'+body).then(()=>toast('Email preview copied. No Gmail draft created.')).catch(()=>toast('Copy failed; select and copy the text manually.',true));}if(e.target.closest('#import-done')){closeModal();setPage('jobs')}if(e.target.closest('#retry-sheet-import')){sheetDialog()}if(e.target.closest('#copy-research-prompt')){const value=document.querySelector('#prompt-form textarea')?.value||'';navigator.clipboard.writeText(value).then(()=>toast('Prompt copied. Paste it into your scheduled ChatGPT task.')).catch(()=>toast('Copy failed. Select the text and copy manually.',true));}});
document.addEventListener('click',async e=>{
 const btn=e.target.closest('[data-page],[data-filter],[data-job],[data-action]');if(!btn)return;
 if(btn.dataset.page){setPage(btn.dataset.page);return}
 if(btn.dataset.filter){state.filter=btn.dataset.filter;render();return}
 if(btn.dataset.job){state.selected=btn.dataset.job;render();return}
 const action=btn.dataset.action,id=btn.dataset.id;
 if(action==='import')return importDialog();
 if(action==='contact')return contactDialog(id);
 if(action==='outreach')return setPage('outbox');
 if(action==='refresh')return load();
 if(action==='gemini-test')return geminiTest();
 if(action==='gemini-preview')return geminiPreview(id);
 if(action==='rocketreach')return rocketreachAction(id,false,btn.dataset.retry==='true');
 if(action==='rocketreach-check')return rocketreachAction(id,true,false);
 if(action==='approve')return mutate(`/api/jobs/${id}/approve`);
 if(action==='reject'){if(confirm('Pass on this opportunity?'))return mutate(`/api/jobs/${id}/reject`)}
 if(action==='cancel'){if(confirm('Cancel this unsent draft or follow-up?'))return mutate(`/api/outreach/${id}/cancel`)}
 if(action==='prepare')return mutate(`/api/outreach/${id}/prepare`);
});
document.addEventListener('change',e=>{if(e.target.id==='reviewed-resume'){reviewedPdfPreviewed=false;if($('review-confirm'))$('review-confirm').checked=false;}});
document.addEventListener('input',e=>{if(e.target.id==='job-search'){state.search=e.target.value;const sel=state.selected;const caret=e.target.selectionStart;render();state.selected=sel;const next=$('job-search');if(next){next.focus();next.setSelectionRange(caret,caret)}}});
document.addEventListener('submit',async e=>{if(!['import-form','contact-form','prompt-form'].includes(e.target.id))return;e.preventDefault();const form=e.target;const payload=Object.fromEntries(new FormData(form).entries());try{if(form.id==='prompt-form'){await api('/api/search/prompt',{method:'PUT',body:JSON.stringify(payload)});closeModal();toast('Research prompt saved for ChatGPT web search');return}const path=form.id==='import-form'?'/api/jobs/import':`/api/jobs/${form.dataset.jobid}/contacts`;await api(path,{method:'POST',body:JSON.stringify(payload)});closeModal();await load();toast(form.id==='import-form'?'Job added to pipeline':'Contact saved')}catch(err){toast(err.message,true)}});
$('close-dialog').onclick=closeModal;$('toggle-menu').onclick=()=> $('app-shell').classList.toggle('open');
if($('edit-search-prompt')) $('edit-search-prompt').onclick=promptDialog;
$('import-sheet').onclick=sheetDialog;
load();
