import {test} from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const html=readFileSync(process.env.FOLDER_TEMPLATE || new URL('../templates/projects/detail.html',import.meta.url),'utf8');
function fixture(){
 const nodes={uploadForm:{},folderInput:{files:[{name:'a.txt',webkitRelativePath:'folder/a.txt'}]},fileInput:{files:[]},uploadFolder:{value:'资料'},projectUploadFeedback:{},submit:{disabled:false}};
 const requests=[],messages=[];let reloads=0;
 const ctx=vm.createContext({document:{getElementById:id=>nodes[id],querySelector:()=>nodes.submit},FormData:class{append(){}},projectUpdateUrl:'/projects',projectUploadBusy:false,fetch:()=>new Promise((resolve,reject)=>requests.push({resolve,reject})),alert:m=>messages.push(m),location:{reload(){reloads++;}}});
 vm.runInContext(html.slice(html.indexOf("document.getElementById('uploadForm').onsubmit"),html.indexOf("document.getElementById('archiveForm').onsubmit")).replace(/{{[\s\S]*?}}/g,'fixture'),ctx);
 const submit=()=>nodes.uploadForm.onsubmit.call(nodes.uploadForm,{preventDefault(){}});
 const settle=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
 return {ctx,nodes,requests,messages,submit,settle,reloads:()=>reloads};
}
test('folder duplicate submit is blocked until settled',async()=>{const x=fixture();x.submit();x.submit();assert.equal(x.requests.length,1);assert.equal(x.nodes.submit.disabled,true);x.requests[0].resolve({ok:true,json:async()=>({success:true})});await x.settle();assert.equal(x.ctx.projectUploadBusy,false);assert.equal(x.nodes.submit.disabled,false);});
test('folder HTTP error cannot fake success, preserves selection for retry',async()=>{const x=fixture();x.submit();x.requests[0].resolve({ok:false,status:500,json:async()=>({success:true})});await x.settle();assert.equal(x.reloads(),0);assert.match(x.messages.join(' '),/失败|拒绝/);assert.equal(x.nodes.folderInput.files.length,1);x.submit();assert.equal(x.requests.length,2);});
test('unknown folder outcome stays visible and releases busy without reload',async()=>{const x=fixture();x.submit();x.requests[0].reject(new Error('network'));await x.settle();assert.equal(x.reloads(),0);assert.match(x.messages.join(' '),/未确认/);assert.equal(x.ctx.projectUploadBusy,false);});
