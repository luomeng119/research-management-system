import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { chromium } from '@playwright/test';
const root=new URL('../../',import.meta.url);
async function fixture(){
 const html=execFileSync(fileURLToPath(new URL('.venv/bin/python',root)),['-c',`
from jinja2 import Environment,ChoiceLoader,FileSystemLoader,DictLoader
from flask import Flask
static_app=Flask(__name__,static_url_path='/static')
static_adapter=static_app.url_map.bind('equipment.fixture')
env=Environment(loader=ChoiceLoader([DictLoader({'base.html':'<!doctype html><link rel="stylesheet" href="/static/css/bootstrap.min.css"><main>{% block content %}{% endblock %}</main><script src="/static/js/bootstrap.bundle.min.js"></script>'}),FileSystemLoader('app/templates')]),autoescape=True)
env.globals['url_for']=lambda endpoint, **values: static_adapter.build(endpoint,values)
e={'id':'eq1','name':'演练设备','model':'M1','category':'通用设备','group_id':'group1','quantity':2,'location':'原位置'}
print(env.get_template('projects/detail.html').render(project={'project_id':'fixture','name':'fixture'},category='research',project_equipment=[e],available_equipment=[e],available_equipment_total=1,project_groups=[],folder_types=[],folder_tree=[]))
`],{cwd:fileURLToPath(root),encoding:'utf8'});
 const browser=await chromium.launch({headless:true});const page=await browser.newPage();const writes=[],dialogs=[],errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async r=>{const q=r.request(),u=new URL(q.url());
  if(q.method()==='POST'){writes.push({path:u.pathname,body:q.postData()});return r.fulfill({json:{success:false,message:'quantity 必须是整数'}});}
  if(u.pathname==='/')return r.fulfill({body:html,contentType:'text/html; charset=utf-8'});
  if(u.pathname.startsWith('/static/'))return r.fulfill({body:await readFile(new URL('app'+u.pathname,root))});return r.abort();
 });await page.goto('http://equipment.fixture/');return{browser,page,writes,dialogs,errors};
}
test('duplicate equipment exposes existing values and Cancel never submits',async()=>{
 const{browser,page,writes,errors}=await fixture();try{
  await page.getByRole('button',{name:'添加设备',exact:false}).click();await page.locator('.eq-row input').check();
  assert.equal(await page.locator('#eqSelectedBody input[type=number]').inputValue(),'2');
  assert.equal(await page.locator('#eqSelectedBody input[type=text]').inputValue(),'原位置');
  assert.match(await page.locator('#eqSelectedCount').locator('..').innerText(),/1\s*种/);
  assert.match(await page.locator('#eqSelectedBody').innerText(),/已关联/);
  let prompt='';page.on('dialog',async d=>{prompt=d.message();await d.dismiss();});
  await page.locator('#eqSelectedBody input[type=number]').fill('3');
  await page.evaluate(()=>renderEquipmentCandidates([{id:'eq2',name:'新增设备',model:'M2',category:'通用设备'}],true));
  await page.locator('.eq-row input[value="eq2"]').check();
  assert.equal(await page.locator('#eqSelectedBody input[type=number]').first().inputValue(),'3');
  await page.locator('#equipmentModal').getByRole('button',{name:'确定',exact:true}).click();
  assert.match(prompt,/覆盖/);assert.match(prompt,/不累加/);assert.match(prompt,/原位置/);assert.match(prompt,/2.*3/);assert.equal(writes.length,0);assert.deepEqual(errors,[]);
 }finally{await browser.close();}
});
test('confirmed duplicate preserves replacement quantity and Chinese server error',async()=>{
 const{browser,page,writes,errors}=await fixture();const messages=[];let alertDone;const handled=new Promise(resolve=>{alertDone=resolve;});try{
  page.on('dialog',async d=>{messages.push(d.message());await d.accept();if(d.type()==='alert')alertDone();});
  await page.getByRole('button',{name:'添加设备',exact:false}).click();await page.locator('.eq-row input').check();await page.locator('#eqSelectedBody input[type=number]').fill('3');
  const posted=page.waitForResponse(r=>r.request().method()==='POST');await page.locator('#equipmentModal').getByRole('button',{name:'确定',exact:true}).click();await posted;await handled;
  assert.equal(writes.length,1);assert.equal(new URLSearchParams(writes[0].body).get('quantity'),'3');assert.ok(messages.some(m=>m.includes('数量')&&!m.includes('quantity')));assert.deepEqual(errors,[]);
 }finally{await browser.close();}
});
test('invalid quantity is rejected in Chinese before inline save',async()=>{
 const{browser,page,writes,errors}=await fixture();const messages=[];try{
  page.on('dialog',d=>{messages.push(d.message());return d.accept();});
  for(const value of ['0','-1','1.5']){await page.locator('#collapseEquipment input[type=number]').fill(value);await page.locator('h4').click();}
  assert.equal(writes.length,0);assert.equal(messages.length,3);assert.ok(messages.every(m=>m.includes('数量')&&!m.includes('quantity')));assert.deepEqual(errors,[]);
 }finally{await browser.close();}
});
