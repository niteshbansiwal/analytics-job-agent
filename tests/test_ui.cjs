// Contract tests for fixed-PDF controls without external providers or a browser session.
const fs=require('node:fs'); const vm=require('node:vm'); const assert=require('node:assert/strict');
const elements=new Map();
const element=id=>{if(!elements.has(id))elements.set(id,{innerHTML:'',value:'',checked:false,classList:{add(){},remove(){}},showModal(){},appendChild(){}});return elements.get(id)};
const context=vm.createContext({console,URL,document:{getElementById:element,addEventListener(){},querySelectorAll(){return []},createElement(){return {remove(){}}}},setTimeout(){},fetch:async()=>{throw Error('Unexpected request')}});
let source=fs.readFileSync('public/app.js','utf8');source=source.replace(/load\(\);\s*$/,'');vm.runInContext(source,context);
vm.runInContext(`state.data={demo:false,jobs:[{id:'job',contacts:[]}],integrations:{},settings:{},resumes:[{id:'analytics',label:'Analytics Professional',configured:true,filename:'analytics.pdf',page_count:1,sha256:'a'.repeat(64)},{id:'data_science',label:'Data Science',configured:false}]};`,context);
const html=vm.runInContext('settingsPanel()',context);
assert(html.includes('data-kind="analytics"'));assert(html.includes('data-kind="data_science"'));assert(!html.includes('master-resume-form'));assert(!html.includes('15 of 18'));
vm.runInContext(`fetch=async()=>({ok:true,json:async()=>({mode:'facts_only_fallback',model:'none',subject:'Test',body:'Review email',resumes:state.data.resumes.filter(r=>r.configured)})});`,context);
(async()=>{await vm.runInContext("geminiPreview('job')",context);const review=element('modal-inner').innerHTML;assert(review.includes('id="reviewed-resume"'));assert(!review.includes('resume-bullet'));element('reviewed-resume').value='analytics';assert.equal(vm.runInContext('selectedResume().resume_id',context),'analytics');console.log('Fixed-PDF settings and review UI contracts passed');})().catch(e=>{console.error(e);process.exitCode=1});
