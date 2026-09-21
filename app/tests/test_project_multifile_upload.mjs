import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { chromium } from '@playwright/test';
const root=new URL('../../',import.meta.url);
async function fixture(outcomes={}) {
 const html=execFileSync(fileURLToPath(new URL('.venv/bin/python',root)),['-c',`
from jinja2 import Environment,ChoiceLoader,FileSystemLoader,DictLoader
from flask import Flask
app=Flask(__name__,static_url_path='/static');adapter=app.url_map.bind('upload.fixture')
env=Environment(loader=ChoiceLoader([DictLoader({'base.html':'<!doctype html><link rel="stylesheet" href="/static/css/bootstrap.min.css"><main>{% block content %}{% endblock %}</main><script src="/static/js/bootstrap.bundle.min.js"></script>'}),FileSystemLoader('app/templates')]),autoescape=True)
env.globals['url_for']=lambda endpoint,**values:adapter.build(endpoint,values)
print(env.get_template('projects/detail.html').render(project={'project_id':'fixture','name':'fixture'},category='research',project_equipment=[],available_equipment=[],available_equipment_total=0,project_groups=[],folder_types=[],folder_tree=[]))
`],{cwd:fileURLToPath(root),encoding:'utf8'});
 const browser=await chromium.launch({headless:true});const page=await browser.newPage();const writes=[],dialogs=[],errors=[];let navigations=0;
 page.on('pageerror',error=>errors.push(error.message));
 page.on('dialog',async dialog=>{dialogs.push(dialog.message());await dialog.accept();});
 await page.route('**/*',async route=>{
  const req=route.request(),url=new URL(req.url());
  if(req.method()==='POST'){
   const body=req.postDataBuffer().toString('utf8');const names=[...body.matchAll(/filename="([^"]+)"/g)].map(m=>m[1]);
   writes.push({names,body,path:url.pathname});const action=outcomes[names[0]];
   if(action==='network')return route.abort('failed');
   if(action==='reject')return route.fulfill({status:415,json:{success:false,message:'文件类型不支持'}});
   if(action==='malformed')return route.fulfill({status:200,body:'not-json'});
   if(action==='http-error')return route.fulfill({status:500,json:{success:true}});
   return route.fulfill({status:200,json:{success:true,fileId:'record-'+writes.length,versionNo:1}});
  }
  if(url.pathname==='/'){navigations++;return route.fulfill({body:html,contentType:'text/html; charset=utf-8'});}
  if(url.pathname.startsWith('/static/'))return route.fulfill({body:await readFile(new URL('app'+url.pathname,root))});
  return route.abort();
 });
 await page.goto('http://upload.fixture/');
 return {browser,page,writes,dialogs,errors,navigations:()=>navigations};
}
const files=names=>names.map(name=>({name,mimeType:'text/plain',buffer:Buffer.from(name+' contents')}));
async function submit(page,names,twice=false){
 await page.locator('#fileInput').setInputFiles(files(names));
 await page.locator('#uploadFolder').evaluate(el=>el.value='试验资料');
 await page.evaluate(twice=>{document.getElementById('uploadForm').requestSubmit();if(twice)document.getElementById('uploadForm').requestSubmit();},twice);
}
async function waitFor(predicate){for(let i=0;i<100;i++){if(predicate())return;await new Promise(resolve=>setTimeout(resolve,20));}throw new Error('bounded fixture wait timed out');}

test('modal five-file selection uploads every file separately to selected folder',async()=>{
 const x=await fixture();try{
  await submit(x.page,['a.txt','b.txt','c.txt','d.txt','e.txt']);await waitFor(()=>x.dialogs.length>0);
  assert.equal(x.writes.length,5);assert.deepEqual(x.writes.map(w=>w.names),[['a.txt'],['b.txt'],['c.txt'],['d.txt'],['e.txt']]);
  assert.ok(x.writes.every(w=>w.body.includes('试验资料')));assert.match(x.dialogs.at(-1),/成功\s*5/);assert.deepEqual(x.errors,[]);
 }finally{await x.browser.close();}
});
test('partial rejection and network uncertainty stay visible with no false success or reload',async()=>{
 const x=await fixture({'b.txt':'reject','c.txt':'network'});try{
  await submit(x.page,['a.txt','b.txt','c.txt']);await waitFor(()=>x.dialogs.length>0);
  assert.equal(x.writes.length,3);const message=x.dialogs.at(-1);assert.match(message,/成功\s*1/);assert.match(message,/失败\s*1/);assert.match(message,/未确认\s*1/);assert.match(message,/b\.txt.*文件类型不支持/);assert.match(message,/c\.txt/);
  assert.equal(x.navigations(),1);assert.match(await x.page.locator('#projectUploadFeedback').innerText(),/b\.txt/);assert.equal(await x.page.locator('#fileInput').inputValue(),'');assert.deepEqual(x.errors,[]);
 }finally{await x.browser.close();}
});
test('double submit does not upload the same selected files twice',async()=>{
 const x=await fixture();try{
  await submit(x.page,['a.txt','b.txt'],true);await waitFor(()=>x.dialogs.some(text=>/成功\s*2/.test(text)));
  assert.equal(x.writes.length,2);assert.deepEqual(x.errors,[]);
 }finally{await x.browser.close();}
});
test('HTTP failure with success payload and invalid JSON cannot count as success',async()=>{
 const x=await fixture({'a.txt':'http-error','b.txt':'malformed'});try{
  await submit(x.page,['a.txt','b.txt']);await waitFor(()=>x.dialogs.length>0);
  assert.equal(x.writes.length,2);assert.match(x.dialogs.at(-1),/成功\s*0/);assert.equal(x.navigations(),1);assert.deepEqual(x.errors,[]);
 }finally{await x.browser.close();}
});

test('simultaneous file and folder selections are rejected without dropping either silently',async()=>{
 const x=await fixture();try{
  await x.page.locator('#fileInput').setInputFiles(files(['a.txt']));
  await x.page.evaluate(()=>{const selected=new DataTransfer();selected.items.add(new File(['folder'],'folder.txt'));document.getElementById('folderInput').files=selected.files;document.getElementById('uploadForm').requestSubmit();});
  await waitFor(()=>x.dialogs.length>0);assert.equal(x.writes.length,0);assert.match(x.dialogs.at(-1),/一种上传方式/);assert.equal(x.navigations(),1);
 }finally{await x.browser.close();}
});
