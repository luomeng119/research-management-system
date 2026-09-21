import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { chromium } from '@playwright/test';
import { fileURLToPath } from 'node:url';
const root=new URL('../../',import.meta.url);
test('log summaries, filters and source timestamps remain intact',async()=>{
 const html=execFileSync(fileURLToPath(new URL('.venv/bin/python',root)),['-c',`
from jinja2 import Environment,ChoiceLoader,FileSystemLoader,DictLoader
from datetime import datetime,timezone,timedelta
env=Environment(loader=ChoiceLoader([DictLoader({'base.html':'<!doctype html><meta charset="utf-8">{% block content %}{% endblock %}'}),FileSystemLoader('app/templates')]),autoescape=True)
logs=[{'timestamp':datetime(2026,9,9,21,48,34,123456,tzinfo=timezone(timedelta(hours=8))),'operation_type':'添加设备'},{'timestamp':'2026-09-09T21:48:34.654321+08:00','operation_type':'删除设备'},{'timestamp':None,'operation_type':'导入文件'},{'timestamp':None,'operation_type':'PREVIEW'}]
print(env.get_template('equipment/logs.html').render(module_title='设备知识库',logs=logs,request={'args':{'operator':'张老师','file_name':'原文件','operation_type':'PREVIEW','start_date':'2026-09-01','end_date':'2026-09-09'}}))
`],{cwd:fileURLToPath(root),encoding:'utf8'});
 const browser=await chromium.launch({headless:true});const page=await browser.newPage();try{
  await page.setContent(html);assert.deepEqual(await page.locator('.log-summary h2').allTextContents(),['4','1','1','1']);assert.equal(await page.locator('tbody tr').count(),4);
  assert.deepEqual(await page.locator('time').allTextContents(),['2026-09-09 21:48','2026-09-09 21:48']);
  assert.equal(await page.locator('time').first().getAttribute('title'),'2026-09-09 21:48:34.123456+08:00');assert.equal(await page.locator('time').last().getAttribute('title'),'2026-09-09T21:48:34.654321+08:00');
  assert.equal(await page.getByText('未记录',{exact:true}).count(),2);
  assert.equal(await page.getByLabel('开始日期').inputValue(),'2026-09-01');assert.equal(await page.getByLabel('结束日期').inputValue(),'2026-09-09');assert.equal(await page.getByLabel('操作人',{exact:true}).inputValue(),'张老师');assert.equal(await page.getByLabel('文件名',{exact:true}).inputValue(),'原文件');assert.equal(await page.getByLabel('操作类型').inputValue(),'PREVIEW');
  assert.equal(await page.locator('form').getAttribute('method'),'get');assert.equal(await page.locator('option').count(),8);
 }finally{await browser.close();}
});
