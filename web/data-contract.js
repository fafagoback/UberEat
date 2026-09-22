/* Shared by the packed reader and the small, explicitly dated fallback. */
(function (root) {
  const cities = '基隆市 台北市 新北市 桃園市 新竹市 新竹縣 苗栗縣 台中市 彰化縣 南投縣 雲林縣 嘉義市 嘉義縣 台南市 高雄市 屏東縣 宜蘭縣 花蓮縣 台東縣 澎湖縣 金門縣 連江縣'.split(' ');
  const english = 'Keelung City|Taipei City|New Taipei City|Taoyuan City|Hsinchu City|Hsinchu County|Miaoli County|Taichung City|Changhua County|Nantou County|Yunlin County|Chiayi City|Chiayi County|Tainan City|Kaohsiung City|Pingtung County|Yilan County|Hualien County|Taitung County|Penghu County|Kinmen County|Lienchiang County'.split('|');
  function city(address, region = '') {
    for (const value of [address, region]) {
      const text = String(value || '').replaceAll('臺', '台').trim();
      const matches = cities.filter(c => text.includes(c));
      if (matches.length === 1) return matches[0];
      if (matches.length > 1) return '';
      const i = english.findIndex(c => c.toLowerCase() === text.toLowerCase());
      if (i >= 0) return cities[i];
    }
    return '';
  }
  function positive(value) {
    if (value == null || value === '') return null;
    const n = Number(value);
    return Number.isFinite(n) && n > 0 ? n : null;
  }
  function product(row) {
    const price = positive(row.price ?? row.curr_raw_price);
    const quantity = positive(row.quantity ?? row.curr_qty) || 1;
    const effective = positive(row.effective_price ?? row.eff_price ?? row.current_price);
    const legacy = row.curr_raw_price !== undefined || row.prev_raw_price !== undefined;
    return {...row, price, quantity, eff_price: effective, effective_price: effective,
      // Old static exporters guessed cities from ambiguous street names.
      city: city(row.street_address || row.address, legacy ? '' : row.city),
      valid_price: price !== null};
  }
  function money(value) {
    const n = positive(value);
    return n === null ? '價格未提供' : '$' + n.toLocaleString('zh-TW', {maximumFractionDigits: 2});
  }
  root.UBER_DATA_CONTRACT = {city, positive, product, money};
})(typeof window === 'undefined' ? globalThis : window);
