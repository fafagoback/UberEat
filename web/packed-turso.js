let runtimePromise;
const bucketCache = new Map();
const bundleCache = new Map();
const encoder = new TextEncoder();
let locationStoresCache = { key: '', storeIds: new Set(), stores: [] };

function cfg() { return window.UBER_RADAR_CONFIG || {}; }
function normalize(value) { return String(value || '').normalize('NFKC').toLocaleLowerCase().replace(/\s+/g, ''); }

function queryTokens(value) {
  const s = normalize(value);
  if (s.length >= 3) return Array.from({length: s.length - 2}, (_, i) => `t:${s.slice(i, i + 3)}`);
  if (s.length === 2) return [`b:${s}`];
  return s ? [`u:${s}`] : [];
}

async function runtime() {
  if (!runtimePromise) runtimePromise = Promise.all([
    import('https://cdn.jsdelivr.net/npm/fzstd@0.1.1/+esm'),
    import('https://cdn.jsdelivr.net/npm/@msgpack/msgpack@3.1.2/+esm'),
    import('https://cdn.jsdelivr.net/npm/@noble/hashes@1.7.1/blake2s.js/+esm')
  ]).then(([zstd, msgpack, hashes]) => ({
    decompress: zstd.decompress,
    decode: msgpack.decode,
    blake2s: hashes.blake2s
  }));
  return runtimePromise;
}

function blobBytes(cell) {
  let b64 = (cell.base64 || '').replace(/-/g, '+').replace(/_/g, '/');
  while (b64.length % 4) b64 += '=';
  const raw = atob(b64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

async function sql(statement) {
  const c = cfg();
  const url = String(c.TURSO_DATABASE_URL || '').replace(/\/+$/, '');
  const response = await fetch(`${url}/v2/pipeline`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${c.TURSO_READONLY_TOKEN}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      requests: [
        { type: 'execute', stmt: { sql: statement } },
        { type: 'close' }
      ]
    })
  });
  if (!response.ok) throw new Error(`Turso HTTP ${response.status}`);
  const body = await response.json();
  const first = body.results?.[0];
  if (first?.type === 'error') throw new Error(first.error?.message || 'Turso query failed');
  const result = first?.response?.result;
  if (!result) return [];
  const names = result.cols.map(c => c.name);
  return result.rows.map(row => Object.fromEntries(row.map((cell, i) => [names[i], cell.type === 'blob' ? blobBytes(cell) : cell.value])));
}

async function unpack(payload) {
  const r = await runtime();
  return r.decode(r.decompress(payload));
}

async function bucketId(term, count) {
  const { blake2s } = await runtime();
  const d = blake2s(encoder.encode(term), { dkLen: 4 });
  return (((d[0] * 0x1000000) + (d[1] << 16) + (d[2] << 8) + d[3]) >>> 0) % count;
}

export async function metadata() {
  const rows = await sql('select key,value from metadata');
  return Object.fromEntries(rows.map(r => {
    try { return [r.key, JSON.parse(r.value)]; }
    catch { return [r.key, r.value]; }
  }));
}

async function fetchBuckets(bucketIds) {
  const missing = [...new Set(bucketIds)].filter(id => !bucketCache.has(id));
  if (missing.length === 0) return;
  for (let i = 0; i < missing.length; i += 60) {
    const ids = missing.slice(i, i + 60);
    const rows = await sql(`select bucket_id, payload from search_buckets where bucket_id in (${ids.join(',')})`);
    for (const r of rows) {
      bucketCache.set(Number(r.bucket_id), r.payload ? await unpack(r.payload) : {});
    }
  }
}

async function posting(term, count) {
  const id = await bucketId(term, count);
  if (!bucketCache.has(id)) {
    await fetchBuckets([id]);
  }
  return (bucketCache.get(id) || {})[term] || [];
}

function intersect(groups) {
  if (!groups.length) return [];
  groups.sort((a, b) => a.length - b.length);
  let result = new Set(groups[0]);
  for (const g of groups.slice(1)) {
    const s = new Set(g);
    result = new Set([...result].filter(x => s.has(x)));
  }
  return [...result];
}

export function distanceKm(a, b, c, d) {
  const R = 6371.0088, p = Math.PI / 180;
  const x = (c - a) * p, y = (d - b) * p;
  const q = Math.sin(x / 2) ** 2 + Math.cos(a * p) * Math.cos(c * p) * Math.sin(y / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(q));
}

function inLocation(store, location) {
  if (!location?.enabled) return true;
  const lat = Number(store.latitude), lon = Number(store.longitude);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return false;
  store.distance_km = distanceKm(Number(location.latitude), Number(location.longitude), lat, lon);
  return store.distance_km <= Number(location.radiusKm);
}

