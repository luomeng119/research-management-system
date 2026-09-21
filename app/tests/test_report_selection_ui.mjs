import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const source=readFileSync('app/templates/research_reports/wording.html','utf8').match(/<script>([\s\S]*)<\/script>/)[1];
class Node {
  constructor(){this.listeners={};this.children=[];this.value='';this.disabled=false;this.dataset={};this.style={};this.textContent='';this.selectionStart=0;this.selectionEnd=0;}
  addEventListener(event, fn){(this.listeners[event]??=[]).push(fn);}
  async fire(event){for(const fn of this.listeners[event]??[])await fn({preventDefault(){}});}
  setSelectionRange(start,end){this.selectionStart=start;this.selectionEnd=end;}
  replaceChildren(...nodes){this.children=nodes;}
  append(...nodes){this.children.push(...nodes);}
  get childElementCount(){return this.children.length;}
  focus(){}
}
function setup(fetch){
  const names=['wording-tools','body','wording-request','wording-cancel','wording-undo','wording-status','wording-candidates','wording-processor'];
  const nodes=Object.fromEntries(names.map(n=>[n,new Node()]));
  const version={value:'2'},csrf={value:'token'},dictionary={value:''};
  nodes.body.form={querySelector:q=>q.includes('baseVersion')?version:q.includes('dictionaryVersion')?dictionary:csrf};
  nodes['wording-tools'].dataset.url='/research-reports/r1/selection-suggestions';
  const context={window:{},document:{getElementById:id=>nodes[id],createElement:()=>new Node()},fetch,crypto:{randomUUID:()=> '16a6d65e-6268-4642-a3da-a865c46471c0'},AbortController};
  vm.runInNewContext(source,context);
  nodes.body.value='😀前文重复词，重复词\n尾文';
  const start=nodes.body.value.lastIndexOf('重复词');nodes.body.setSelectionRange(start,start+3);
  return {nodes,version,dictionary,state:context.window.ResearchSelectionState(nodes.body,version)};
}
const good=()=>Promise.resolve({ok:true,json:async()=>({data:{suggestions:[{text:'清晰措辞',reason:'简洁'}],processorStatus:'PENDING_INTEGRATION'}})});
let passed=0;
async function test(name,fn){await fn();passed++;process.stdout.write(name+' PASS\n');}
await test('exact repeated selection with emoji and undo',async()=>{
 const {nodes}=setup(good),before=nodes.body.value;
 await nodes['wording-request'].fire('click');
 await nodes['wording-candidates'].children[0].children[2].fire('click');
 assert.equal(nodes.body.value,'😀前文重复词，清晰措辞\n尾文');
 await nodes['wording-undo'].fire('click');assert.equal(nodes.body.value,before);
});
await test('cancelling candidate keeps original',async()=>{
 const {nodes}=setup(good),before=nodes.body.value;await nodes['wording-request'].fire('click');await nodes['wording-cancel'].fire('click');assert.equal(nodes.body.value,before);assert.equal(nodes['wording-candidates'].children.length,0);
});
await test('stale body response cannot apply',async()=>{
 let resolve;const {nodes}=setup(()=>new Promise(r=>resolve=r));const waiting=nodes['wording-request'].fire('click');nodes.body.value+='新内容';await nodes.body.fire('input');resolve(await good());await waiting;assert.equal(nodes['wording-candidates'].children.length,0);assert.match(nodes['wording-status'].textContent,/作废/);
});
await test('changed selection or version rejects adoption',async()=>{
 const {nodes,version}=setup(good);await nodes['wording-request'].fire('click');const adopt=nodes['wording-candidates'].children[0].children[2];version.value='3';nodes.body.setSelectionRange(0,2);const before=nodes.body.value;await adopt.fire('click');assert.equal(nodes.body.value,before);
});
await test('cross paragraph exact replacement',async()=>{
 const {nodes,state}=setup(good);nodes.body.value='头\n第一段\n第二段\n尾';nodes.body.setSelectionRange(2,9);const s=state.snapshot();assert.equal(state.apply(s,'合并表述'),true);assert.equal(nodes.body.value,s.prefix+'合并表述'+s.suffix);assert.equal(state.undo(),true);assert.equal(nodes.body.value,s.body);
});
await test('empty selection makes no request',async()=>{
 let calls=0;const {nodes}=setup(()=>{calls++;return good();});nodes.body.setSelectionRange(0,0);await nodes['wording-request'].fire('click');assert.equal(calls,0);
});
await test('model failure retains exact draft',async()=>{
 const {nodes}=setup(async()=>({ok:false,json:async()=>({error:{message:'本地失败'}})})),before=nodes.body.value;await nodes['wording-request'].fire('click');assert.equal(nodes.body.value,before);assert.match(nodes['wording-status'].textContent,/本地失败/);
});
await test('manual edit after adoption cannot be undone over',async()=>{
 const {nodes}=setup(good);await nodes['wording-request'].fire('click');await nodes['wording-candidates'].children[0].children[2].fire('click');nodes.body.value+='人工补充';await nodes.body.fire('input');const before=nodes.body.value;await nodes['wording-undo'].fire('click');assert.equal(nodes.body.value,before);
});
await test('cancel pending response never changes draft',async()=>{
 let resolve;const {nodes}=setup((url)=>url.endsWith('/cancel')?Promise.resolve({json:async()=>({data:{cancelled:true}})}):new Promise(r=>resolve=r));const before=nodes.body.value;const waiting=nodes['wording-request'].fire('click');await nodes['wording-cancel'].fire('click');resolve(await good());await waiting;assert.equal(nodes.body.value,before);assert.equal(nodes['wording-candidates'].children.length,0);
});
await test('adoption and undo preserve dictionary version binding',async()=>{
 const {nodes,dictionary}=setup(async()=>({ok:true,json:async()=>({data:{suggestions:[{text:'清晰措辞',reason:'简洁'}],dictionaryVersion:7}})}));
 assert.equal(dictionary.value,'');await nodes['wording-request'].fire('click');await nodes['wording-candidates'].children[0].children[2].fire('click');assert.equal(dictionary.value,'7');await nodes['wording-undo'].fire('click');assert.equal(dictionary.value,'');
});
process.stdout.write(JSON.stringify({passed,scope:'Node VM of production script, not browser or model quality validation'})+'\n');
