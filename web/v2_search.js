/* UberEats Radar v2 full-catalog search.
 * Uses a sharded n-gram inverted index + sharded current records on HF.
 * The browser downloads only the index/data shards needed by the query.
 */
(function () {
  'use strict';

  const HF_BASE = 'https://huggingface.co/datasets/hub-google/UberEat/resolve/main/v2';
  const SEARCH_MANIFEST_URL = `${HF_BASE}/search/manifest.json`;
  const CURRENT_MANIFEST_URL = `${HF_BASE}/current/manifest.json`;
  const searchShardCache = new Map();
  const currentShardCache = new Map();
  let manifestsPromise = null;

  function normalize(value) {
    return String(value || '').normalize('NFKC').toLowerCase().replace(/\s+/g, ' ').trim();
  }

  async function sha1hex(text) {
    const data = new TextEncoder().encode(text);
    const digest = await crypto.subtle.digest('SHA-1', data);
    return Array.from(new Uint8Array(digest)).map(b => b.toString(16).padStart(2, '0')).join('');
  }

  async function tokenBucket(token) {
    return (await sha1hex(token)).slice(0, 2);
  }

  function queryTokens(query) {
    const runs = normalize(query).match(/[0-9a-z\u3400-\u9fff]+/g) || [];
    const out = [];
    for (const run of runs) {
      if (run.length <= 3) out.push(run);
      else for (let i = 0; i <= run.length - 3; i++) out.push(run.slice(i, i + 3));
    }
    return [...new Set(out)];
  }

  async function fetchJson(url) {
    const res = await fetch(`${url}${url.includes('?') ? '&' : '?'}_t=${Date.now()}`, { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${url}`);
    return res.json();
  }

  async function fetchGzipJson(url) {
    const res = await fetch(url, { cache: 'force-cache' });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${url}`);
    if (typeof DecompressionStream === 'undefined') throw new Error('Browser does not support DecompressionStream(gzip)');
    const text = await new Response(res.body.pipeThrough(new DecompressionStream('gzip'))).text();
    return JSON.parse(text);
  }

  async function loadManifests() {
    if (!manifestsPromise) {
      manifestsPromise = Promise.all([fetchJson(SEARCH_MANIFEST_URL), fetchJson(CURRENT_MANIFEST_URL)])
        .then(([search, current]) => ({ search, current }));
    }
    return manifestsPromise;
  }

  async function loadSearchShard(bucket) {
    if (!searchShardCache.has(bucket)) searchShardCache.set(bucket, fetchGzipJson(`${HF_BASE}/search/shards/${bucket}.json.gz`));
    return searchShardCache.get(bucket);
  }

  async function loadCurrentShard(bucket) {
    if (!currentShardCache.has(bucket)) currentShardCache.set(bucket, fetchGzipJson(`${HF_BASE}/current/shards/${bucket}.json.gz`));
    return currentShardCache.get(bucket);
  }

  function intersectSets(sets) {
    if (!sets.length) return new Set();
    sets.sort((a, b) => a.size - b.size);
    const result = new Set(sets[0]);
    for (let i = 1; i < sets.length; i++) {
      for (const value of result) if (!sets[i].has(value)) result.delete(value);
      if (!result.size) break;
    }
    return result;
  }

  function exactMatch(record, query) {
    const runs = normalize(query).match(/[0-9a-z\u3400-\u9fff]+/g) || [];
    const haystack = normalize(record.search_text || [record.store_name, record.product_name, record.category_name, record.description, record.city, record.locality].join(' '));
    return runs.every(run => haystack.includes(run));
  }

  function sortRows(rows, sortMode) {
    const copy = rows.slice();
    if (sortMode === 'price_asc') copy.sort((a, b) => (a.eff_price ?? a.price ?? 0) - (b.eff_price ?? b.price ?? 0));
    else if (sortMode === 'price_desc') copy.sort((a, b) => (b.eff_price ?? b.price ?? 0) - (a.eff_price ?? a.price ?? 0));
    else if (sortMode === 'name_asc') copy.sort((a, b) => String(a.product_name || '').localeCompare(String(b.product_name || ''), 'zh-Hant'));
    else copy.sort((a, b) => (b.rating_value ?? -1) - (a.rating_value ?? -1));
    return copy;
  }

  async function searchAll(query, city) {
    await loadManifests();
    const q = normalize(query);
    if (!q) return [];
    const tokens = queryTokens(q);
    if (!tokens.length) return [];

    const bucketToTokens = new Map();
    for (const token of tokens) {
      const bucket = await tokenBucket(token);
      if (!bucketToTokens.has(bucket)) bucketToTokens.set(bucket, []);
      bucketToTokens.get(bucket).push(token);
    }

    const tokenSets = [];
    for (const [bucket, neededTokens] of bucketToTokens.entries()) {
      const shard = await loadSearchShard(bucket);
      for (const token of neededTokens) tokenSets.push(new Set(shard[token] || []));
    }

    const candidateIds = intersectSets(tokenSets);
    if (!candidateIds.size) return [];

    const docBuckets = new Map();
    for (const id of candidateIds) {
      const bucket = String(id).slice(0, 2);
      if (!docBuckets.has(bucket)) docBuckets.set(bucket, new Set());
      docBuckets.get(bucket).add(id);
    }

    const rows = [];
    for (const [bucket, ids] of docBuckets.entries()) {
      const shardRows = await loadCurrentShard(bucket);
      for (const row of shardRows) {
        if (!ids.has(String(row.id))) continue;
        if (!exactMatch(row, q)) continue;
        if (city && city !== '全部' && String(row.city || '') !== city) continue;
        rows.push(row);
      }
    }
    return rows;
  }

  async function installOverride() {
    let attempts = 0;
    while ((typeof fetchGlobalProducts !== 'function' || typeof APP_STATE === 'undefined') && attempts < 100) {
      await new Promise(r => setTimeout(r, 50));
      attempts++;
    }
    if (typeof fetchGlobalProducts !== 'function' || typeof APP_STATE === 'undefined') {
      console.warn('[v2 search] app core was not ready; legacy search remains active');
      return;
    }

    const legacyFetchGlobalProducts = fetchGlobalProducts;
    fetchGlobalProducts = async function (page = 1) {
      const state = APP_STATE;
      const query = (state.filters && state.filters.globalSearch) || '';
      const city = (state.filters && state.filters.globalCity) || '全部';
      const sortMode = (state.filters && state.filters.globalSort) || 'rating_desc';

      if (!normalize(query)) return legacyFetchGlobalProducts(page);

      try {
        const badgeEl = document.getElementById('lakehouse-badge');
        if (badgeEl) badgeEl.textContent = 'v2 全台索引搜尋中...';
        const rows = sortRows(await searchAll(query, city), sortMode);
        const pageSize = (typeof PAGE_SIZE !== 'undefined' ? PAGE_SIZE : 50);
        const total = rows.length;
        const totalPages = Math.max(1, Math.ceil(total / pageSize));
        const safePage = Math.min(Math.max(1, page), totalPages);
        const start = (safePage - 1) * pageSize;

        state.globalProducts = rows.slice(start, start + pageSize);
        state.globalPage = safePage;
        state.globalTotalPages = totalPages;
        state.globalTotalItems = total;
        if (typeof renderGlobalProducts === 'function') renderGlobalProducts();
        if (badgeEl) badgeEl.textContent = `v2 真實全台索引 · ${total.toLocaleString()} 筆`;
      } catch (err) {
        console.error('[v2 search] failed, falling back to legacy path', err);
        return legacyFetchGlobalProducts(page);
      }
    };

    if (window.UBER_RADAR_CONFIG) window.UBER_RADAR_CONFIG.ENABLE_DUCKDB = false;
    console.log('[v2 search] sharded full-catalog search installed');
  }

  installOverride();
})();
