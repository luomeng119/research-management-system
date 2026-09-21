import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const html=readFileSync('app/templates/projects/lifecycle_detail.html','utf8');
assert.ok(html.includes('id="statusTransitionModal"'),'status dialog exists in the page');
assert.ok(!html.includes("window.prompt('请填写本次状态变化原因')"));
const source=html.split('// BEGIN status transition modal')[1]?.split('// END status transition modal')[0];
assert.ok(source,'production status dialog controller exists');
class Element {
 constructor(){this.events={};this.dataset={};this.disabled=false;this.value='';this.textContent='';this.focused=false;}
 addEventListener(name,fn){(this.events[name]??=[]).push(fn);}
 async fire(name){let prevented=false;for(const fn of this.events[name]??[])await fn({preventDefault(){prevented=true;}});return prevented;}
 focus(){this.focused=true;}
 setAttribute(name,value){this[name]=value;}
}
function setup(send){
 const ids=['statusTransitionModal','statusTransitionForm','statusTransitionReason','statusTransitionTarget','statusTransitionError','statusTransitionConfirm'];
 const nodes=Object.fromEntries(ids.map(id=>[id,new Element()]));
 const cancel=new Element(),button=new Element();button.dataset.status='ACTIVE';
 nodes.statusTransitionModal.querySelectorAll=()=>[cancel];
 const modal={shown:0,hidden:0,show(){this.shown++;nodes.statusTransitionModal.fire('shown.bs.modal');},hide(){this.hidden++;nodes.statusTransitionModal.fire('hidden.bs.modal');}};
 const window={location:{reload(){window.reloaded=true;}}};
 const context={document:{getElementById:id=>nodes[id],querySelectorAll:()=>[button]},bootstrap:{Modal:{getOrCreateInstance:()=>modal}},window,page:{dataset:{projectStatus:'PENDING'}},statusLabels:{PENDING:'待启动',ACTIVE:'执行中'},projectId:'project-1',send};
 vm.runInNewContext(source,context);
 return {nodes,cancel,button,modal,window};
}
let passed=0;
async function test(name,fn){await fn();passed++;process.stdout.write(name+' PASS\n');}
await test('opening and cancelling do not send transition',async()=>{
 let calls=0;const f=setup(async()=>calls++);await f.button.fire('click');assert.equal(f.modal.shown,1);assert.equal(f.nodes.statusTransitionReason.focused,true);assert.match(f.nodes.statusTransitionTarget.textContent,/待启动.*执行中/);await f.nodes.statusTransitionModal.fire('hidden.bs.modal');assert.equal(f.button.focused,true);assert.equal(calls,0);
});
await test('empty or whitespace reason cannot submit',async()=>{
 let calls=0;const f=setup(async()=>calls++);await f.button.fire('click');f.nodes.statusTransitionReason.value='  \n ';await f.nodes.statusTransitionForm.fire('submit');assert.equal(calls,0);assert.match(f.nodes.statusTransitionError.textContent,/原因/);
});
await test('confirmation sends exact reason once with original endpoint',async()=>{
 const calls=[];let finish;const f=setup((...args)=>{calls.push(args);return new Promise(resolve=>finish=resolve);});await f.button.fire('click');f.nodes.statusTransitionReason.value='  （确认启动）\n保留原文';const first=f.nodes.statusTransitionForm.fire('submit');await f.nodes.statusTransitionForm.fire('submit');assert.equal(calls.length,1);assert.equal(calls[0][0],'/api/projects/project-1/status-transitions');assert.equal(calls[0][1].toStatus,'ACTIVE');assert.equal(calls[0][1].reason,'  （确认启动）\n保留原文');assert.equal(f.nodes.statusTransitionConfirm.disabled,true);assert.equal(await f.nodes.statusTransitionModal.fire('hide.bs.modal'),true);finish({});await first;assert.equal(f.window.reloaded,true);
});
await test('failed submission retains reason and status, supports retry',async()=>{
 let fail=true;const f=setup(async()=>{if(fail)throw new Error('版本已更新');});await f.button.fire('click');f.nodes.statusTransitionReason.value='真实原因';await f.nodes.statusTransitionForm.fire('submit');assert.equal(f.nodes.statusTransitionReason.value,'真实原因');assert.equal(f.button.dataset.status,'ACTIVE');assert.equal(f.window.reloaded,undefined);assert.match(f.nodes.statusTransitionError.textContent,/版本已更新/);assert.equal(f.nodes.statusTransitionConfirm.disabled,false);assert.equal(f.cancel.disabled,false);fail=false;await f.nodes.statusTransitionForm.fire('submit');assert.equal(f.window.reloaded,true);
});
process.stdout.write(JSON.stringify({passed,scope:'production controller Node VM; no live server or DB writes'})+'\n');
