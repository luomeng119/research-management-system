import { createHash, randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { expect } from '@playwright/test';

function verifyLegacyRecovery(runtimeRoot, files) {
  const recovery = files.legacyRecovery;
  expect(recovery.id).toMatch(/^[a-f0-9]{32}$/);
  const root = path.join(runtimeRoot, 'data', 'legacy-folder-archive', recovery.id);
  const manifest = JSON.parse(fs.readFileSync(path.join(root, 'recovery.json'), 'utf8'));
  expect(manifest.kind).toBe('LEGACY_PROJECT_FILE_DELETE');
  expect(manifest.projectId).toBe(files.businessId);
  expect(manifest.category).toBe(files.category);
  expect(manifest.filepath).toBe('任务输入文件/browser-legacy.txt');
  expect(manifest.files.map(item => item.sourceRelativePath).sort()).toEqual(Object.keys(recovery.entries).sort());
  for (const item of manifest.files) {
    const bytes = Buffer.from(recovery.entries[item.sourceRelativePath], 'hex');
    expect(item.payloadRelativePath).toBe(`payload/${item.sourceRelativePath}`);
    expect(item.sha256).toBe(createHash('sha256').update(bytes).digest('hex'));
    expect(item.sizeBytes).toBe(bytes.length);
    expect(fs.readFileSync(path.join(root, item.payloadRelativePath))).toEqual(bytes);
    expect(fs.existsSync(path.join(runtimeRoot, 'uploads', files.businessId, item.sourceRelativePath))).toBe(false);
  }
}

export async function runProjectFiles(page, evidence, runtimeRoot) {
  const record = await page.request.get(`/api/projects/${evidence.projectId}`);
  expect(record.ok()).toBeTruthy();
  const project = await record.json();
  const businessId = project.businessId;
  const prefix = { GENERAL_RESEARCH: 'projects', SECURITY_CONFIDENTIALITY: 'security_projects',
    CRYPTO_APPLICATION: 'crypto_projects' }[project.category];
  expect(prefix).toBeTruthy();
  await page.goto(`/${prefix}/detail/${businessId}`);
  const payload = Buffer.from('浏览器真实附件演练\n', 'utf8');
  async function reloadAfter(action) {
    await Promise.all([page.waitForEvent('load'), action()]);
  }
  for (const name of ['browser-selected.txt', 'browser-unselected.txt']) {
    await reloadAfter(() => page.locator('input[type="file"][data-folder="任务输入文件"]').setInputFiles({
      name, mimeType: 'text/plain', buffer: payload,
    }));
    await expect(page.locator(`.btn-del[data-path="任务输入文件/${name}"]`)).toBeVisible();
  }
  await page.locator('.btn-rename[data-path="任务输入文件/browser-selected.txt"]').click();
  await page.locator('#newFileName').fill('browser-renamed.txt');
  await reloadAfter(() => page.locator('#renameForm button[type="submit"]').click());
  const filePath = '任务输入文件/browser-renamed.txt';
  await expect(page.locator(`.btn-del[data-path="${filePath}"]`)).toBeVisible();
  await expect(page.locator('.btn-del[data-path="任务输入文件/browser-selected.txt"]')).toHaveCount(0);
  await page.locator(`.file-cb[value="${filePath}"]`).check();
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.locator('button[onclick="exportSelected()"]').click(),
  ]);
  expect(await download.failure()).toBeNull();
  const stream = await download.createReadStream();
  const chunks = [];
  for await (const chunk of stream) chunks.push(chunk);
  const zip = Buffer.concat(chunks);
  const contents = JSON.parse(execFileSync('.venv/bin/python', ['-c',
    'import io,json,sys,zipfile; z=zipfile.ZipFile(io.BytesIO(sys.stdin.buffer.read())); print(json.dumps({n:z.read(n).hex() for n in z.namelist()}))',
  ], { input: zip, encoding: 'utf8' }));
  expect(contents).toEqual({ [filePath]: payload.toString('hex') });
  await reloadAfter(() => page.locator(`.btn-del[data-path="${filePath}"]`).click());
  await expect(page.locator(`.file-cb[value="${filePath}"]`)).toHaveCount(0);
  await expect(page.locator('.file-cb[value="任务输入文件/browser-unselected.txt"]')).toBeVisible();
  const deletedFolder = '任务输入文件/浏览器目录演练';
  await page.locator('.btn-subfolder[data-folder="任务输入文件"]').click();
  await page.locator('#folderForm input[name="folder_name"]').fill('浏览器目录演练');
  const [creation] = await Promise.all([
    page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname.includes(`/create_folder/${businessId}`)),
    page.waitForEvent('load'),
    page.locator('#folderForm button[type="submit"]').click(),
  ]);
  expect(new URL(creation.url()).pathname).toBe(`/${prefix}/create_folder/${businessId}`);
  expect(creation.status()).toBe(200);
  await page.locator('.folder-cb[value="任务输入文件"]').locator('..').locator('.toggle-icon').click();
  await expect(page.locator(`.folder-cb[value="${deletedFolder}"]`)).toBeVisible();
  await reloadAfter(() => page.locator(`input[type="file"][data-folder="${deletedFolder}"]`).setInputFiles({
    name: 'directory-content.txt', mimeType: 'text/plain', buffer: payload,
  }));
  await page.locator('.folder-cb[value="任务输入文件"]').locator('..').locator('.toggle-icon').click();
  await expect(page.locator(`.file-cb[value="${deletedFolder}/directory-content.txt"]`)).toBeVisible();
  await page.locator('.folder-cb[value="任务输入文件"]').check();
  await page.locator(`.folder-cb[value="${deletedFolder}"]`).uncheck();
  await expect(page.locator('.folder-cb[value="任务输入文件"]')).not.toBeChecked();
  const [filteredDownload] = await Promise.all([
    page.waitForEvent('download'),
    page.locator('button[onclick="exportSelected()"]').click(),
  ]);
  expect(await filteredDownload.failure()).toBeNull();
  const filteredChunks = [];
  for await (const chunk of await filteredDownload.createReadStream()) filteredChunks.push(chunk);
  const filteredContents = JSON.parse(execFileSync('.venv/bin/python', ['-c',
    'import io,json,sys,zipfile; z=zipfile.ZipFile(io.BytesIO(sys.stdin.buffer.read())); print(json.dumps({n:z.read(n).hex() for n in z.namelist()}))',
  ], { input: Buffer.concat(filteredChunks), encoding: 'utf8' }));
  expect(filteredContents).toEqual({ '任务输入文件/browser-unselected.txt': payload.toString('hex') });
  const [deletion] = await Promise.all([
    page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname === `/${prefix}/delete_folder/${businessId}`),
    page.waitForEvent('load'),
    page.locator(`.btn-delete-folder[data-folder="${deletedFolder}"]`).click(),
  ]);
  expect(deletion.status()).toBe(200);
  await expect(page.locator(`.folder-cb[value="${deletedFolder}"]`)).toHaveCount(0);
  await expect(page.locator(`.file-cb[value="${deletedFolder}/directory-content.txt"]`)).toHaveCount(0);
  await expect(page.locator('.file-cb[value="任务输入文件/browser-unselected.txt"]')).toBeVisible();
  // Explicit legacy fixture preparation, not a UI upload or real business material.
  expect(businessId).toMatch(/^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$/);
  const legacyEntries = {
    '任务输入文件/browser-legacy.txt': Buffer.from('旧文件浏览器删除演练正文\n'),
    '任务输入文件/.history/browser-legacy_v1.txt': Buffer.from('旧文件浏览器删除演练历史\n'),
    '任务输入文件/.history/browser-legacy_versions.json': Buffer.from(JSON.stringify({
      'browser-legacy.txt': [{ version: 1, original_name: 'browser-legacy.txt' }],
    })),
  };
  for (const [relative, bytes] of Object.entries(legacyEntries)) {
    const target = path.join(runtimeRoot, 'uploads', businessId, relative);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, bytes, { flag: 'wx' });
  }
  await page.reload();
  const legacyButton = page.locator('.btn-del[data-path="任务输入文件/browser-legacy.txt"]');
  await expect(legacyButton).toBeVisible();
  await expect(legacyButton).toHaveAttribute('data-file-id', '');
  await page.locator('.file-cb[value="任务输入文件/browser-legacy.txt"]').locator('..').locator('a[onclick]').click();
  await expect(page.locator('#previewPanelBody')).toContainText('旧文件浏览器删除演练正文');
  await page.locator('#previewPanel button[onclick="closePreview()"]', { hasText: '' }).click();
  const [legacyResult] = await Promise.all([
    page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname === `/${prefix}/delete_file/${businessId}`).then(async response => {
      expect(response.status()).toBe(200);
      return response.json();
    }),
    page.waitForEvent('load'), legacyButton.click(),
  ]);
  expect(legacyResult.success).toBe(true);
  const legacyRecovery = { id: legacyResult.recoveryId, fixture: true,
    entries: Object.fromEntries(Object.entries(legacyEntries).map(([name, bytes]) => [name, bytes.toString('hex')])) };
  verifyLegacyRecovery(runtimeRoot, { businessId, category: project.category, legacyRecovery });
  await expect(page.locator('.file-cb[value="任务输入文件/browser-legacy.txt"]')).toHaveCount(0);
  evidence.projectFiles = { businessId, prefix, category: project.category, selectedArchiveEntries: Object.keys(contents),
    sha256: createHash('sha256').update(payload).digest('hex'), renamed: true, deleted: true,
    unrelatedPreserved: true, deletedFolder, legacyRecovery };
}

