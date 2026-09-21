import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {test} from 'node:test';
const source = fs.readFileSync(new URL('../static/js/app-shell.js', import.meta.url), 'utf8');
const highlight = source.slice(source.indexOf('    function highlightTree()'), source.indexOf('    function showTreeError('));
const api = fs.readFileSync(new URL('../routes/api.py', import.meta.url), 'utf8');
const finance = [['expense-assistant', '/expense'], ['expense-upload', '/expense/records#expense-upload'], ['expense-records', '/expense/records'], ['expense-fill', '/expense/approvals']];

function activeAt(path, hash, links) {
  const controls = links.map(([id, url]) => ({dataset: {id, url}, active: false, classList: {
    add() {this.owner.active = true;}, remove() {this.owner.active = false;}, toggle(name, value) {this.owner.active = value;},
  }}));
  controls.forEach(control => {control.classList.owner = control;});
  vm.runInNewContext(highlight + '\nhighlightTree();', {path, window: {location: {hash}, addEventListener() {}}, document: {querySelectorAll: () => controls}});
  return controls.filter(control => control.active).map(control => control.dataset.id);
}

test('financial entries retain their distinct existing destinations', () => {
  for (const [id, url] of finance.slice(1)) assert.ok(api.split('\n').some(line => line.includes(`'id': '${id}'`) && line.includes(`'url': '${url}'`)));
});
test('financial pages have one active entry including hash refresh', () => {
  assert.deepEqual(activeAt('/expense/records', '#expense-upload', finance), ['expense-upload']);
  assert.deepEqual(activeAt('/expense/records', '', finance), ['expense-records']);
  assert.deepEqual(activeAt('/expense/approvals', '', finance), ['expense-fill']);
});
test('non-financial prefix highlighting stays unchanged', () => {
  for (const module of ['projects', 'experts', 'equipment']) {
    assert.deepEqual(activeAt(`/${module}/fixture`, '', [[module, `/${module}`], ['child', `/${module}/fixture`]]), [module, 'child']);
  }
});