function dedupeStores(stores) {
  const seen = new Set();
  const out = [];
  for (const s of stores) {
    const lat = Number(s.latitude || 0).toFixed(2);
    const lon = Number(s.longitude || 0).toFixed(2);
    const k = `${s.store_name}::${lat}::${lon}`;
    if (!seen.has(k)) {
      seen.add(k);
      out.push(s);
    }
  }
  return out;
}

function dedupeProducts(products) {
  const seen = new Set();
  const out = [];
  for (const p of products) {
    const k = `${p.store_name}::${p.product_name}`;
    if (!seen.has(k)) {
      seen.add(k);
      out.push(p);
    }
  }
  return out;
}

function productView(product, store) {
  const price = Number(product.price || 0);
  const effective = Number(product.effective_price || price);
  const saving = Math.max(0, price - effective);
  return {
    product_id: product.product_uuid,
    product_uuid: product.product_uuid,
    store_id: product.store_uuid,
    store_uuid: product.store_uuid,
    store_name: store.name || store.store_name,
    product_name: product.product_name,
    category_name: product.category,
    description: product.description,
    price,
    quantity: Number(product.quantity || 1),
    promo_type: product.promo_type,
    eff_price: effective,
    effective_price: effective,
    order_action_url: product.order_url || store.order_url,
    rating_value: store.rating || store.rating_value,
    review_count: Number(store.review_count || 0),
    locality: store.locality,
    street_address: store.address || store.street_address,
    city: store.city,
    latitude: Number(store.latitude),
    longitude: Number(store.longitude),
    distance_km: store.distance_km ?? null,
    crawled_time: product.last_seen,
    first_seen: product.first_seen,
    original_price: price,
    current_price: effective,
    discount_pct: Number(product.discount_pct || Math.round(price ? saving * 100 / price : 0)),
    savings_amount: Number(product.discount_amount || saving)
  };
}

async function bundlesForStores(storeIds) {
  const missing = [...new Set(storeIds)].filter(id => !bundleCache.has(id));
  for (let i = 0; i < missing.length; i += 60) {
    const ids = missing.slice(i, i + 60);
    const rows = await sql(`select store_id, chunk_no, payload from store_bundles where store_id in (${ids.join(',')}) order by store_id, chunk_no`);
    for (const row of rows) {
      const decoded = await unpack(row.payload);
      const sid = Number(row.store_id);
      if (!bundleCache.has(sid)) bundleCache.set(sid, []);
      bundleCache.get(sid).push(decoded);
    }
  }
  return storeIds.flatMap(id => bundleCache.get(Number(id)) || []);
}

export async function getStoresInLocation(location) {
  if (!location?.enabled) return null;
  const key = `${Number(location.latitude).toFixed(4)},${Number(location.longitude).toFixed(4)},${Number(location.radiusKm).toFixed(2)}`;
  if (locationStoresCache.key === key) return locationStoresCache;
  const lat = Number(location.latitude), lon = Number(location.longitude), r = Number(location.radiusKm);
  const dy = r / 111.32, dx = r / Math.max(1, 111.32 * Math.cos(lat * Math.PI / 180));
  const rows = await sql(`select * from store_directory where latitude between ${lat - dy} and ${lat + dy} and longitude between ${lon - dx} and ${lon + dx} order by rating desc nulls last, review_count desc limit 5000`);
  const allStores = rows.map(r => ({
    store_id: Number(r.store_id),
    store_uuid: r.store_uuid,
    store_name: r.name,
    name: r.name,
    city: r.city,
    locality: r.locality,
    rating_value: r.rating === null ? null : Number(r.rating),
    rating: r.rating === null ? null : Number(r.rating),
    review_count: Number(r.review_count || 0),
    total_menu_items: 0,
    latitude: Number(r.latitude),
    longitude: Number(r.longitude),
    street_address: r.address,
    address: r.address,
    order_action_url: r.order_url,
    first_seen: r.first_seen,
    last_seen: r.last_seen
  })).filter(s => inLocation(s, location));
  allStores.sort((a, b) => (a.distance_km || 0) - (b.distance_km || 0) || (b.rating || 0) - (a.rating || 0));
  const storeIds = new Set(allStores.map(s => s.store_id));
  const stores = dedupeStores(allStores);
  locationStoresCache = { key, storeIds, stores };
  return locationStoresCache;
}

