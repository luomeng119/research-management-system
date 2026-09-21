// Render the production modal macro and calls, without app/DB/provider access.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { chromium } from '@playwright/test';

const root = new URL('../../', import.meta.url);
const fields = {
  progressModal: [['recordedAt', '记录时间'], ['status', '状态'], ['summary', '完成情况'], ['issues', '问题与风险'], ['nextActions', '下一步']],
  changeModal: [['decisionDate', '决定日期'], ['changeType', '变更类型'], ['beforeSummary', '变更前摘要'], ['afterSummary', '变更后摘要'], ['basis', '变更原因'], ['decision', '决定']],
};
const render = () => execFileSync(fileURLToPath(new URL('.venv/bin/python', root)), ['-c', `
from pathlib import Path
from jinja2 import Environment
source=Path('app/templates/projects/lifecycle_detail.html').read_text()
start=source.index('{% macro form_modal(')
end=source.index("{{ form_modal('closureModal'", start)
end=source.index(chr(10), end)
print(Environment(autoescape=True).from_string(source[start:end]).render())
`], { cwd: fileURLToPath(root), encoding: 'utf8' });

test('production progress/change modals expose labels and named close controls without changing dismissal', async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    await page.route('**/*', route => route.abort());
    await page.setContent(render());
    await page.addStyleTag({ path: fileURLToPath(new URL('app/static/css/bootstrap.min.css', root)) });
    await page.addScriptTag({ path: fileURLToPath(new URL('app/static/js/bootstrap.bundle.min.js', root)) });
    const failures = [];
    for (const [id, expected] of Object.entries(fields)) {
      await page.evaluate(id => bootstrap.Modal.getOrCreateInstance(document.getElementById(id)).show(), id);
      const modal = page.locator(`#${id}`);
      await modal.waitFor({ state: 'visible' });
      const controls = await modal.locator('input,select,textarea').evaluateAll(elements => elements.map(element => ({
        name: element.name, id: element.id, labels: [...element.labels].map(label => label.textContent.trim()),
      })));
      assert.deepEqual(controls.map(control => control.name), expected.map(([name]) => name));
      for (const [name, label] of expected) {
        const control = controls.find(control => control.name === name);
        if (!control.id || control.labels.length !== 1 || control.labels[0] !== label) failures.push(`${id}.${name}: missing associated ${label}`);
        if (await modal.getByLabel(label, { exact: true }).count() !== 1) failures.push(`${id}.${name}: inaccessible by label`);
      }
      if (await modal.getByRole('button', { name: '关闭', exact: true }).count() !== 1) failures.push(`${id}: close has no accessible name`);
      await page.waitForFunction(id => document.getElementById(id).classList.contains('show'), id);
      // Click the existing dismissal control, even in the red run where its name is absent.
      await modal.locator('.btn-close').click();
      await modal.waitFor({ state: 'hidden' });
    }
    const duplicates = await page.locator('[id]').evaluateAll(elements => {
      const ids = elements.map(element => element.id);
      return ids.filter((id, index) => ids.indexOf(id) !== index);
    });
    assert.deepEqual(duplicates, [], 'IDs must remain unique with all four production modals present');
    assert.deepEqual(failures, [], 'Every existing field and close button must have its expected accessible name');
  } finally {
    await browser.close();
  }
});
