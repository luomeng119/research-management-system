import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';
const source=readFileSync(new URL('../static/js/research-path.js',import.meta.url),'utf8');
const fn=source.slice(source.indexOf('  function updateInspector('),source.indexOf('  async function selectNode('));
test('inspector renders display values and keeps raw trace in titles',()=>{
 const dd=Array.from({length:4},()=>({})),dt=Array.from({length:4},()=>({}));
 const parts={'.path-inspector-status':{},h3:{},':scope > p':{},'footer p':{}};
 const inspector={querySelector:s=>parts[s],querySelectorAll:s=>s==='dd'?dd:dt};
 const data={status:'done',title:'进展',summary:'GENERAL_RESEARCH · CLOSED',summaryDisplay:'一般科研项目 · 已结题',owner:'记录人账号 10',ownerDisplay:'张老师',ownerLabel:'记录人',period:'2026-09-08T22:36:00+08:00',periodDisplay:'2026-09-08 22:36',periodLabel:'记录时间',source:'进展记录 uuid',sourceDisplay:'进展记录'};
 const context={document:{querySelector:()=>inspector},tree:{},flatten:()=>[{id:'fixture',data}],STATUS:{done:{label:'已完成'}},escapeText:(v,f='未记录')=>v||f};
 vm.runInNewContext(fn+';updateInspector("fixture")',context);
 assert.equal(parts[':scope > p'].textContent,data.summaryDisplay);
 assert.equal(parts[':scope > p'].title,data.summary);
 for(const [index,display,raw] of [[0,data.ownerDisplay,data.owner],[1,data.periodDisplay,data.period],[2,data.sourceDisplay,data.source]]){assert.equal(dd[index].textContent,display);assert.equal(dd[index].title,raw);}
 assert.equal(dt[0].textContent,'记录人');assert.equal(dt[1].textContent,'记录时间');
 data.periodLabel=undefined;data.ownerLabel=undefined;
 vm.runInNewContext(fn+';updateInspector("fixture")',context);
 assert.equal(dt[1].textContent,'时间说明');assert.equal(dt[0].textContent,'负责人');
});
