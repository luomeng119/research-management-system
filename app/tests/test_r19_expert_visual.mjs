import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
const index=fs.readFileSync(new URL('../templates/experts/index.html',import.meta.url),'utf8');
const done=fs.readFileSync(new URL('../templates/experts/import_done.html',import.meta.url),'utf8');
const sprite=fs.readFileSync(new URL('../static/icons/lucide-icons.svg',import.meta.url),'utf8');
test('expert title follows approved name and Excel is secondary not success status',()=>{
 assert.match(index,/<h2>专家库<\/h2>/);
 assert.match(index,/{% block title %}专家库 - 科研创新管理/);
 assert.match(index,/<button class="btn btn-outline-primary" onclick="exportSelected\('excel'\)">/);
 for(const text of ['添加专家','导入','专家组','导出Word','导出Excel']) assert.ok(index.includes(text));
 assert.ok(index.includes("{% include 'experts/partial_table.html' %}"));
});
test('import result uses existing local Lucide symbols, not glyph substitutes',()=>{
 assert.doesNotMatch(done,/[✓⚠→]/);
 for(const id of ['check','circle-alert','arrow-right']){
  assert.ok(sprite.includes(`id="${id}"`));
  assert.ok(done.includes(`/static/icons/lucide-icons.svg#${id}`));
 }
 assert.equal((done.match(/aria-hidden="true"/g)||[]).length,6);
});
test('result counts, status colors, and result navigation remain intact',()=>{
 for(const text of ['{{ results.success|length }}','{{ results.skip }}','{{ results.fail|length }}','成功导入','跳过','失败', 'background: #198754','background: #ffc107','background: #dc3545','href="/experts/import"','href="/experts/"']) assert.ok(done.includes(text));
});
