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
});

test('frontend correctly routes API endpoints to static JSON files in Jamstack mode', () => {
  const helper = source.slice(source.indexOf('function getApiUrl'), source.indexOf('async function loadDashboardData'));
  const context = vm.createContext({
    window: {
      UBER_RADAR_CONFIG: { API_BASE_URL: './data' }
    },
    Date: { now: () => 1234567890 }
  });
  vm.runInContext(helper, context);
  assert.equal(context.getApiUrl('/api/stats'), './data/stats.json?_t=1234567890');
  assert.equal(context.getApiUrl('/api/discounts?min_discount=30'), './data/discounts.json?_t=1234567890');
  assert.equal(context.getApiUrl('/api/products?q=test'), './data/products.json?_t=1234567890');
});
