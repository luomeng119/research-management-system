import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {test} from 'node:test';

const template = fs.readFileSync(new URL('../templates/proposals/_assistant.html', import.meta.url), 'utf8');
const script = template.match(/<script>([\s\S]*?)<\/script>/)[1];

for (const errorMessage of ['助手暂不可用，请继续手工填写', '网络连接失败']) {
  test(`failure feedback stays clear: ${errorMessage}`, async () => {
    const elements = Object.fromEntries(['proposal-assistant', 'assistant-source', 'assistant-message', 'assistant-result', 'assistant-generate', 'assistant-cancel', 'title'].map(id => [id, {
      value: '已手工填写的内容', dataset: {proposalId: 'fixture', proposalVersion: '1'},
      classList: {add() {}, remove() {}}, listeners: {}, querySelectorAll: () => [],
      addEventListener(event, callback) {this.listeners[event] = callback;},
    }]));
    let requests = 0;
    vm.runInNewContext(script, {
      document: {getElementById: id => elements[id] || null, querySelector: () => null},
      crypto: {randomUUID: () => 'fixture-run'}, AbortController,
      fetch: async () => {requests++; return {ok: false, json: async () => ({error: {message: errorMessage}})};},
    });
    await elements['assistant-generate'].listeners.click();
    const displayed = elements['assistant-message'].textContent;
    assert.equal((displayed.match(/手工填写/g) || []).length, 1);
    assert.ok(displayed.startsWith(errorMessage));
    assert.equal(elements.title.value, '已手工填写的内容');
    assert.equal(requests, 1);
  });
}
