// Read-only live QA: only login and an empty new-proposal validation POST are allowed.
import { chromium } from '@playwright/test';
import { writeFile, mkdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
const base = 'http://127.0.0.1:8893';
const output = new URL('../../build/repair-20260909/', import.meta.url);
const phase = process.env.R4_PHASE || 'green';
const results = { phase, external: [], blockedWrites: [], errors: [], checks: [] };
await mkdir(output, {recursive:true});
const password = process.env.R4_QA_PASSWORD;
const username = process.env.R4_QA_USERNAME || 'qa_walkthrough_20260905';
if (!password) {
  results.status = 'NOT_RUN';
  results.reason = '缺少 R4_QA_PASSWORD；未启动浏览器、未登录、未执行现场检查。';
  await writeFile(new URL(`r4-form-${phase}-not-run.json`, output), JSON.stringify(results, null, 2));
  console.log(JSON.stringify(results, null, 2));
  process.exit(0);
}

const browser = await chromium.launch({headless:true});
const page = await browser.newPage();
let emptyValidation = false;
await page.route('**/*', route => {
  const req = route.request(), url = new URL(req.url());
  if (url.origin !== base) { results.external.push(req.url()); return route.abort(); }
  if (!['GET','HEAD'].includes(req.method()) && !url.pathname.includes('login')) {
    const fields = new URLSearchParams(req.postData() || '');
    const safeEmpty = emptyValidation && url.pathname === '/proposals/new' && req.method() === 'POST'
      && ['title','sourceSummary','researchProblem','objectives','researchContent','expectedOutcomes'].every(name => fields.get(name) === '');
    if (!safeEmpty) { results.blockedWrites.push(url.pathname); return route.abort(); }
    emptyValidation = false;
  }
  return route.continue();
});
page.on('pageerror', error => results.errors.push(error.message));
try {
  await page.goto(base + '/proposals/new');
  await page.locator('[name="username"]').fill(username);
  await page.locator('[name="password"]').fill(password);
  await page.getByRole('button',{name:'登录',exact:true}).click();
  await page.goto(base + '/proposals/new');
  emptyValidation = true;
  const response = page.waitForResponse(r => r.url() === base + '/proposals/new' && r.request().method() === 'POST');
  await page.locator('.proposal-save-action').first().click();
  const status = (await response).status();
  await page.waitForLoadState('load');
  const focus = await page.evaluate(() => ({active:document.activeElement.id || document.activeElement.tagName, first:document.querySelector('#proposal-form .is-invalid')?.id}));
  results.checks.push({name:'422 focuses first server-invalid field',pass:status===422 && focus.active===focus.first,status,...focus});
  await page.screenshot({path:fileURLToPath(new URL(`r4-form-${phase}-422.png`,output))});
  await page.goto(base + '/proposals?status=DRAFT');
  const drafts = page.locator('a.proposal-table-link');
  if (await drafts.count() === 0) {
    const error = new Error('当前没有DRAFT可编辑提案；未创建业务数据，后续抽屉检查未执行。');
    error.code = 'R4_NO_DRAFT';
    throw error;
  }
  const href = await drafts.first().getAttribute('href');
  results.selectedDraft = href;
  await page.goto(base + href);
  const editLinks = page.locator('a[href$="/edit"]');
  if (await editLinks.count() === 0) throw new Error('所选DRAFT详情未提供编辑入口，请检查状态或权限。');
  const edit = await editLinks.first().getAttribute('href');
  for (const [width,height] of [[1440,900],[1366,768],[1280,720]]) {
    await page.setViewportSize({width,height});
    await page.goto(base + edit);
    const trigger = page.locator('#assistant-open');
    const hasTrigger = await trigger.count() === 1;
    results.checks.push({name:`${width}: drawer starts closed with trigger`,pass:hasTrigger && !await page.locator('#proposal-assistant').isVisible()});
    if (!hasTrigger) continue;
    const before = await page.locator('#title').inputValue();
    await trigger.focus(); await page.keyboard.press('Enter');
    const opened = await page.locator('#proposal-assistant').isVisible();
    const closeFocused = await page.locator('#assistant-close').evaluate(el => el===document.activeElement);
    await page.locator('#assistant-source').fill('仅在浏览器中输入，不发送。');
    await page.locator('#title').focus();
    const backgroundAccessible = await page.locator('#title').evaluate(el => el===document.activeElement);
    await page.screenshot({path:fileURLToPath(new URL(`r4-form-${phase}-drawer-${width}.png`,output))});
    await page.keyboard.press('Escape');
    const closed = !await page.locator('#proposal-assistant').isVisible();
    const restored = await trigger.evaluate(el => el===document.activeElement);
    await trigger.click();
    const retained = await page.locator('#assistant-source').inputValue() === '仅在浏览器中输入，不发送。';
    await page.locator('#assistant-close').click();
    const closeButtonRestores = await trigger.evaluate(el => el===document.activeElement);
    results.checks.push({name:`${width}: open close Escape focus restore preserve input and no overflow`,pass:opened && closeFocused && backgroundAccessible && closed && restored && retained && closeButtonRestores && await page.locator('#title').inputValue()===before && !await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),opened,closeFocused,backgroundAccessible,closed,restored,retained,closeButtonRestores});
  }
} catch(error) {
  if (error.code === 'R4_NO_DRAFT') { results.status='NOT_RUN'; results.reason=error.message; }
  else { results.failure=error.stack; }
}
finally {
  if (results.status !== 'NOT_RUN') results.status = results.failure || results.checks.some(c=>!c.pass) || results.external.length || results.errors.length || results.blockedWrites.length ? 'FAIL' : 'PASS';
  const suffix = results.status === 'NOT_RUN' ? '-not-run' : '';
  await writeFile(new URL(`r4-form-${phase}${suffix}.json`,output),JSON.stringify(results,null,2)); await browser.close();
}
console.log(JSON.stringify(results,null,2));
if (results.failure || results.checks.some(c=>!c.pass) || results.external.length || results.errors.length || results.blockedWrites.length) process.exitCode=1;
