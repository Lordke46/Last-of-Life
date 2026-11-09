const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function theme(saved) {
  const attributes = {}, root = { dataset: saved ? { theme: saved } : {} };
  const meta = {}, writes = [];
  let click;
  const button = {setAttribute: (key, value) => attributes[key] = value,
    addEventListener: (_, fn) => click = fn};
  vm.runInNewContext(fs.readFileSync('static/theme.js', 'utf8'), {
    document: {documentElement: root, getElementById: () => button, querySelector: () => meta},
    localStorage: {setItem: (...args) => writes.push(args)},
  });
  return {root, meta, attributes, writes, button, click: () => click()};
}
test('new visitors get the black palette and can persist white mode', () => {
  const ui = theme();
  assert.equal(ui.meta.content, '#080808');
  assert.equal(ui.attributes['aria-pressed'], 'false');
  ui.click();
  assert.equal(ui.root.dataset.theme, 'light');
  assert.equal(ui.meta.content, '#FAFAFA');
  assert.equal(ui.button.title, 'Koyu temaya geç');
  assert.deepEqual(ui.writes[0], ['theme', 'light']);
});
test('saved light preference survives initialization and toggles back to black', () => {
  const ui = theme('light');
  assert.equal(ui.meta.content, '#FAFAFA');
  assert.equal(ui.attributes['aria-pressed'], 'true');
  ui.click();
  assert.equal(ui.meta.content, '#080808');
  assert.equal(ui.root.dataset.theme, 'dark');
});
