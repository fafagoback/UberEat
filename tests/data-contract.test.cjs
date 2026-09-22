const {test} = require('node:test');
const assert = require('node:assert/strict');
require('../web/data-contract.js');
const c = globalThis.UBER_DATA_CONTRACT;
test('legacy discounted rows keep the raw and effective price distinct', () => {
  const p = c.product({curr_raw_price:2415, current_price:1207.5, curr_qty:2});
  assert.equal(p.price, 2415); assert.equal(p.effective_price, 1207.5); assert.equal(p.quantity, 2);
});
test('invalid prices never become free products', () => {
  for (const value of [undefined, null, '', 'bad', 0, -1, Infinity, NaN]) {
    assert.equal(c.product({price:value}).valid_price, false);
    assert.equal(c.money(value), '價格未提供');
  }
  assert.equal(c.money(0.5), '$0.5');
});
test('legacy guessed city is discarded; explicit address wins', () => {
  assert.equal(c.product({curr_raw_price:100, city:'台北市', street_address:'中山路'}).city, '');
  assert.equal(c.city('台東縣台東市中山路', '台北市'), '台東縣');
  assert.equal(c.city('中山路'), '');
  assert.equal(c.city('', 'New Taipei City'), '新北市');
});
