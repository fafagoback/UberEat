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
  assert.match(helper, /sequence === globalSearchSequence/);
});

test('unfiltered global catalog uses the local snapshot instead of blocking queries', () => {
  const helper = source.slice(source.indexOf('async function fetchGlobalProducts'), source.indexOf('function renderGlobalProducts'));
  assert.match(helper, /const requiresLakehouseQuery = Boolean\(rawSearch\)/);
  assert.match(helper, /if \(!requiresLakehouseQuery\) \{\s*executeInMemoryGlobalSearch\(page\);\s*return;/);
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

test('location filter validates coordinates and positive radius', () => {
  const helper = source.slice(source.indexOf('function validateLocationFilter'), source.indexOf('function loadLocationFilter'));
  const context = vm.createContext({Number});
  vm.runInContext(helper, context);
  assert.equal(context.validateLocationFilter('25.033', '121.5654', '5'), '');
  assert.match(context.validateLocationFilter('', '121.5654', '5'), /同時輸入/);
  assert.match(context.validateLocationFilter('91', '121.5654', '5'), /緯度/);
  assert.match(context.validateLocationFilter('25', '181', '5'), /經度/);
  assert.match(context.validateLocationFilter('25', '121', '0'), /大於 0/);
});

test('location filter is persisted and defaults to five kilometers', () => {
  assert.match(source, /radiusKm:\s*5/);
  assert.match(source, /localStorage\.setItem\(LOCATION_STORAGE_KEY/);
  assert.match(source, /localStorage\.getItem\(LOCATION_STORAGE_KEY/);
  assert.match(source, /localStorage\.removeItem\(LOCATION_STORAGE_KEY/);
});

test('packed Turso dashboard passes the active location to every dataset query', () => {
  const helper = source.slice(source.indexOf('async function loadFromTurso'), source.indexOf('// -----------------------------------------------------------------------------\n// 1.'));
  assert.match(helper, /loadPackedDashboard\(APP_STATE\.locationFilter\)/);
});

test('crawl batch time is never overwritten by location filter string', () => {
  const helper = source.slice(source.indexOf('async function loadFromTurso'), source.indexOf('// -----------------------------------------------------------------------------\n// 1.'));
  assert.doesNotMatch(helper, /latest_batch_formatted:\s*isLoc\s*\?\s*`座標周圍/);
  assert.match(helper, /latestBatchTime/);
});

test('global Turso search treats an active location as a server query', () => {
  const helper = source.slice(source.indexOf('async function fetchGlobalProducts'), source.indexOf('function renderGlobalProducts'));
  assert.match(helper, /APP_STATE\.locationFilter\.enabled/);
  assert.match(helper, /client\.searchPacked/);
  assert.match(helper, /location: APP_STATE\.locationFilter/);
});

test('identity dedupe uses stable store and product IDs', () => {
  const packed = fs.readFileSync('web/packed-turso.js', 'utf8');
  assert.match(packed, /String\(s\.store_uuid \|\| s\.store_id\)/);
  assert.match(packed, /p\.store_uuid \|\| p\.store_id/);
  assert.match(packed, /p\.product_uuid \|\| p\.product_id/);
  assert.doesNotMatch(packed, /`\$\{p\.store_name\}::\$\{p\.product_name\}`/);
});

test('new-store and promotional searches preserve their business filters', () => {
  const stores = source.slice(source.indexOf('async function fetchNewStores'), source.indexOf('function changeStoresPage'));
  const promos = source.slice(source.indexOf('async function fetchPromotions'), source.indexOf('function changePromosPage'));
  assert.match(stores, /newOnly:\s*true/);
  assert.match(promos, /promo:\s*true/);
  assert.match(source, /Number\(p\.quantity \|\| 1\) > 1/);
});

test('promotion name sorting compares a and b without an undefined variable', () => {
  assert.match(source, /localeCompare\(String\(b\.product_name \|\| ''\), 'zh-TW'\)/);
  assert.doesNotMatch(source, /localeCompare\(p\.product_name/);
});
