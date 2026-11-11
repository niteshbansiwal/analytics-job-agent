// Offline contracts for navigation, bounded reads and read-only refresh.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const elements=new Map(),requests=[];
function element(id){if(!elements.has(id))elements.set(id,{innerHTML:'',textContent:'',style:{},open:false,addEventListener(){},classList:{add(){},remove(){},toggle(){}},appendChild(){}});return elements.get(id)}
const data={demo:false,jobs:[],outreach:[],events:[],resumes:[],integrations:{},settings:{},totals:{jobs:80,new_jobs:3},jobs_page:{page:1,total:80,page_size:20},revision:'r1'};
const row={id:'o1',job_id:'j1',company:'Example',title:'Analytics Manager',contact_name:'Person',to_email:'',status:'pending_contact'};
const context=vm.createContext({console,URL,URLSearchParams,AbortController,CSS:{escape:x=>x},window:{scrollY:0,scrollTo(){}},localStorage:{getItem(){return null}},setTimeout(){},clearTimeout(){},setInterval(){},document:{hidden:false,activeElement:null,getElementById:element,addEventListener(){},querySelectorAll(){return []},createElement(){return {remove(){}}}},fetch:async(path,opts)=>{
 requests.push([path,opts?.method||'GET']);let result=data;
 if(path.startsWith('/api/workspace/outreach?'))result={items:[row],page:1,total:1,page_size:25,summary:{sent:0,replied:0,unread:0,action_pending:0}};
 else if(path.startsWith('/api/workspace/logs?'))result={items:[{id:'j1',company:'Example',title:'Analytics Manager'}],page:1,total:1,page_size:25};
 else if(path==='/api/workspace/changes')result={revision:'r1'};
 return {ok:true,json:async()=>result};
}});
vm.runInContext(fs.readFileSync('public/app.js','utf8'),context);
vm.runInContext(fs.readFileSync('public/email-controls.js','utf8'),context);
vm.runInContext(fs.readFileSync('public/workspace.js','utf8').replace(/load\(\);\s*$/,''),context);
(async()=>{
 await vm.runInContext('load()',context);
 assert(element('main-view').innerHTML.includes('Your outreach'));
 assert(!element('demo-banner').innerHTML.includes('FIRST IMPORT'));
 assert(requests.some(([p])=>p.startsWith('/api/overview?section=outbox')));
 await vm.runInContext("setPage('logs')",context);
 assert(element('main-view').innerHTML.includes('Not recorded'));
 assert(element('main-view').innerHTML.includes('First email sent'));
 assert(element('main-view').innerHTML.includes('Read-only history'));
 const before=requests.length;
 await vm.runInContext('pollWorkspace()',context);
 assert.equal(requests.length,before+1); // unchanged revision must not fetch full data
 assert(requests.every(([,method])=>method==='GET'));
 assert(fs.readFileSync('public/index.html','utf8').includes('data-page="logs"'));
 assert(fs.readFileSync('public/index.html','utf8').includes('/assets/email-controls.js?v=fast-1'));
 vm.runInContext(`state.page='emails';state.emailControls={controls:{subjects:['{{first_name}}, analytics at {{company}}'],subject_mode:'rotate',subject_index:0,body_template:'Hi {{first_name}},',max_repairs:2,resume_id:'analytics',allowed_fact_ids:['dashboards']},facts:{dashboards:'Verified dashboards achievement'},resumes:[]};render();`,context);
 assert(element('main-view').innerHTML.includes('Email studio'));
 assert(element('main-view').innerHTML.includes('Body template'));
 const formatted=vm.runInContext("draftHtml('Analytics role','Hi Priya, https://careers.example.com/jobs/123?x=1&y=2')",context);
 assert(formatted.includes('href="https://careers.example.com/jobs/123?x=1&amp;y=2"'));

 const realFetch=context.fetch;let release;
 const gate=new Promise(resolve=>release=resolve);
 context.fetch=async(...args)=>{await gate;return realFetch(...args)};
 const pending=vm.runInContext("setPage('outbox')",context);
 assert(element('main-view').innerHTML.includes('Your outreach')); // paint cache before network completes
 release();await pending;context.fetch=realFetch;
 console.log('Workspace navigation, log history and refresh contracts passed');
})().catch(e=>{console.error(e);process.exitCode=1});
