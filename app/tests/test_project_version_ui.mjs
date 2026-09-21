import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { chromium } from '@playwright/test';
const root=new URL('../../',import.meta.url);
async function fixture(fail=false){
 const html=execFileSync(fileURLToPath(new URL('.venv/bin/python',root)),['-c',`
from jinja2 import Environment,ChoiceLoader,FileSystemLoader,DictLoader
from flask import Flask
static_app=Flask(__name__,static_url_path='/static')
static_adapter=static_app.url_map.bind('versions.fixture')
env=Environment(loader=ChoiceLoader([DictLoader({'base.html':'<!doctype html><link rel="stylesheet" href="/static/css/bootstrap.min.css"><main>{% block content %}{% endblock %}</main><script src="/static/js/bootstrap.bundle.min.js"></script>'}),FileSystemLoader('app/templates')]),autoescape=True)
env.globals['available_equipment_total']=0
env.globals['url_for']=lambda endpoint, **values: static_adapter.build(endpoint,values)
print(env.get_template('projects/detail.html').render(project={'project_id':'fixture-project','name':'Fixture'},category='research',project_equipment=[],available_equipment=[],project_groups=[],folder_types=['任务输入文件'],folder_tree=[{'name':'任务输入文件','path':'任务输入文件','fileCount':2,'files':[{'name':'版本.txt','path':'任务输入文件/版本.txt','size':442,'fileId':'fixture-file','versionNo':2},{'name':'未登记.txt','path':'任务输入文件/未登记.txt','size':20}],'children':[]}]))
`],{cwd:fileURLToPath(root),encoding:'utf8'});
 const browser=await chromium.launch({headless:true});const page=await browser.newPage();const calls=[],errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async r=>{const u=new URL(r.request().url());calls.push({path:u.pathname,query:u.search,method:r.request().method()});
  if(u.pathname==='/')return r.fulfill({body:html,contentType:'text/html; charset=utf-8'});
  if(u.pathname.startsWith('/static/'))return r.fulfill({body:await readFile(new URL('app'+u.pathname,root))});
  if(u.pathname==='/api/files/fixture-file/versions')return r.fulfill({status:fail?404:200,json:fail?{message:'文件不存在'}:{fileId:'fixture-file',originalName:'版本.txt',versionNo:2,versions:[{versionNo:2,createdAt:'2026-09-09T12:00:00.123456+08:00',sizeBytes:442,mediaType:'text/plain'},{versionNo:1,createdAt:null,sizeBytes:12,mediaType:'text/plain'}]}});
  if(u.pathname==='/preview/file')return r.fulfill({json:{success:true,type:'text',content:'旧版内容'}});
  return r.abort();
 });await page.goto('http://versions.fixture/');return{browser,page,calls,errors};
}
test('controlled project attachment has version history and exact version preview/download',async()=>{
 const{browser,page,calls,errors}=await fixture();try{
  assert.equal(await page.locator('.project-file-version').textContent(),'v2');
  assert.equal(await page.locator('.btn-file-history').count(),1);
  assert.equal(await page.locator('.file-cb').count(),2);assert.equal(await page.locator('.btn-rename').count(),2);assert.equal(await page.locator('.btn-del').count(),2);
  await page.locator('.btn-file-history').click();await page.locator('#projectFileHistoryList a').first().waitFor();
  assert.equal(await page.locator('#projectFileHistoryList a').count(),2);
  assert.match(await page.locator('#projectFileHistoryList').innerText(),/未记录/);
  assert.equal(await page.locator('#projectFileHistoryList time').first().textContent(),'2026-09-09 12:00');
  assert.equal(await page.locator('#projectFileHistoryList time').first().getAttribute('title'),'2026-09-09T12:00:00.123456+08:00');
  assert.equal(await page.locator('#projectFileHistoryList time').last().textContent(),'时间未记录');
  const download=await page.locator('#projectFileHistoryList a').last().getAttribute('href');assert.match(download,/versions\/1\/download/);assert.match(download,/objectId=fixture-project/);
  await page.locator('#projectFileHistoryList button').last().click();await page.locator('#previewPanel.open').waitFor();
  assert.ok(calls.some(c=>c.path==='/preview/file'&&c.query.includes('versionNo=1')));
  assert.ok(calls.every(c=>c.method==='GET'));assert.deepEqual(errors,[]);
 }finally{await browser.close();}
});
test('history lookup failure is visible and dialog closes with Escape',async()=>{
 const{browser,page,errors}=await fixture(true);try{
  await page.locator('.btn-file-history').click();await page.getByText('文件不存在',{exact:true}).waitFor();
  assert.equal(await page.locator('#projectFileHistoryList a').count(),0);
  await page.keyboard.press('Escape');assert.equal(await page.locator('#projectFileHistory').isVisible(),false);
  assert.equal(await page.evaluate(()=>document.activeElement.classList.contains('btn-file-history')),true);assert.deepEqual(errors,[]);
 }finally{await browser.close();}
});
test('late response from closed history cannot overwrite reopened history',async()=>{
 const{browser,page,errors}=await fixture();let release;let count=0;
 const held=new Promise(resolve=>{release=resolve;});
 try{
  await page.route('**/api/files/fixture-file/versions?*',async route=>{
   const index=++count;if(index===1)await held;
   const no=index===1?1:2;
   await route.fulfill({json:{fileId:'fixture-file',versionNo:no,versions:[{versionNo:no,sizeBytes:no,createdAt:null}]}});
  });
  const first=page.waitForRequest('**/api/files/fixture-file/versions?*');await page.locator('.btn-file-history').click();await first;
  await page.keyboard.press('Escape');await page.locator('.btn-file-history').click();
  await page.getByRole('link',{name:'下载 v2',exact:true}).waitFor();release();await page.waitForLoadState('networkidle');
  assert.equal(await page.getByRole('link',{name:'下载 v2',exact:true}).count(),1);
  assert.equal(await page.getByRole('link',{name:'下载 v1',exact:true}).count(),0);assert.deepEqual(errors,[]);
 }finally{release();await browser.close();}
});
