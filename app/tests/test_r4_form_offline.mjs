// Exact production Jinja form/assistant, synthetic context and shell, no app/DB/provider.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { chromium } from '@playwright/test';
const root = new URL('../../',import.meta.url);
const render = options => execFileSync(fileURLToPath(new URL('.venv/bin/python',root)), ['-c', `
import json,sys
from jinja2 import Environment,ChoiceLoader,FileSystemLoader,DictLoader
env=Environment(loader=ChoiceLoader([DictLoader({'base.html':'<!doctype html><link rel="stylesheet" href="/static/css/bootstrap.min.css"><link rel="stylesheet" href="/static/css/design-tokens.css">{% block extra_css %}{% endblock %}<main style="margin:24px">{% block content %}{% endblock %}</main>{% block extra_js %}{% endblock %}'}),FileSystemLoader('app/templates')]),autoescape=True)
options=json.loads(sys.argv[1])
print(env.get_template('proposals/form.html').render(proposal={'businessId':'fixture','version':1,'status':'DRAFT'} if options.get('edit') else None, form={'title':'保留手工标题'}, errors=options.get('errors',{}), source_types=['IDEA'], request={'method':'POST' if options.get('errors') else 'GET'}, csrf_token=lambda:'fixture-token',url_for=lambda *a,**k:'/proposals',config={'AI_ASSISTANT_AVAILABLE':False}))
`, JSON.stringify(options)], {cwd:fileURLToPath(root),encoding:'utf8'});
async function fixture(options) {
  const browser=await chromium.launch({headless:true});
  const page=await browser.newPage({viewport:{width:1280,height:720}});
  const errors=[],requests=[];
  page.on('pageerror',e=>errors.push(e.message));
  const html=render(options);
  await page.route('**/*',async route=>{
    const url=new URL(route.request().url());
    if(url.origin!=='http://r4-form.fixture'){requests.push(url.href);return route.abort();}
    if(url.pathname==='/')return route.fulfill({body:html,contentType:'text/html; charset=utf-8'});
    if(url.pathname.startsWith('/static/'))return route.fulfill({body:await readFile(new URL('app'+url.pathname,root)),contentType:url.pathname.endsWith('.css')?'text/css':'image/svg+xml'});
    requests.push(url.href);return route.abort();
  });
  await page.goto('http://r4-form.fixture/');
  return {browser,page,errors,requests};
}
test('server error determines focus, not an earlier empty required field',async()=>{
  const {browser,page,errors,requests}=await fixture({errors:{sourceSummary:'请填写依据摘要'}});
  try{
    assert.equal(await page.evaluate(()=>document.activeElement.id),'sourceSummary');
    assert.equal(await page.locator('#sourceSummary').getAttribute('aria-invalid'),'true');
    assert.equal(await page.locator('#sourceSummary').getAttribute('aria-describedby'),'sourceSummary-error');
    assert.equal(await page.locator('#sourceSummary-error').textContent(),'请填写依据摘要');
    assert.equal(await page.locator('#title').inputValue(),'保留手工标题');
    assert.deepEqual(errors,[]);assert.deepEqual(requests,[]);
  }finally{await browser.close();}
});
test('nonmodal drawer toggles with keyboard, restores focus and retains manual/source text',async()=>{
  const {browser,page,errors,requests}=await fixture({edit:true});
  try{
    assert.equal(await page.locator('#proposal-assistant').isVisible(),false);
    await page.locator('#assistant-open').focus();await page.keyboard.press('Enter');
    assert.equal(await page.evaluate(()=>document.activeElement.id),'assistant-close');
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(()=>document.activeElement.id),'assistant-source');
    await page.locator('#assistant-source').fill('离线保留输入');
    assert.equal(await page.locator('#assistant-generate').isDisabled(),true);
    await page.locator('#title').focus();assert.equal(await page.evaluate(()=>document.activeElement.id),'title');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#proposal-assistant').isVisible(),false);
    assert.equal(await page.evaluate(()=>document.activeElement.id),'assistant-open');
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('#assistant-source').inputValue(),'离线保留输入');
    assert.equal(await page.locator('#title').inputValue(),'保留手工标题');
    await page.locator('#assistant-close').click();
    assert.equal(await page.evaluate(()=>document.activeElement.id),'assistant-open');
    assert.equal(await page.locator('#assistant-open').getAttribute('aria-expanded'),'false');
    assert.deepEqual(errors,[]);assert.deepEqual(requests,[]);
  }finally{await browser.close();}
});
test('drawer fits all required viewports and scrolls its own contents',async()=>{
  const {browser,page,errors,requests}=await fixture({edit:true});
  const sizes=[];
  try{
    await page.locator('#assistant-open').click();
    for(const [width,height] of [[1440,900],[1366,768],[1280,720]]){
      await page.setViewportSize({width,height});
      const metrics=await page.evaluate(()=>({overflow:document.documentElement.scrollWidth>innerWidth,width:document.querySelector('#proposal-assistant').getBoundingClientRect().width,scroll:getComputedStyle(document.querySelector('.proposal-assistant-body')).overflowY}));
      assert.equal(metrics.overflow,false);assert.equal(metrics.width,width===1440?390:350);assert.equal(metrics.scroll,'auto');
      sizes.push({width,height,...metrics});
    }
    await writeFile(new URL('build/repair-20260909/r4-form-offline.json',root),JSON.stringify({sizes,errors,requests},null,2));
    assert.deepEqual(errors,[]);assert.deepEqual(requests,[]);
  }finally{await browser.close();}
});
