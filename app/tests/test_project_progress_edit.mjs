import {test} from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const html=readFileSync(new URL('../templates/projects/lifecycle_detail.html',import.meta.url),'utf8');
function setup(send){
 const handlers={},modalHandlers={},fields=Object.fromEntries(['recordedAt','status','summary','issues','nextActions'].map(k=>[k,{value:''}]));
 const error={textContent:''},button={disabled:false},cancel={disabled:false};
 const form={dataset:{kind:'progress'},elements:{namedItem:k=>fields[k]},querySelector:sel=>sel==='.form-error'?error:button,querySelectorAll:()=>[button,cancel],setAttribute(){},reset(){Object.values(fields).forEach(f=>f.value='');},addEventListener:(k,fn)=>handlers[k]=fn,closest:()=>modal};
 const modal={querySelector:()=>form,addEventListener:(k,fn)=>modalHandlers[k]=fn};
 const context={document:{getElementById:id=>id==='progressEditError'?error:modal,querySelectorAll:()=>[form]},Date,JSON,encodeURIComponent,projectId:'p1',version:7,send,FormData:class{constructor(){}entries(){return Object.entries(fields).map(([k,v])=>[k,v.value]);}},window:{location:{reload(){context.reloaded=true;}}}};
 const source=html.split('// BEGIN lifecycle record forms')[1]?.split('// END lifecycle record forms')[0];assert.ok(source,'record controller exists');vm.runInNewContext(source,context);
 return {context,fields,form,error,button,cancel,handlers,modalHandlers};
}
test('edit prefill keeps exact text and cancelled open sends nothing',()=>{let calls=0;const x=setup(()=>calls++);const item={id:'r1',recordedAt:'2026-09-10T12:30:00Z',status:'RISK',summary:' 原文\n（测试） ',issues:'问题',nextActions:'下一步',riskLevel:'HIGH'};x.modalHandlers['show.bs.modal']({relatedTarget:{dataset:{progress:JSON.stringify(item)}}});assert.equal(x.fields.summary.value,item.summary);assert.equal(x.form.dataset.recordId,'r1');assert.equal(calls,0);});
test('edit PATCH sends once, failure preserves input and enables retry',async()=>{let finish;const calls=[];const x=setup((...args)=>{calls.push(args);return new Promise((resolve,reject)=>finish=reject);});x.modalHandlers['show.bs.modal']({relatedTarget:{dataset:{progress:JSON.stringify({id:'r1',recordedAt:'2026-09-10T12:30:00Z',summary:'旧',status:'RISK',riskLevel:'HIGH'})}}});x.fields.summary.value=' 未保存\n新正文 ';const first=x.handlers.submit({preventDefault(){}});await x.handlers.submit({preventDefault(){}});assert.equal(calls.length,1);assert.equal(calls[0][0],'/api/projects/p1/progress/r1');assert.equal(calls[0][2],'PATCH');assert.equal(calls[0][3],7);assert.equal(calls[0][1].riskLevel,'HIGH');assert.equal(calls[0][1].recordedAt,'2026-09-10T12:30:00Z');assert.equal(x.button.disabled,true);finish(new Error('版本冲突'));await first;assert.equal(x.fields.summary.value,' 未保存\n新正文 ');assert.match(x.error.textContent,/版本冲突/);assert.equal(x.button.disabled,false);assert.equal(x.context.reloaded,undefined);});
test('invalid time is visible error and no request; new open clears edit identity',async()=>{let calls=0;const x=setup(()=>calls++);x.form.dataset.recordId='old';x.modalHandlers['show.bs.modal']({relatedTarget:{dataset:{}}});assert.equal(x.form.dataset.recordId,'');x.fields.recordedAt.value='invalid';await x.handlers.submit({preventDefault(){}});assert.equal(calls,0);assert.ok(x.error.textContent);assert.equal(x.button.disabled,false);});

test('invalid record cancels opening and cannot submit stale previous record',()=>{const x=setup(()=>{});x.form.dataset.recordId='old';let prevented=false;x.modalHandlers['show.bs.modal']({relatedTarget:{dataset:{progress:JSON.stringify({id:'bad',recordedAt:'invalid'})}},preventDefault(){prevented=true;}});assert.equal(prevented,true);assert.equal(x.form.dataset.recordId,'');assert.ok(x.error.textContent);});
