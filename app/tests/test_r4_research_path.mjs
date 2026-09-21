// Isolated synthetic UI fixture: no application server, production data, or DB access.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { chromium } from '@playwright/test';

const root = new URL('../../', import.meta.url);
const assets = Object.fromEntries(await Promise.all(['vendor/g6.min.js', 'js/research-path.js', 'css/research-path.css'].map(async name => [name, await readFile(new URL(`app/static/${name}`, root), 'utf8')])));
const tree = { id: 'fixture-root', data: { title: '合成测试项目', status: 'active' }, children: Array.from({ length: 8 }, (_, i) => ({ id: `fixture-${i}`, data: { title: `测试阶段 ${i}`, status: i ? 'pending' : 'active' } })) };
async function fixture(options = {}) {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1100, height: 700 }, reducedMotion: options.reduced ? 'reduce' : 'no-preference' });
  let g6Requests = 0;
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.startsWith('/static/')) {
      const name = path.slice('/static/'.length);
      if (name === 'vendor/g6.min.js' && ++g6Requests <= Number(options.failG6 || 0)) return route.abort();
      return route.fulfill({ body: assets[name], contentType: name.endsWith('.css') ? 'text/css' : 'text/javascript' });
    }
    if (path.startsWith('/api/')) return route.fulfill({ json: { readOnly: true, tree } });
    return route.fulfill({ contentType: 'text/html; charset=utf-8', body: `<!doctype html><link rel="stylesheet" href="/static/css/research-path.css"><body style="margin:0"><main class="lifecycle-page" data-project-id="fixture"><button id="togglePathMotion"><i></i><span></span></button><button id="fitResearchPath">适应视图</button><div class="research-path-canvas-wrap"><div id="researchPathCanvas"></div></div><div style="height:1200px">Synthetic fixture</div></main><script>window.__G6_LOAD_FAILED__=false</script><script defer src="/static/vendor/g6.min.js" onerror="window.__G6_LOAD_FAILED__=true"></script><script defer src="/static/js/research-path.js"></script>` });
  });
  await page.goto('http://r4-fixture.local/');
  await page.evaluate(() => window.ResearchPath.mount());
  return { browser, page, errors, requests: () => g6Requests };
}
const camera = page => page.evaluate(() => ({ zoom: ResearchPath.getGraph().getZoom(), position: ResearchPath.getGraph().getPosition() }));
test('R4 real pointer drag, wheel zoom, and fit restore camera without page scroll', async () => {
  const { browser, page, errors } = await fixture();
  try {
    const initial = await camera(page);
    const box = await page.locator('#researchPathCanvas').boundingBox();
    await page.mouse.move(box.x + 15, box.y + 15);
    await page.mouse.down();
    await page.waitForTimeout(150);
    await page.mouse.move(box.x + 120, box.y + 95, { steps: 15 });
    await page.mouse.up();
    await page.waitForTimeout(350);
    const dragged = await camera(page);
    assert.ok(Math.abs(dragged.position[0] - initial.position[0]) > 30, 'pointer drag must translate camera');
    await page.mouse.wheel(0, -240);
    await page.waitForTimeout(500);
    const zoomed = await camera(page);
    assert.ok(Math.abs(zoomed.zoom - dragged.zoom) > 0.01, 'wheel must change zoom');
    assert.equal(await page.evaluate(() => scrollY), 0, 'canvas wheel must not scroll page');
    await page.locator('#fitResearchPath').click();
    await page.waitForTimeout(600);
    const fitted = await camera(page);
    assert.ok(Math.abs(fitted.zoom - initial.zoom) < 0.02, 'fit restores zoom');
    assert.ok(Math.abs(fitted.position[0] - initial.position[0]) < 2, 'fit restores horizontal position');
    assert.ok(Math.abs(fitted.position[1] - initial.position[1]) < 2, 'fit restores vertical position');
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});
test('R4 fitted node borders have visible canvas clearance', async () => {
  const { browser, page } = await fixture();
  try {
    await page.locator('#researchPathCanvas').evaluate(el => { el.style.width = '398px'; });
    await page.evaluate(async () => { const g=ResearchPath.getGraph(); g.resize(); await ResearchPath.fit(); });
    const bounds = await page.evaluate(() => {
      const g=ResearchPath.getGraph();
      return g.getNodeData().map(n => {const b=g.getElementRenderBounds(n.id); return {id:n.id,min:g.getViewportByCanvas(b.min),max:g.getViewportByCanvas(b.max)};});
    });
    for (const b of bounds) {
      assert.ok(b.min[0] >= 8, `${b.id}: left border needs clearance (${b.min[0]})`);
      assert.ok(b.max[0] <= 390, `${b.id}: right border needs clearance (${b.max[0]})`);
    }
  } finally { await browser.close(); }
});
test('R4 failed local G6 retries in place and remains operable', async () => {
  const { browser, page, errors, requests } = await fixture({ failG6: true });
  try {
    assert.equal(await page.locator('.path-error').count(), 1);
    await page.getByRole('button', { name: '重新加载研究路径' }).click();
    await page.waitForFunction(() => !!window.ResearchPath.getGraph()?.getNodeData('fixture-root'), null, { timeout: 2500 });
    await page.evaluate(() => ResearchPath.fit());
    assert.equal(requests(), 2);
    assert.equal(await page.locator('.path-error').count(), 0);
    assert.equal(await page.evaluate(() => __G6_LOAD_FAILED__), false);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});
test('R4 retry accepts a late local component despite a stale failure flag', async () => {
  const { browser, page, errors, requests } = await fixture({ failG6: true });
  try {
    await page.addScriptTag({ content: assets['vendor/g6.min.js'] });
    assert.equal(await page.evaluate(() => __G6_LOAD_FAILED__), true);
    await page.getByRole('button', { name: '重新加载研究路径' }).click();
    await page.waitForFunction(() => !!window.ResearchPath.getGraph()?.getNodeData('fixture-root'), null, { timeout: 2500 });
    assert.equal(requests(), 1, 'available local component need not be downloaded again');
    assert.equal(await page.evaluate(() => __G6_LOAD_FAILED__), false);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});
test('R4 repeated component failure offers another usable retry', async () => {
  const { browser, page, errors, requests } = await fixture({ failG6: 2 });
  try {
    await page.getByRole('button', { name: '重新加载研究路径' }).click();
    await page.waitForFunction(() => !document.querySelector('.path-error button')?.disabled);
    assert.equal(requests(), 2);
    await page.getByRole('button', { name: '重新加载研究路径' }).click();
    await page.waitForFunction(() => !!window.ResearchPath.getGraph()?.getNodeData('fixture-root'));
    await page.evaluate(() => ResearchPath.fit());
    assert.equal(requests(), 3);
    assert.equal(await page.locator('.path-error').count(), 0);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});
test('R4 reduced motion and pause/resume preserved', async () => {
  const { browser, page, errors } = await fixture({ reduced: true });
  try {
    assert.equal(await page.evaluate(() => ResearchPath.getMotionEnabled()), false);
    await page.locator('#togglePathMotion').click();
    await page.waitForFunction(() => document.querySelector('#togglePathMotion').getAttribute('aria-pressed') === 'false');
    assert.equal(await page.evaluate(() => ResearchPath.getMotionEnabled()), true);
    await page.locator('#togglePathMotion').click();
    await page.waitForFunction(() => document.querySelector('#togglePathMotion').getAttribute('aria-pressed') === 'true');
    assert.equal(await page.evaluate(() => ResearchPath.getMotionEnabled()), false);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});
