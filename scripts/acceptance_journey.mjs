import { createHash, randomUUID } from 'node:crypto';
import { expect } from '@playwright/test';

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
    page.once('dialog', dialog => dialog.accept('页面演练：记录实际阶段变化'));
    await submit(page.getByRole('button', { name: label, exact: true }), `${apiPath}/status-transitions`, 200);
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
