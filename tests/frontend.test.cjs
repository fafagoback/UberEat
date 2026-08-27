const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const appPath = fs.existsSync('web/app.js') ? 'web/app.js' : 'app.js';
const source = fs.readFileSync(appPath, 'utf8');

test('dataset values are not interpolated into executable onclick attributes', () => {
  assert.doesNotMatch(source, /onclick="(?:showPriceHistoryModal|openUberEatsOrder)/);
  assert.match(source, /data-args="\$\{escapeHtml\(JSON.stringify/);
});

test('order URLs reject scripts, credentials and unrelated hosts', () => {
  const helper = source.slice(source.indexOf('function safeOrderUrl'), source.indexOf('// Dataset text'));
  const context = vm.createContext({URL});
  vm.runInContext(helper, context);
  for (const url of ['javascript:alert(1)', 'https://evil.test', 'https://user@www.ubereats.com/tw']) {
    assert.equal(context.safeOrderUrl(url), '#');
  }
  assert.equal(context.safeOrderUrl('https://www.ubereats.com/tw'), 'https://www.ubereats.com/tw');
});

test('only latest search response may mutate displayed products', () => {
  const helper = source.slice(source.indexOf('let globalSearchController'), source.indexOf('function renderGlobalProducts'));
  assert.match(helper, /globalSearchController\?\.abort\(\)/);
  assert.match(helper, /if \(sequence !== globalSearchSequence\) return;/);
  assert.match(helper, /signal: controller.signal/);
});

test('API reads published snapshots and rejects invalid pagination', async () => {
  const workerSource = fs.readFileSync('cloudflare/worker.js', 'utf8');
  assert.doesNotMatch(workerSource, /(?:FROM|JOIN) (?:products|stores)\b/);
  const {default: worker} = await import('data:text/javascript;base64,' + Buffer.from(workerSource).toString('base64'));
  const DB = {prepare: () => ({all: async () => ({results: []})})};
  for (const query of ['limit=-1', 'page=0', 'page=foo', 'limit=101']) {
    const response = await worker.fetch(new Request('https://api.test/api/products?' + query), {DB}, {});
    assert.equal(response.status, 400);
  }
});