async function productsFromRefs(refs, location, limit = 50000) {
  const wanted = new Map();
  for (const ref of refs) {
    const sid = Math.floor(Number(ref) / 1048576);
    const idx = Number(ref) % 1048576;
    if (!wanted.has(sid)) wanted.set(sid, new Set());
    wanted.get(sid).add(idx);
  }

  let storeIds = [...wanted.keys()];
  if (storeIds.length === 0) return [];

  const storeMetaMap = new Map();

  if (location?.enabled) {
    const lat = Number(location.latitude), lon = Number(location.longitude), r = Number(location.radiusKm);
    // 預先過濾外接矩形 (Bounding Box)，加 5% 安全裕度避免地表橢球曲率造成邊界誤切
    const dy = (r * 1.05) / 110.5;
    const maxLat = Math.min(89, Math.abs(lat) + dy);
    const dx = (r * 1.05) / Math.max(1, 111.32 * Math.cos(maxLat * Math.PI / 180));
    const validStoreIds = [];

    // 針對搜尋命中的候選店家直接進行座標與精確距離測算，不再盲取全區前 5000 家
    for (let i = 0; i < storeIds.length; i += 100) {
      const batch = storeIds.slice(i, i + 100);
      const rows = await sql(`select store_id, name, latitude, longitude, rating, review_count from store_directory where store_id in (${batch.join(',')}) and latitude between ${lat - dy} and ${lat + dy} and longitude between ${lon - dx} and ${lon + dx}`);
      for (const row of rows) {
        const sid = Number(row.store_id);
        const sLat = Number(row.latitude), sLon = Number(row.longitude);
        if (Number.isFinite(sLat) && Number.isFinite(sLon)) {
          const d = distanceKm(lat, lon, sLat, sLon);
          if (d <= r) {
            row.distance_km = d;
            storeMetaMap.set(sid, row);
            validStoreIds.push(sid);
          }
        }
      }
    }
    // 由近至遠排序，確保最近的門市排在最前面
    validStoreIds.sort((a, b) => (storeMetaMap.get(a)?.distance_km || 0) - (storeMetaMap.get(b)?.distance_km || 0));
    storeIds = validStoreIds;
  }

  const out = [];
  for (let i = 0; i < storeIds.length; i += 30) {
    const batch = storeIds.slice(i, i + 30);
    await bundlesForStores(batch);
    for (const sid of batch) {
      const indexes = wanted.get(sid);
      let store, cols, offset = 0;
      for (const chunk of bundleCache.get(sid) || []) {
        if (chunk.store) {
          store = Object.fromEntries(chunk.store_columns.map((c, j) => [c, chunk.store[j]]));
          cols = chunk.product_columns;
        }
        if (!store) {
          offset += (chunk.products || []).length;
          continue;
        }
        if (storeMetaMap.has(sid)) {
          store.distance_km = storeMetaMap.get(sid).distance_km;
        } else if (location?.enabled) {
          if (!inLocation(store, location)) {
            offset += (chunk.products || []).length;
            continue;
          }
        }
        for (const pair of chunk.products || []) {
          if (indexes.has(offset)) {
            const p = Object.fromEntries(cols.map((c, j) => [c, pair[1][j]]));
            out.push(productView(p, store));
            if (out.length >= limit) return dedupeProducts(out);
          }
          offset++;
        }
      }
    }
    if (out.length >= limit) break;
  }
  return dedupeProducts(out);
}

export async function directory(location, limit = 200) {
  if (location?.enabled) {
    const locInfo = await getStoresInLocation(location);
    return locInfo.stores.slice(0, limit);
  }
  const rows = await sql(`select * from store_directory where name != '' order by rating desc nulls last, review_count desc limit ${limit}`);
  const stores = rows.map(r => ({
    store_id: Number(r.store_id),
    store_uuid: r.store_uuid,
    store_name: r.name,
    name: r.name,
    city: r.city,
    locality: r.locality,
    rating_value: r.rating === null ? null : Number(r.rating),
    rating: r.rating === null ? null : Number(r.rating),
    review_count: Number(r.review_count || 0),
    total_menu_items: 0,
    latitude: Number(r.latitude),
    longitude: Number(r.longitude),
    street_address: r.address,
    address: r.address,
    order_action_url: r.order_url,
    first_seen: r.first_seen,
    last_seen: r.last_seen
  })).filter(s => inLocation(s, location));
  return dedupeStores(stores).slice(0, limit);
}

