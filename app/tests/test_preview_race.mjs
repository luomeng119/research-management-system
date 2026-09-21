import {test} from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const html=readFileSync(new URL('../templates/components/preview_panel.html',import.meta.url),'utf8');
function fixture(){
 const elements=new Map(),events={},pending=[];
 const doc={activeElement:null,getElementById(id){if(!elements.has(id))elements.set(id,{style:{},textContent:'',innerHTML:'',href:'',setAttribute(){},isConnected:true,classList:{add(){},remove(){}},focus(){doc.activeElement=this;},contains(el){return el===this;}});return elements.get(id);},addEventListener(k,v){events[k]=v;}};
 const opener={isConnected:true,focus(){doc.activeElement=this;}};doc.activeElement=opener;
 const context=vm.createContext({document:doc,window:{location:{origin:'http://test'}},URL,URLSearchParams,fetch:()=>new Promise((resolve,reject)=>pending.push({resolve,reject})),renderPreviewContent(data){doc.getElementById('previewPanelBody').textContent=data.content;}});
 vm.runInContext(html.slice(html.includes('var _previewRequestId') ? html.indexOf('var _previewRequestId') : html.indexOf('function openProjectPreview'),html.indexOf('function renderPreviewContent')),context);
 const open=name=>context.openControlledPreview(name,1,'PROJECT','p',name,'/download/'+name);
 const reply=async(i,value)=>{pending[i].resolve({headers:{get:()=> 'application/json'},ok:true,json:async()=>({success:true,content:value})});for(let n=0;n<8;n++)await Promise.resolve();};
 return{context,doc,opener,events,pending,open,reply};
}
test('late A cannot overwrite B title/body/download',async()=>{const x=fixture();x.open('A');x.open('B');await x.reply(1,'B body');await x.reply(0,'A body');assert.equal(x.doc.getElementById('previewPanelBody').textContent,'B body');assert.equal(x.doc.getElementById('previewFileName').textContent,'B');assert.equal(x.doc.getElementById('previewDownloadBtn').href,'/download/B');});
test('closed then reopened preview rejects stale failure',async()=>{const x=fixture();x.open('A');x.context.closePreview();x.open('B');await x.reply(1,'B body');x.pending[0].reject(new Error('late'));for(let n=0;n<8;n++)await Promise.resolve();assert.equal(x.doc.getElementById('previewPanelBody').textContent,'B body');});
test('Escape closes, restores opener and invalidates pending response',async()=>{const x=fixture();x.open('A');assert.equal(x.doc.activeElement,x.doc.getElementById('previewCloseBtn'));x.events.keydown({key:'Escape',preventDefault(){}});assert.equal(x.doc.activeElement,x.opener);await x.reply(0,'A body');assert.notEqual(x.doc.getElementById('previewPanelBody').textContent,'A body');});

test('legacy controlled-link entry shares latest-request guard',async()=>{const x=fixture();x.context.openPreview('/preview/file?fileId=A&versionNo=1&objectType=PROJECT&objectId=p','A','PROJECT','/download/A');x.open('B');await x.reply(1,'B body');await x.reply(0,'A body');assert.equal(x.doc.getElementById('previewPanelBody').textContent,'B body');});
