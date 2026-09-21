import { test, expect } from '@playwright/test';


test('领导介绍页目录可切换且全程零外网', async ({ page }) => {
  const externalRequests = [];
  const pageErrors = [];
  const failedRequests = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  page.on('requestfailed', request => failedRequests.push(`${request.method()} ${request.url()}`));
  page.on('request', request => {
    const url = new URL(request.url());
    if (!['127.0.0.1', 'localhost'].includes(url.hostname)) externalRequests.push(request.url());
  });

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/system-intro');
  await expect(page.getByRole('heading', { name: /把科研工作从/ })).toBeVisible();

  const sections = [
    ['执行流程', '一条从提案到报告的科研执行流程'],
    ['系统功能', '六类功能支撑完整科研管理'],
    ['数据流程', '文档从进入系统到形成成果的完整来龙去脉'],
    ['本地 AI', 'AI 有明确入口，只在三类业务动作中发挥作用'],
    ['系统架构', '全部能力在本机形成闭环'],
    ['交付结果', '最终交付的是可继续完善的科研成果'],
  ];
  for (const [tabName, heading] of sections) {
    await page.getByRole('tab', { name: new RegExp(tabName) }).click();
    await expect(page.getByRole('heading', { name: heading })).toBeVisible();
  }

  await expect(page.getByRole('link', { name: '进入系统' })).toHaveAttribute('href', '/auth/login');
  expect(externalRequests).toEqual([]);
  expect(pageErrors).toEqual([]);
  expect(failedRequests).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
});
