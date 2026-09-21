import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../static/js/app-shell.js', import.meta.url), 'utf8');
for (const initial of [null, 'true', 'false']) {
  test(`directory default/preferences: ${initial}`, () => {
    const classes = new Set();
    const attrs = {};
    const values = new Map(initial === null ? [] : [['directoryCollapsed', initial]]);
    let click;
    const shell = {classList: {
      add: value => classes.add(value),
      contains: value => classes.has(value),
      toggle: value => classes.has(value) ? (classes.delete(value), false) : (classes.add(value), true)
    }};
    const toggle = {setAttribute: (key,value) => {attrs[key] = value;}, addEventListener: (_,fn) => {click = fn;}};
    vm.runInNewContext(source, {
      document: {getElementById: id => ({appShell: shell, directoryToggle: toggle}[id] || null), querySelector: () => null, addEventListener: () => {}},
      window: {location: {pathname: '/'}, matchMedia: () => ({matches: false, addEventListener: () => {}}), addEventListener: () => {}},
      sessionStorage: {getItem: key => values.get(key) ?? null, setItem: (key,value) => values.set(key,value)}
    });
    const expectedCollapsed = initial !== 'false';
    assert.equal(classes.has('directory-collapsed'), expectedCollapsed);
    assert.equal(attrs['aria-expanded'], String(!expectedCollapsed));
    assert.equal(classes.has('nav-collapsed'), false, 'primary navigation stays independent');
    assert.equal(values.get('directoryCollapsed') ?? null, initial, 'initialization does not overwrite preferences');
    click();
    assert.equal(classes.has('directory-collapsed'), !expectedCollapsed);
    assert.equal(values.get('directoryCollapsed'), String(!expectedCollapsed));
    click();
    assert.equal(classes.has('directory-collapsed'), expectedCollapsed);
  });
}
