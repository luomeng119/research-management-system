import { test, expect } from '@playwright/test';

async function login(page) {
  await page.goto('/__e2e_login');
  await expect(page.getByRole('heading', { name: '便携式保障设备适配研究' })).toBeVisible();
}

test('只读研究路径支持选择、暂停和适应视图且零外网', async ({ page }) => {
  const externalRequests = [];
  page.on('request', request => {
    const url = new URL(request.url());
    if (!['127.0.0.1', 'localhost'].includes(url.hostname)) externalRequests.push(request.url());
  });
  await login(page);
  await page.getByRole('tab', { name: /研究路径/ }).click();
  await expect(page.locator('#researchPathCanvas canvas').first()).toBeVisible();
  await expect(page.getByText('路径图只读取已有业务记录')).toBeVisible();

  const riskNodeId = await page.evaluate(() => {
    const stack = [window.ResearchPath.getTree()];
    while (stack.length) {
      const node = stack.shift();
      if (node && node.data && node.data.status === 'risk') return node.id;
      stack.push(...(node.children || []));
    }
    return null;
  });
  expect(riskNodeId).toBeTruthy();
  await page.evaluate(id => window.ResearchPath.selectNode(id), riskNodeId);
  await expect(page.locator('#pathInspector')).toContainText('部分字段格式不一致');
  await expect(page.locator('#pathInspector')).toContainText('有风险');

  await page.getByRole('button', { name: '暂停动态线' }).click();
  await expect(page.getByRole('button', { name: '播放动态线' })).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('button', { name: '适应视图' }).click();
  await expect(page.locator('#researchPathCanvas canvas').first()).toBeVisible();
  expect(externalRequests).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
});

test('减少动效时动态线默认暂停', async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 1366, height: 768 }, reducedMotion: 'reduce' });
  const page = await context.newPage();
  await login(page);
  await page.getByRole('tab', { name: /研究路径/ }).click();
  await expect(page.locator('#researchPathCanvas canvas').first()).toBeVisible();
  await expect(page.getByRole('button', { name: '播放动态线' })).toHaveAttribute('aria-pressed', 'true');
  await context.close();
});

test('G6 本地脚本失败只降级图面板，不阻塞项目详情', async ({ page }) => {
  await page.route('**/static/vendor/g6.min.js', route => route.abort());
  await login(page);
  await page.getByRole('tab', { name: /研究路径/ }).click();
  await expect(page.getByText('本地图形组件加载失败')).toBeVisible();
  await expect(page.getByRole('button', { name: '重新加载研究路径' })).toBeVisible();
  await page.getByRole('tab', { name: '概况' }).click();
  await expect(page.getByText('状态只能通过受控动作转换并记录原因')).toBeVisible();
});

test('路径数据失败只降级图面板，不阻塞进展记录', async ({ page }) => {
  await page.route('**/api/projects/*/research-path', route => route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":{"message":"unavailable"}}' }));
  await login(page);
  await page.getByRole('tab', { name: /研究路径/ }).click();
  await expect(page.getByText('研究路径数据暂时无法读取')).toBeVisible();
  await page.getByRole('tab', { name: /进展记录/ }).click();
  await expect(page.locator('[data-panel-content="progress"] h3', { hasText: '历史数据格式核对' })).toBeVisible();
});

test('历史业务日期可手工填写且旧项目文件入口可用', async ({ page }) => {
  await login(page);
  const legacyLink = page.getByRole('link', { name: '原项目文件' });
  await expect(legacyLink).toHaveAttribute('href', '/projects/detail/KY-2026-001');
  const legacyResponse = await page.request.get('/projects/detail/KY-2026-001');
  expect(legacyResponse.status()).toBe(200);

  await page.getByRole('button', { name: '记录进展' }).first().click();
  const form = page.locator('form[data-kind="progress"]');
  await form.locator('[name="recordedAt"]').fill('2025-01-15T10:30');
  await form.locator('[name="summary"]').fill('历史进展补录');
  const expected = await page.evaluate(() => new Date('2025-01-15T10:30').toISOString());
  const requestPromise = page.waitForRequest(request =>
    request.url().includes('/progress') && request.method() === 'POST'
  );
  const responsePromise = page.waitForResponse(response =>
    response.url().includes('/progress') && response.request().method() === 'POST'
  );
  await form.getByRole('button', { name: '保存' }).click();
  const submitted = (await requestPromise).postDataJSON();
  const response = await responsePromise;
  expect(response.status()).toBe(201);
  expect(Date.parse(submitted.recordedAt)).toBe(Date.parse(expected));
  await expect(page.getByRole('heading', { name: '便携式保障设备适配研究' })).toBeVisible();
  await page.getByRole('tab', { name: /进展记录/ }).click();
  await expect(
    page.locator('[data-panel-content="progress"] h3', { hasText: '历史进展补录' }).first()
  ).toBeVisible();
});

test('项目详情可读取设备、设备组和项目之间的真实关系', async ({ page }) => {
  await login(page);
  await page.getByRole('link', { name: '原项目文件' }).click();

  await expect(page).toHaveURL(/\/projects\/detail\/KY-2026-001$/);
  await expect(page.getByText('设备选型 (1)')).toBeVisible();
  const equipmentRow = page.locator('tr[data-group-id]').filter({ hasText: '便携式数据采集终端' });
  await expect(equipmentRow).toContainText('通用设备');
  await expect(equipmentRow.locator('input[type="number"]')).toHaveValue('2');
  await expect(equipmentRow.locator('textarea')).toHaveValue('综合试验室');

  await page.goto('/equipment/groups/?project_id=KY-2026-001');
  const groupRow = page.locator('tbody tr').filter({ hasText: '便携式保障设备适配研究' });
  await expect(groupRow).toContainText('KY-2026-001');
  await expect(groupRow).toContainText('1 台');
});
