/* Browser-only interactive demonstration. Never calls external APIs. */
(()=>{'use strict';
const names=[
['Northstar Payments (DEMO)','Analytics Manager - Commercial Strategy','Gurugram',9.3,'Payments, SQL, forecasting and commercial analytics'],
['Helix Financial (DEMO)','Risk Analytics Manager','Bengaluru',9.0,'Fraud, risk, decisioning and data products'],
['Orion Commerce (DEMO)','Senior Manager, Business Intelligence','Hyderabad',8.8,'Leading dashboards, stakeholders and analysts'],
['Summit Networks (DEMO)','Manager, Revenue Analytics','Noida',8.5,'Pricing, margins, revenue and automation'],
['Bluepeak Insights (DEMO)','Analytics Consulting Manager','Mumbai',8.2,'Consulting, forecasting and cross-functional delivery']];
const initial=()=>({demo:true,jobs:names.map((x,i)=>({id:'sample-'+i,company:x[0],title:x[1],location:x[2],fit_score:x[3],fit_why:x[4],arrangement:'Hybrid (sample)',experience:'7–10 years (sample)',leadership:'Managerial (sample)',salary:'Not stated',source:'SAMPLE – NOT A REAL POST',post_url:'',apply_url:'',post_age:'Sample only – no posting date',posting_date_kind:'fictional_demo',verification:'fictional_demo',jd:'Fictional demonstration vacancy. No actual employer or opening is claimed.',gaps:'Verify original posting and contacts before live use.',author_name:'',author_url:'',status:'discovered',is_demo:true,created_at:new Date().toISOString(),contacts:[],outreach:null})),outreach:[],events:[],integrations:{gmail:false,telegram:false,rocketreach:false,serper:false,openai:false,database:'Browser-only preview'},settings:{timezone:'Asia/Kolkata',search_time:'09:00',search_weekdays:[1,2,3,4,5],lookback_days:45,max_jobs:5,send_start:'09:00',send_end:'15:00',send_time:'10:30',send_enabled:false,followup_enabled:false,followup_hours:48,max_followups:2}});
let data=initial();try{const old=JSON.parse(sessionStorage.getItem('atlasPreview')||'null');if(old&&old.demo&&Array.isArray(old.jobs))data=old;}catch{}
const save=()=>sessionStorage.setItem('atlasPreview',JSON.stringify(data));
const json=(obj,status=200)=>new Response(JSON.stringify(obj),{status,headers:{'Content-Type':'application/json'}});
const native=window.fetch.bind(window);
window.fetch=async(input,opts={})=>{const u=typeof input==='string'?input:input.url;if(!u.startsWith('/api/'))return native(input,opts);
const method=(opts.method||'GET').toUpperCase();
if(u==='/api/search/prompt'&&method==='GET')return json({prompt:sessionStorage.getItem('atlasDemoPrompt')||'DEMO PROMPT — Search publicly indexed LinkedIn hiring posts for Manager and Senior Manager Analytics jobs posted in the last 45 days. Prioritize the last 48 hours. Find original job links, posting dates, actual posters, and up to two relevant hiring contacts per role. Never invent people, dates or URLs. This browser-only demo does not perform a real search.',editable:true,source:'demo placeholder'});
if(u==='/api/search/prompt'&&method==='PUT'){const p=JSON.parse(opts.body||'{}');if((p.prompt||'').length<100)return json({detail:'Prompt must contain at least 100 characters'},422);sessionStorage.setItem('atlasDemoPrompt',p.prompt);return json({saved:true,characters:p.prompt.length});}
if(u==='/api/discover'&&method==='POST')return json({detail:'Browser-only demo: no live job search or API calls are made.'},409);
if(u==='/api/overview')return json(data);
if(u==='/api/health')return json({ok:true,demo:true,version:'browser-preview'});
const match=u.match(/^\/api\/jobs\/([^/]+)\/(approve|reject|contacts)$/);
if(match&&method==='POST'){const j=data.jobs.find(x=>x.id===match[1]);if(!j)return json({detail:'Sample not found'},404);
if(match[2]==='contacts'){const p=JSON.parse(opts.body||'{}');if(!p.name)return json({detail:'Name required'},422);j.contacts.push({id:'contact-'+Date.now(),name:p.name,title:p.title||'',category:'Demo contact',linkedin_url:p.linkedin_url||'',email:p.email||'',email_status:'demo_only',relationship:'potential',reason:'Entered for browser demonstration only'});data.events.unshift({action:'contact_added_demo',job_id:j.id,at:new Date().toISOString(),details:{}});}
else if(match[2]==='reject'){j.status='rejected';data.events.unshift({action:'job_rejected_demo',job_id:j.id,at:new Date().toISOString(),details:{}});}
else {if(j.status!=='discovered')return json({detail:'Already processed'},409);j.status='approved';const o={id:'out-'+j.id,job_id:j.id,contact_id:'',to_email:'',subject:'Interest in '+j.title+' (DEMO)',body:'This is fictional sample outreach. No email will be sent.',resume_name:'Demo_Resume.pdf',gmail_draft_id:'',gmail_thread_id:'',sent_message_id:'',status:'demo_draft',due_at:null,sent_at:null,last_reply_at:null,last_reply_class:'',followup_count:0,error:'',created_at:new Date().toISOString()};j.outreach=o;data.outreach.push(o);data.events.unshift({action:'job_approved_demo',job_id:j.id,at:new Date().toISOString(),details:{}});}save();return json(j.outreach||{status:j.status});}
if(u==='/api/jobs/import'&&method==='POST'){const p=JSON.parse(opts.body||'{}');const id='manual-'+Date.now();data.jobs.unshift({...data.jobs[0],id,company:p.company,title:p.title,location:p.location||'Not stated',jd:p.jd||'',post_url:p.post_url||'',post_age:'User-entered (not verified)',source:'User input (demo)',is_demo:true,verification:'user_supplied_unverified',status:'discovered',contacts:[],outreach:null,fit_score:7.5});save();return json({id,duplicate:false});}
const out=u.match(/^\/api\/outreach\/([^/]+)\/(cancel|prepare|schedule)$/);
if(out&&method==='POST'){const o=data.outreach.find(x=>x.id===out[1]);if(!o)return json({detail:'Outreach not found'},404);o.status=out[2]==='cancel'?'cancelled':'demo_draft';data.events.unshift({action:'outreach_'+out[2]+'_demo',job_id:o.job_id,at:new Date().toISOString(),details:{}});save();return json(o);}
return json({detail:'Unavailable in browser-only preview. No external action was taken.'},409);
};
document.addEventListener('DOMContentLoaded',()=>{document.getElementById('reset-preview')?.addEventListener('click',()=>{sessionStorage.removeItem('atlasPreview');location.reload()});});
})();