export async function searchStores({ keyword = '', location = null, limit = 100 } = {}) {
  const k = normalize(keyword);
  let where = "name != ''";
  if (k) {
    const safe = k.replace(/'/g, "''");
    if (k.includes('costco') || k.includes('好市多')) {
      where += " and (lower(name) like '%costco%' or name like '%好市多%')";
    } else if (k.includes('mcdonald') || k.includes('麥當勞')) {
      where += " and (lower(name) like '%mcdonald%' or name like '%麥當勞%')";
    } else {
      where += ` and (lower(name) like '%${safe}%' or locality like '%${safe}%' or address like '%${safe}%')`;
    }
  }
  if (location?.enabled) {
    const lat = Number(location.latitude), lon = Number(location.longitude), r = Number(location.radiusKm);
    const dy = r / 111.32, dx = r / Math.max(1, 111.32 * Math.cos(lat * Math.PI / 180));
    where += ` and latitude between ${lat - dy} and ${lat + dy} and longitude between ${lon - dx} and ${lon + dx}`;
  }
  const rows = await sql(`select * from store_directory where ${where} order by rating desc nulls last, review_count desc limit ${Math.max(limit, location?.enabled ? 5000 : limit)}`);
  const stores = rows.map(r => ({
    store_id: Number(r.store_id),
    store_uuid: r.store_uuid,
    store_name: r.name,
    name: r.name,
    city: r.city,
    locality: r.locality,
    rating_value: r.rating === null ? null : Number(r.rating),
    rating: r.rating === null ? null : Number(r.rating),
    review_count: Number(r.review_count || 0),
    total_menu_items: 0,
    latitude: Number(r.latitude),
    longitude: Number(r.longitude),
    street_address: r.address,
    address: r.address,
    order_action_url: r.order_url,
    first_seen: r.first_seen,
    last_seen: r.last_seen
  })).filter(s => inLocation(s, location));
  if (location?.enabled) {
    stores.sort((a, b) => (a.distance_km || 0) - (b.distance_km || 0) || (b.rating || 0) - (a.rating || 0));
  }
  return dedupeStores(stores).slice(0, limit);
}

async function sampleProducts(stores, location, limit = 1000) {
  const sids = stores.map(s => s.store_id);
  await bundlesForStores(sids.slice(0, 40));
  const out = [];
  for (const d of stores) {
    let store, cols;
    for (const chunk of bundleCache.get(d.store_id) || []) {
      if (chunk.store) {
        store = Object.fromEntries(chunk.store_columns.map((c, i) => [c, chunk.store[i]]));
        cols = chunk.product_columns;
      }
      if (!store || !inLocation(store, location)) continue;
      for (const pair of chunk.products || []) {
        const p = Object.fromEntries(cols.map((c, i) => [c, pair[1][i]]));
        out.push(productView(p, store));
        if (out.length >= limit) return dedupeProducts(out);
      }
    }
  }
  return dedupeProducts(out);
}

export async function loadPackedDashboard(location) {
  const meta = await metadata();
  const stores = await directory(location, location?.enabled ? 300 : 100);
  const products = await sampleProducts(stores, location, 1000);
  return { meta, stores, products };
}

export async function searchPacked({ keyword = '', city = '', promo = false, newOnly = false, minDiscount = null, location = null, limit = 50000 } = {}) {
  const meta = await metadata();
  const count = Number(meta.buckets || 8192);
  const terms = [...queryTokens(keyword), ...queryTokens(city)];
  if (promo) terms.push('f:promo');
  if (newOnly) terms.push('f:new');
  const bIds = await Promise.all(terms.map(t => bucketId(t, count)));
  await fetchBuckets(bIds);

  const groups = [];
  for (const term of terms) {
    groups.push((bucketCache.get(await bucketId(term, count)) || {})[term] || []);
  }

  if (minDiscount !== null) {
    const minBand = Math.floor(Number(minDiscount) / 5);
    const discTerms = [];
    for (let n = minBand; n <= 20; n++) discTerms.push(`f:discount:${n}`);
    const discBids = await Promise.all(discTerms.map(t => bucketId(t, count)));
    await fetchBuckets(discBids);
    const union = new Set();
    for (const t of discTerms) {
      const bid = await bucketId(t, count);
      for (const ref of (bucketCache.get(bid) || {})[t] || []) union.add(ref);
    }
    groups.push([...union]);
  }

  if (!groups.length) {
    const stores = await directory(location, location?.enabled ? 200 : 80);
    return sampleProducts(stores, location, limit);
  }

  const rows = await productsFromRefs(intersect(groups), location, limit);
  const k = normalize(keyword), c = normalize(city);
  return rows.filter(p => (!k || normalize(`${p.product_name}${p.store_name}${p.category_name}`).includes(k)) && (!c || normalize(`${p.city}${p.locality}${p.street_address}`).includes(c)));
}

export async function packedHistory(storeUuid, productUuid) {
  const row = (await sql(`select store_id from store_directory where store_uuid='${String(storeUuid).replace(/'/g, "''")}' limit 1`))[0];
  if (!row) return [];
  await bundlesForStores([Number(row.store_id)]);
  const out = [];
  for (const chunk of bundleCache.get(Number(row.store_id)) || []) {
    for (const event of chunk.events || []) {
      const cols = chunk.event_columns;
      if (!cols) continue;
      const e = Object.fromEntries(cols.map((c, i) => [c, event[i]]));
      if (String(e.product_uuid) === String(productUuid)) out.push(e);
    }
  }
  return out.sort((a, b) => String(a.event_time).localeCompare(String(b.event_time))).slice(-50);
}