export async function verifyRestoredProjectFiles(page, evidence, runtimeRoot) {
  const files = evidence.projectFiles;
  await page.goto(`/${files.prefix}/detail/${files.businessId}`);
  verifyLegacyRecovery(runtimeRoot, files);
  await expect(page.locator('.file-cb[value="任务输入文件/browser-legacy.txt"]')).toHaveCount(0);
  await expect(page.locator('.file-cb[value="任务输入文件/browser-renamed.txt"]')).toHaveCount(0);
  await expect(page.locator(`.folder-cb[value="${files.deletedFolder}"]`)).toHaveCount(0);
  await expect(page.locator(`.file-cb[value="${files.deletedFolder}/directory-content.txt"]`)).toHaveCount(0);
  const retained = page.locator('.file-cb[value="任务输入文件/browser-unselected.txt"]');
  await expect(retained).toBeVisible();
  const [download] = await Promise.all([
    page.waitForEvent('download'), retained.locator('..').locator('a[title="下载"]').click(),
  ]);
  expect(await download.failure()).toBeNull();
  const stream = await download.createReadStream();
  const hash = createHash('sha256');
  for await (const chunk of stream) hash.update(chunk);
  expect(hash.digest('hex')).toBe(files.sha256);
  return download.url();
}

// Every business write below is submitted by the real page. API calls only read.
export async function runJourney(page, evidence) {
  const title = `页面演练-科研设备适配-${randomUUID().slice(0, 8)}`;
  const date = new Date().toISOString().slice(0, 10);
  const attachment = Buffer.from(`演练依据：${title}\n`, 'utf8');
  Object.assign(evidence, {
    title, steps: [], attachmentName: 'ui-research-source.txt',
    attachmentSha256: createHash('sha256').update(attachment).digest('hex'),
    progressSummary: '完成适配实验并记录结果',
    outputTitle: `${title}-研究报告`, closureSummary: '完成研究目标并整理报告及记录',
    proposalFields: {
      sourceType: 'IDEA',
      sourceSummary: '张老师提出科研设备接口适配想法并整理已有实验材料作为依据',
      researchProblem: '不同科研设备的数据接口尚未完成适配验证',
      objectives: '验证接口兼容性并形成实验报告',
      researchContent: '开展接口对照、适配实验和结果整理',
      expectedOutcomes: '适配实验报告及实验记录',
    },
  });

  async function submit(button, pathname, expectedStatus = 302) {
    const pending = page.waitForResponse(response =>
      response.request().method() === 'POST'
      && new URL(response.url()).pathname === pathname);
    await button.click();
    const response = await pending;
    expect(response.status(), `Unexpected response from ${pathname}`).toBe(expectedStatus);
    if (expectedStatus === 302) await page.waitForLoadState('domcontentloaded');
    evidence.steps.push({ path: pathname, status: response.status() });
  }

  await page.goto('/proposals/new');
  await page.locator('#title').fill(title);
  // Exercise server-side validation and retained input before completing the form.
  await submit(page.locator('#proposal-form').getByRole('button', { name: '保存草稿' }), '/proposals/new', 422);
  await expect(page.locator('#title')).toHaveValue(title);
  await page.locator('#sourceType').selectOption(evidence.proposalFields.sourceType);
  for (const [field, value] of Object.entries(evidence.proposalFields)) {
    if (field !== 'sourceType') await page.locator(`#${field}`).fill(value);
  }
  await submit(page.locator('#proposal-form').getByRole('button', { name: '保存草稿' }), '/proposals/new');
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
  evidence.proposalBusinessId = decodeURIComponent(new URL(page.url()).pathname.split('/').pop());
  const proposalPath = `/proposals/${evidence.proposalBusinessId}`;
  await expect(page.locator('.proposal-heading .proposal-status')).toHaveText('草稿');

  await page.locator('#attachment').setInputFiles({
    name: evidence.attachmentName, mimeType: 'text/plain', buffer: attachment,
  });
  const uploadPath = new URL(await page.locator('form.proposal-upload').getAttribute('action'), page.url()).pathname;
  await submit(page.getByRole('button', { name: '上传', exact: true }), uploadPath);
  await expect(page.getByRole('link', { name: evidence.attachmentName, exact: true })).toBeVisible();
  await page.locator('#summary').fill('已核对原始资料，研究方向可开展');
  await page.locator('#argumentationDate').fill(date);
  await page.locator('#conclusion').fill('建议立项');
  await page.locator('#basis').fill('以已有实验材料和研究问题为依据');
  await submit(page.getByRole('button', { name: '保存论证', exact: true }), `${proposalPath}/argumentations`);
  await expect(page.locator('.proposal-heading .proposal-status')).toHaveText('论证中');

  await page.locator('#decision').selectOption('ESTABLISH');
  await page.locator('#decisionDate').fill(date);
  await page.locator('#decisionConclusion').fill('同意立项');
  await page.locator('#decisionBasis').fill('依据已登记论证记录');
  await page.locator('#projectCategory').selectOption('GENERAL_RESEARCH');
  await page.locator('#projectLeader').fill('张老师');
  await submit(page.getByRole('button', { name: '记录处理', exact: true }), `${proposalPath}/decisions`);
  await expect(page.locator('.proposal-heading .proposal-status')).toHaveText('已立项');
  const response = await page.request.get('/api/projects?pageSize=100');
  expect(response.ok()).toBeTruthy();
  const matches = (await response.json()).data.filter(item => item.name === title);
  expect(matches).toHaveLength(1);
  evidence.projectId = matches[0].id;
  const apiPath = `/api/projects/${evidence.projectId}`;
  const detailPath = `/projects/${evidence.projectId}/overview`;
  await page.goto(detailPath);
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();

  async function transition(label, status) {
    await page.getByRole('button', { name: label, exact: true }).click();
    await expect(page.locator('#statusTransitionModal')).toBeVisible();
    await page.locator('#statusTransitionReason').fill('页面演练：记录实际阶段变化');
    await submit(page.locator('#statusTransitionConfirm'), `${apiPath}/status-transitions`, 200);
    await page.waitForLoadState('domcontentloaded');
    await expect(page.locator('#projectStatusLabel')).toHaveText(status);
  }
  async function modalSave(id, suffix) {
    await submit(page.locator(id).getByRole('button', { name: '保存', exact: true }), `${apiPath}/${suffix}`, 201);
    await expect(page.locator(id)).toBeHidden();
  }
  await transition('启动项目', '执行中');
  await page.getByRole('button', { name: /记录进展/ }).click();
  await page.locator('#progressModal [name="summary"]').fill(evidence.progressSummary);
  await page.locator('#progressModal [name="nextActions"]').fill('整理研究成果');
  await modalSave('#progressModal', 'progress');
  await expect(page.getByRole('heading', { name: evidence.progressSummary, exact: true }).first()).toBeVisible();

  await page.getByRole('tab', { name: /成果/ }).click();
  await page.getByRole('button', { name: '登记成果', exact: true }).click();
  await page.locator('#outputModal [name="title"]').fill(evidence.outputTitle);
  await page.locator('#outputModal [name="contributors"]').fill('张老师、李老师');
  await modalSave('#outputModal', 'outputs');
  await page.getByRole('tab', { name: /成果/ }).click();
  await expect(page.getByRole('heading', { name: evidence.outputTitle, exact: true })).toBeVisible();
  await page.getByRole('tab', { name: '概况', exact: true }).click();
  await transition('进入结题', '结题中');
  await page.getByRole('tab', { name: '结题', exact: true }).click();
  await page.getByRole('button', { name: '记录结题', exact: true }).click();
  await page.locator('#closureModal [name="summary"]').fill(evidence.closureSummary);
  await modalSave('#closureModal', 'closure');
  await page.reload();
  await expect(page.locator('#projectStatusLabel')).toHaveText('已结题');
  evidence.status = 'PASSED';
}
