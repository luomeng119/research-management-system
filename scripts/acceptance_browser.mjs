import fs from 'node:fs';
import { chromium, expect } from '@playwright/test';
import { runJourney } from './acceptance_journey.mjs';


function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`Required environment variable is missing: ${name}`);
  return value;
}


let password = '';
for await (const chunk of process.stdin) password += chunk;
password = password.replace(/\r?\n$/, '');
if (!password) throw new Error('Acceptance password was not supplied on stdin.');

const baseURL = required('ACCEPTANCE_BASE_URL');
const baselinePath = required('ACCEPTANCE_BASELINE');
const outboundPath = required('ACCEPTANCE_OUTBOUND');
const resultPath = required('ACCEPTANCE_RESULT');
const username = required('ACCEPTANCE_USERNAME');
const outbound = [];
const browserErrors = [];
const expectedValidationErrors = [];
let status = 'FAILED';
let browser;
let page;
const journey = {};

try {
  const baseline = JSON.parse(fs.readFileSync(baselinePath, 'utf8'));
  const ids = baseline.identities;
  const browserEnvironment = {};
  for (const name of ['PATH', 'HOME', 'TMPDIR', 'TMP', 'TEMP', 'LANG', 'LC_ALL']) {
    if (process.env[name]) browserEnvironment[name] = process.env[name];
  }
  browser = await chromium.launch({ env: browserEnvironment });
  const context = await browser.newContext({ baseURL, serviceWorkers: 'block' });
  await context.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (['http:', 'https:'].includes(url.protocol)
        && !['127.0.0.1', 'localhost'].includes(url.hostname)) {
      outbound.push(route.request().url());
      await route.abort('blockedbyclient');
      return;
    }
    await route.continue();
  });
  await context.routeWebSocket(/.*/, async websocket => {
    const url = new URL(websocket.url());
    if (!['127.0.0.1', 'localhost'].includes(url.hostname)) {
      outbound.push(websocket.url());
      await websocket.close({ code: 1008, reason: 'Non-local WebSocket blocked' });
      return;
    }
    websocket.connectToServer();
  });
  page = await context.newPage();
  page.on('pageerror', error => browserErrors.push({ type: 'pageerror', message: error.message }));
  page.on('console', message => {
    if (message.type() !== 'error') return;
    const entry = { type: 'console', message: message.text(), url: message.location().url };
    // The journey deliberately submits one invalid draft to test server validation.
    if (entry.url === new URL('/proposals/new', baseURL).href
        && /^Failed to load resource: the server responded with a status of 422\b/.test(entry.message)) {
      expectedValidationErrors.push(entry);
    } else {
      browserErrors.push(entry);
    }
  });
  context.on('requestfailed', request => {
    const url = new URL(request.url());
    if (['127.0.0.1', 'localhost'].includes(url.hostname)) {
      browserErrors.push({ type: 'requestfailed', url: request.url(), message: request.failure()?.errorText });
    }
  });

  context.on('request', request => {
    const url = new URL(request.url());
    if (['http:', 'https:', 'ws:', 'wss:'].includes(url.protocol)
        && !['127.0.0.1', 'localhost'].includes(url.hostname)) {
      outbound.push(request.url());
    }
  });

  await page.goto('/auth/login');
  await page.locator('input[name="username"]').fill(username);
  await page.locator('input[name="password"]').fill(`${password}-wrong`);
  await page.locator('button[type="submit"]').click();
  if (!/\/auth\/login/.test(page.url())) throw new Error('Invalid password was accepted.');
  await page.locator('input[name="username"]').fill(username);
  await page.locator('input[name="password"]').fill(password);
  password = '';
  await page.locator('button[type="submit"]').click();
  if (/\/auth\/login/.test(page.url())) throw new Error('Acceptance login failed.');

  async function open(path, marker) {
    const response = await page.goto(path);
    if (!response) throw new Error(`Route returned no response: ${path}`);
    if (response.status() >= 400) {
      const body = (await response.text()).replace(/\s+/g, ' ').slice(0, 500);
      throw new Error(`Route failed (${response.status()}): ${path}: ${body}`);
    }
    if (/\/auth\/login/.test(page.url())) throw new Error(`Route redirected to login: ${path}`);
    await page.getByText(marker, { exact: false }).filter({ visible: true }).first().waitFor({ state: 'visible', timeout: 10000 });
  }

  await open(`/proposals/${encodeURIComponent(ids.proposalBusinessId)}`, '提案内容');
  await page.getByRole('heading', { name: 'V1验收-智能保障设备适配研究-20260904', exact: true }).waitFor({ state: 'visible' });
  await page.getByText('v1-proposal-source.txt', { exact: true }).waitFor({ state: 'visible' });
  await page.getByText('立项', { exact: true }).first().waitFor({ state: 'visible' });

  const overviewPath = `/projects/${encodeURIComponent(ids.projectRegistryId)}/overview`;
  const overviewResponse = await page.goto(overviewPath);
  if (!overviewResponse || overviewResponse.status() >= 400) throw new Error(`Route failed: ${overviewPath}`);
  if (/\/auth\/login/.test(page.url())) throw new Error(`Route redirected to login: ${overviewPath}`);
  await page.getByRole('heading', { name: 'V1验收-智能保障设备适配研究-20260904', exact: true }).waitFor({ state: 'visible' });
  await page.locator('#projectStatusLabel').filter({ hasText: '已结题' }).waitFor({ state: 'visible' });
  await page.locator('.project-kicker').filter({ hasText: '一般科研项目' }).waitFor({ state: 'visible' });
  await page.getByText('V1验收-阶段进展记录-20260904', { exact: true }).first().waitFor({ state: 'visible' });

  const projectResponse = await page.request.get(`/api/projects/${encodeURIComponent(ids.projectRegistryId)}`);
  if (!projectResponse.ok()) throw new Error('Project detail API failed.');
  const projectRecord = await projectResponse.json();
  if (projectRecord.businessId !== ids.projectBusinessId
      || projectRecord.sourceProposalId !== ids.proposalInternalId) {
    throw new Error('Project-to-proposal relationship mismatch.');
  }

  const projectFilesResponse = await page.request.get(
    `/api/files?objectType=PROJECT&objectId=${encodeURIComponent(ids.projectBusinessId)}`,
  );
  if (!projectFilesResponse.ok()) throw new Error('Project attachment API failed.');
  const projectFiles = (await projectFilesResponse.json()).files || [];
  if (!projectFiles.some(file => file.fileId === ids.projectFileId
      && file.originalName === 'v1-project-record.txt' && Number(file.versionNo) === 1)) {
    throw new Error('Project controlled attachment mismatch.');
  }

  await page.getByRole('tab', { name: /成果/ }).click();
  await page.getByText('V1验收-适配研究报告-20260904', { exact: true }).waitFor({ state: 'visible' });
  await page.getByRole('tab', { name: '结题', exact: true }).click();
  await page.getByText('完成研究目标并形成可复核报告', { exact: true }).waitFor({ state: 'visible' });

  await open(`/projects/detail/${encodeURIComponent(ids.projectBusinessId)}`, 'V1验收-科研保障设备-20260904');
  await page.getByText('一号科研实验室', { exact: true }).waitFor({ state: 'visible' });
  await open(`/experts/groups/edit/${encodeURIComponent(ids.expertGroupId)}`, 'V1验收-科研论证专家组-20260904');
  const memberRow = page.locator('.card', { hasText: '已选择成员' }).locator('tbody tr').filter({ hasText: '张老师' });
  await memberRow.getByText('张老师', { exact: true }).waitFor({ state: 'visible' });
  await memberRow.getByText('第一研究室', { exact: true }).waitFor({ state: 'visible' });
  await memberRow.getByText('科研设备适配', { exact: true }).waitFor({ state: 'visible' });

  await open('/equipment', 'V1验收-科研保障设备-20260904');
  await open('/expense/records', 'V1验收-设备采购登记-20260904');
  await open('/standards/', 'V1验收-科研设备试验记录规范-20260904');
  await open('/templates/', 'V1验收-科研项目记录模板-20260904');
  await open(`/tables/${encodeURIComponent(ids.genericTableId)}`, 'V1验收-科研试验记录表-20260904');
  await page.locator('#tableBody').getByText('V1验收-智能保障设备适配研究-20260904', { exact: true }).waitFor({ state: 'visible' });
  await open('/utils/', '文档校对');
  await page.locator('.container').getByRole('button', { name: '当前未启用', exact: true }).waitFor({ state: 'visible' });

  await runJourney(page, journey);
  // Retained editor: exercise real controls, persistence and a fresh history view.
  page.on('dialog', dialog => dialog.accept());
  await page.goto('/template/edit/research');
  const staleTemplate = await context.newPage();
  const expectedTemplateConflicts = [];
  staleTemplate.on('pageerror', error => browserErrors.push({ type: 'pageerror', message: error.message }));
  staleTemplate.on('console', message => {
    if (message.type() !== 'error') return;
    const entry = { type: 'console', message: message.text(), url: message.location().url };
    if (entry.url === new URL('/template/api/save', baseURL).href
        && /^Failed to load resource: the server responded with a status of 409\b/.test(entry.message)) {
      expectedTemplateConflicts.push(entry);
    } else browserErrors.push(entry);
  });
  await staleTemplate.goto('/template/edit/research');
  const staleStructure = await staleTemplate.locator('#template-structure').inputValue();
  await staleTemplate.locator('#template-name').fill('演练：未保存的过期修改');
  await page.locator('#template-name').fill('演练：设备论证模板');
  await page.locator('#template-structure').fill(JSON.stringify({ chapters: [
    { id: 'note', title: '研究说明', type: 'text' },
    { id: 'ref', title: '设备引用', type: 'device_ref' },
    { id: 'list', title: '设备清单', type: 'device_list' },
    { id: 'ref2', title: '第二设备引用', type: 'device_ref' },
    { id: 'list2', title: '第二设备清单', type: 'device_list' },
  ] }));
  const [templateResponse] = await Promise.all([
    page.waitForResponse(response => response.url().endsWith('/template/api/save') && response.request().method() === 'POST'),
    page.locator('#template-editor button[type="submit"]').click(),
  ]);
  expect(templateResponse.status()).toBe(200);
  await expect(page.locator('#template-result')).toContainText('成功');
  const winningStructure = await page.locator('#template-structure').inputValue();
  const [conflictResponse] = await Promise.all([
    staleTemplate.waitForResponse(response => response.url().endsWith('/template/api/save') && response.request().method() === 'POST'),
    staleTemplate.locator('#template-editor button[type="submit"]').click(),
  ]);
  expect(conflictResponse.status()).toBe(409);
  await expect(staleTemplate.locator('#template-result')).toContainText('尚未保存');
  await expect(staleTemplate.locator('#template-name')).toHaveValue('演练：未保存的过期修改');
  await expect(staleTemplate.locator('#template-structure')).toHaveValue(staleStructure);
  await staleTemplate.reload();
  await expect(staleTemplate.locator('#template-name')).toHaveValue('演练：设备论证模板');
  const reloadedStructure = JSON.parse(await staleTemplate.locator('#template-structure').inputValue());
  expect(reloadedStructure.chapters).toEqual(JSON.parse(winningStructure).chapters);
  expect(expectedTemplateConflicts).toHaveLength(1);
  journey.templateConflict = { status: 409, inputPreserved: true, winnerReloaded: true, expectedConsoleErrors: expectedTemplateConflicts };
  await staleTemplate.close();
  await page.goto(`/argumentation/research/${encodeURIComponent(ids.projectBusinessId)}`);
  await page.locator('.ql-editor').fill('演练：原有论证设备核对');
  await page.locator('[data-chapter-id="list"]').getByRole('button', { name: /添加设备到清单/ }).click();
  const listCheckbox = page.locator('#deviceListModal .device-list-check').first();
  const deviceId = await listCheckbox.getAttribute('value');
  const deviceName = await listCheckbox.getAttribute('data-name');
  await listCheckbox.check();
  await page.getByRole('button', { name: '确认添加', exact: true }).click();
  await page.locator('[data-chapter-id="ref"]').getByRole('button', { name: '选择设备', exact: true }).click();
  await page.locator(`#deviceModal .device-checkbox[value="${deviceId}"]`).check();
  await page.locator('#deviceModal').getByRole('button', { name: '确定', exact: true }).click();
  for (const [chapter, modal, checkbox, button] of [
    ['ref2', 'deviceModal', '.device-checkbox', '选择设备'],
    ['list2', 'deviceListModal', '.device-list-check', '添加设备到清单'],
  ]) {
    await page.locator(`[data-chapter-id="${chapter}"]`).getByRole('button', { name: new RegExp(button) }).click();
    await expect(page.locator(`#${modal} ${checkbox}:checked`)).toHaveCount(0);
    await page.locator(`#${modal}`).getByRole('button', { name: '取消', exact: true }).click();
  }
  await page.locator('button[onclick="saveDocument()"]').click();
  const [savedResponse] = await Promise.all([
    page.waitForResponse(response => response.url().endsWith('/argumentation/save') && response.request().method() === 'POST'),
    page.locator('#saveModal').getByRole('button', { name: '确认保存', exact: true }).click(),
  ]);
  expect(savedResponse.status()).toBe(200);
  const savedDocument = await savedResponse.json();
  await page.waitForLoadState('networkidle');
  await page.reload();
  await expect(page.locator('.ql-editor')).toContainText('演练：原有论证设备核对');
  await expect(page.locator('#device_ref_ref')).toHaveValue(deviceId);
  await expect(page.locator('#device_list_list')).toHaveValue(deviceId);
  await expect(page.locator('#device_info_ref')).toContainText(deviceName);
  await expect(page.locator('#device_list_content_list')).toContainText(deviceName);
  await page.locator('[data-chapter-id="ref"]').getByRole('button', { name: '选择设备', exact: true }).click();
  await expect(page.locator(`#deviceModal .device-checkbox[value="${deviceId}"]`)).toBeChecked();
  await page.locator('#deviceModal').getByRole('button', { name: '取消', exact: true }).click();
  await page.screenshot({ path: `${resultPath}.argumentation-current.png`, fullPage: true });
  await page.locator('[data-chapter-id="list"]').getByRole('button', { name: /添加设备到清单/ }).click();
  await expect(page.locator(`#deviceListModal .device-list-check[value="${deviceId}"]`)).toBeChecked();
  await page.locator(`#deviceListModal .device-list-check[value="${deviceId}"]`).uncheck();
  await page.getByRole('button', { name: '确认添加', exact: true }).click();
  await page.locator('button[onclick="saveDocument()"]').click();
  const [clearedResponse] = await Promise.all([
    page.waitForResponse(response => response.url().endsWith('/argumentation/save') && response.request().method() === 'POST'),
    page.locator('#saveModal').getByRole('button', { name: '确认保存', exact: true }).click(),
  ]);
  expect(clearedResponse.status()).toBe(200);
  const clearedDocument = await clearedResponse.json();
  await page.waitForLoadState('networkidle');
  await page.reload();
  await expect(page.locator('#device_list_list')).toHaveValue('');
  await expect(page.locator('#device_ref_ref')).toHaveValue(deviceId);
  await page.locator('.dropdown-toggle').filter({ hasText: '版本 v' }).click();
  await page.locator('a.dropdown-item').filter({ hasText: 'v1 -' }).click();
  await expect(page.locator('p[role="status"].alert-info')).toContainText('仅供查看');
  await expect(page.locator('.ql-editor')).toHaveAttribute('contenteditable', 'false');
  await expect(page.locator('.ql-toolbar')).toHaveCount(0);
  await expect(page.getByRole('button', { name: /编辑章节/ })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '选择设备', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: /添加设备到清单/ })).toHaveCount(0);
  await expect(page.locator('#device_ref_ref')).toHaveValue(deviceId);
  await expect(page.locator('#device_info_ref')).toContainText(deviceName);
  journey.retainedArgumentation = { savedDocument, deviceId, deviceName, clearedVersion: clearedDocument, historyUrl: page.url() };
  await page.screenshot({ path: `${resultPath}.argumentation-history.png`, fullPage: true });
  await page.goto(`/argumentation/research/${encodeURIComponent(ids.projectBusinessId)}`);
  const probeName = '<b data-device-probe>演练设备标签</b>';
  await page.locator('[data-chapter-id="ref2"]').getByRole('button', { name: '选择设备', exact: true }).click();
  await page.locator('#deviceModal .device-checkbox[data-model="V1-HTML-PROBE"]').check();
  await expect(page.locator('#selectedDevicesBody')).toContainText(probeName);
  await expect(page.locator('[data-device-probe]')).toHaveCount(0);
  await page.locator('#deviceModal').getByRole('button', { name: '确定', exact: true }).click();
  await expect(page.locator('#device_info_ref2')).toContainText(probeName);
  await page.locator('[data-chapter-id="list2"]').getByRole('button', { name: /添加设备到清单/ }).click();
  await page.locator('#deviceListModal .device-list-check[data-model="V1-HTML-PROBE"]').check();
  await page.getByRole('button', { name: '确认添加', exact: true }).click();
  await expect(page.locator('#device_list_content_list2')).toContainText(probeName);
  await expect(page.locator('[data-device-probe]')).toHaveCount(0);
  journey.retainedArgumentation.plainTextDeviceLabels = true;
  if (outbound.length !== 0) throw new Error('Non-local browser requests were observed.');
  if (browserErrors.length !== 0) throw new Error(`Browser errors observed: ${JSON.stringify(browserErrors)}`);
  if (expectedValidationErrors.length > 1) throw new Error('Unexpected repeated validation response.');
  status = 'PASSED';
} finally {
  password = '';
  try {
    if (page && status !== 'PASSED') {
      try {
        fs.writeFileSync(`${resultPath}.failure.html`, await page.content());
        await page.screenshot({ path: `${resultPath}.failure.png`, fullPage: true });
      } catch (error) {
        // Supplemental capture must not hide the original failure or prevent cleanup.
        console.error(`Failure capture unavailable: ${error.message}`);
      }
    }
  } finally {
    try {
      if (browser) await browser.close();
    } finally {
      fs.writeFileSync(outboundPath, JSON.stringify(outbound, null, 2));
      fs.writeFileSync(resultPath, JSON.stringify({ status, journey, browserErrors, expectedValidationErrors }, null, 2));
    }
  }
}
