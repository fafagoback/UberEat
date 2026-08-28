/**
 * UberEats Radar - 前端分析儀表板核心互動邏輯
 * 方案 C: 邊緣靜態快照與 Jamstack CDN 極速架構 (0ms 本地記憶體即時檢索 + DuckDB-WASM 邊緣湖倉)
 * 完整全資料集分頁展示 (每頁 50 筆，無截斷限制)
 * [Live Sync Support]
 */

const PAGE_SIZE = 50;

let APP_STATE = {
  isServerMode: false,
  currentTab: 'tab-discounts',
  stats: {},
  
  // Tab 1: 今日大特價
  rawDiscounts: [],
  discounts: [],
  discountsPage: 1,

  // Tab 2: 新進店家
  newStores: [],
  filteredStores: [],
  storesPage: 1,

  // Tab 3: 老店新菜
  newProducts: [],
  filteredProducts: [],
  productsPage: 1,

  // Tab 4: 促銷活動
  promotions: [],
  filteredPromotions: [],
  promosPage: 1,

  // Tab 5: 全庫檢索
  allProducts: [],
  globalProducts: [],
  globalPage: 1,
  globalTotalPages: 1,
  globalTotalItems: 0,

  historyMap: {},
  chartInstance: null,
  filters: {
    // Tab 1
    discountMinPct: 30,
    discountMinSavings: 20,
    discountSort: 'discount_desc',
    discountCategory: '全部',
    discountSearch: '',

    // Tab 2
    storeSearch: '',
    storeCity: '全部',
    storeSort: 'rating_desc',

    // Tab 3
    productSearch: '',

    // Tab 4
    promoSearch: '',
    promoType: '全部',
    promoSort: 'price_asc',

    // Tab 5
    globalSearch: '',
    globalCity: '全部',
    globalSort: 'rating_desc'
  }
};

// DuckDB-WASM 邊緣 SQL 湖倉實例
let DUCKDB_INSTANCE = null;
let DUCKDB_CONN = null;
let DUCKDB_INITIALIZING = false;
let DUCKDB_READY = false;
const REGISTERED_PARQUET_TABLES = new Set();

async function ensureParquetRegistered(tableName) {
  if (!DUCKDB_INSTANCE || !DUCKDB_CONN) return false;
  if (REGISTERED_PARQUET_TABLES.has(tableName)) return true;

  const duckdb = await import('https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.28.0/+esm');

  let localUrl = '';
  let remoteUrl = '';

  if (tableName === 'taiwan_catalog.parquet') {
    localUrl = new URL('./data/taiwan_catalog_latest.parquet', window.location.href).href;
    remoteUrl = (window.UBER_RADAR_CONFIG && window.UBER_RADAR_CONFIG.PARQUET_CATALOG_URL) 
      || 'https://huggingface.co/datasets/hub-google/UberEat/resolve/main/Parquet/taiwan_catalog_latest.parquet';
  } else {
    // 縣市分區切片檔 (例如 catalog_taipei.parquet)
    localUrl = new URL(`./data/partitions/${tableName}`, window.location.href).href;
    const partitionsBase = (window.UBER_RADAR_CONFIG && window.UBER_RADAR_CONFIG.PARQUET_PARTITIONS_BASE_URL)
      || 'https://huggingface.co/datasets/hub-google/UberEat/resolve/main/Parquet/partitions';
    remoteUrl = `${partitionsBase.replace(/\/+$/, '')}/${tableName}`;
  }

  // 1. 先檢測本地是否存在切片 (快速 HEAD 探測，避免把 404 HTML 註冊到 DuckDB)
  let targetUrl = '';
  if (window.location.protocol === 'http:' || window.location.protocol === 'https:') {
    try {
      const headRes = await Promise.race([
        fetch(localUrl, { method: 'HEAD', cache: 'no-store' }),
        new Promise((_, reject) => setTimeout(() => reject(new Error('Timeout')), 1200))
      ]);
      if (headRes && headRes.ok) {
        targetUrl = localUrl;
        console.log(`🎯 [DuckDB-WASM] 命中本地切片: ${tableName}`);
      }
    } catch (e) {
      // 本地無此切片，切換遠端
    }
  }

  if (!targetUrl) {
    targetUrl = remoteUrl;
    console.log(`🌐 [DuckDB-WASM] 使用遠端 Hugging Face 湖倉: ${tableName}`);
  }

  // 2. 註冊至 DuckDB (先 dropFile 避免重名註冊錯誤)
  try {
    await DUCKDB_INSTANCE.dropFile(tableName).catch(() => {});
    await DUCKDB_INSTANCE.registerFileURL(tableName, targetUrl, duckdb.DuckDBDataProtocol.HTTP, false);
    await DUCKDB_CONN.query(`SELECT COUNT(*) FROM '${tableName}' LIMIT 1`);
    REGISTERED_PARQUET_TABLES.add(tableName);
    console.log(`✅ [DuckDB-WASM] 成功連線切片: ${tableName}`);
    return true;
  } catch (err) {
    console.warn(`⚠️ [DuckDB-WASM] 切片連線失敗: ${tableName}`, err);
    // 若原 targetUrl 失敗且原本嘗試本地，嘗試遠端
    if (targetUrl === localUrl && remoteUrl) {
      try {
        await DUCKDB_INSTANCE.dropFile(tableName).catch(() => {});
        await DUCKDB_INSTANCE.registerFileURL(tableName, remoteUrl, duckdb.DuckDBDataProtocol.HTTP, false);
        await DUCKDB_CONN.query(`SELECT COUNT(*) FROM '${tableName}' LIMIT 1`);
        REGISTERED_PARQUET_TABLES.add(tableName);
        console.log(`✅ [DuckDB-WASM] 遠端切片備援成功: ${tableName}`);
        return true;
      } catch (err2) {
        console.error(`❌ [DuckDB-WASM] 遠端切片備援亦失敗: ${tableName}`, err2);
      }
    }
    return false;
  }
}

// -----------------------------------------------------------------------------
// 0. 版本追蹤與即時發佈自動偵測
// -----------------------------------------------------------------------------
let CURRENT_VERSION = null;

async function checkVersionUpdate(isInitial = false) {
  try {
    const res = await fetch(`version.json?_t=${Date.now()}`, { cache: 'no-store' });
    if (!res.ok) return;
    const info = await res.json();
    if (!info || !info.version) return;

    if (isInitial) {
      CURRENT_VERSION = info.version;
      console.log(`[UberEats Radar] 當前系統版本: ${CURRENT_VERSION} (${info.buildTime || '最新'})`);
      return;
    }

    if (CURRENT_VERSION && info.version !== CURRENT_VERSION) {
      console.log(`[UberEats Radar] 發現新版本發佈: ${info.version} (目前: ${CURRENT_VERSION})，準備自動更新...`);
      showToast('發現新版本發佈', '系統正在為您載入最新快照資料...', 'external', 2500);
      setTimeout(() => {
        window.location.reload();
      }, 1200);
    }
  } catch (e) {
    // 忽略檢查錯誤 (例如離線時)
  }
}

function startVersionWatcher() {
  checkVersionUpdate(true);

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      checkVersionUpdate(false);
    }
  });

  setInterval(() => {
    checkVersionUpdate(false);
  }, 2 * 60 * 1000);
}

// -----------------------------------------------------------------------------
// DuckDB-WASM 邊緣 SQL 查詢引擎 (v7.0 Hugging Face 百萬大數據湖倉)
// -----------------------------------------------------------------------------
async function initDuckDBEngine() {
  if (DUCKDB_READY || DUCKDB_INITIALIZING) return;
  if (!window.UBER_RADAR_CONFIG || window.UBER_RADAR_CONFIG.ENABLE_DUCKDB === false) return;

  DUCKDB_INITIALIZING = true;
  const badgeEl = document.getElementById('lakehouse-badge');
  if (badgeEl) {
    badgeEl.innerHTML = `<span class="w-1.5 h-1.5 rounded-full bg-amber-500 animate-pulse"></span><span>連線湖倉中...</span>`;
    badgeEl.className = "px-2 py-0.5 rounded-full text-[11px] font-medium bg-amber-50 text-amber-700 dark:bg-amber-950/80 dark:text-amber-300 border border-amber-200 dark:border-amber-800 flex items-center gap-1";
  }

  try {
    const initPromise = (async () => {
      const duckdb = await import('https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.28.0/+esm');
      const JSDELIVR_BUNDLES = duckdb.getJsDelivrBundles();
      const bundle = await duckdb.selectBundle(JSDELIVR_BUNDLES);

      const worker = await duckdb.createWorker(bundle.mainWorker);
      const logger = new duckdb.ConsoleLogger();
      const db = new duckdb.AsyncDuckDB(logger, worker);
      await db.instantiate(bundle.mainModule, bundle.pthreadWorker);

      const conn = await db.connect();
      DUCKDB_INSTANCE = db;
      DUCKDB_CONN = conn;

      // 嘗試預先快取台北市切片 (最常用分區，20MB，非阻塞)
      ensureParquetRegistered('catalog_taipei.parquet').catch(() => null);
      return true;
    })();

    // 8 秒逾時防護，若逾時則無縫維持本地快照模式
    await Promise.race([
      initPromise,
      new Promise((_, reject) => setTimeout(() => reject(new Error('DuckDB 連線超時')), 8000))
    ]);

    DUCKDB_READY = true;
    console.log('✅ [DuckDB-WASM] 湖倉 SQL 查詢引擎已就緒');

    if (badgeEl) {
      badgeEl.innerHTML = `<span class="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse"></span><span>DuckDB 湖倉在線</span>`;
      badgeEl.className = "px-2 py-0.5 rounded-full text-[11px] font-medium bg-emerald-50 text-emerald-700 dark:bg-emerald-950/80 dark:text-emerald-300 border border-emerald-200 dark:border-emerald-800 flex items-center gap-1";
    }

    // 若使用者當前正停留在全庫檢索分頁，無縫升級為湖倉深度檢索
    if (APP_STATE.currentTab === 'tab-global-search') {
      fetchGlobalProducts(APP_STATE.globalPage || 1);
    }
  } catch (err) {
    console.warn('⚠️ [DuckDB-WASM] 湖倉連線未啟動 (維持本地極速快照檢索模式):', err);
    DUCKDB_READY = false;
    if (badgeEl) {
      badgeEl.innerHTML = `<span class="w-1.5 h-1.5 rounded-full bg-slate-400"></span><span>本地快照搜尋</span>`;
      badgeEl.className = "px-2 py-0.5 rounded-full text-[11px] font-medium bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-400 border border-slate-200 dark:border-slate-700 flex items-center gap-1";
    }
    if (APP_STATE.currentTab === 'tab-global-search') {
      executeInMemoryGlobalSearch(APP_STATE.globalPage || 1);
    }
  } finally {
    DUCKDB_INITIALIZING = false;
  }
}

// -----------------------------------------------------------------------------
// 初始化啟動
// -----------------------------------------------------------------------------
async function bootstrap() {
  initTheme();
  initEventListeners();
  await loadDashboardData();
  if (window.lucide) {
    lucide.createIcons();
  }
  startVersionWatcher();
  initDuckDBEngine().catch(e => console.warn('DuckDB init background error:', e));
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bootstrap);
} else {
  bootstrap();
}

// -----------------------------------------------------------------------------
// 1. 靜態 API 網址映射與資料載入
// -----------------------------------------------------------------------------
function getApiUrl(endpoint) {
  const base = (window.UBER_RADAR_CONFIG && window.UBER_RADAR_CONFIG.API_BASE_URL) || './data';
  const cleanBase = base.replace(/\/+$/, '');
  const path = endpoint.split('?')[0];

  const map = {
    '/api/stats': '/stats.json',
    '/api/discounts': '/discounts.json',
    '/api/new-stores': '/new_stores.json',
    '/api/new-products': '/new_products.json',
    '/api/promotions': '/promotions.json',
    '/api/products': '/products.json',
    '/api/history': '/history.json'
  };

  const mapped = map[path] || (path.endsWith('.json') ? path : `${path}.json`);
  return `${cleanBase}${mapped}?_t=${Date.now()}`;
}

async function loadDashboardData() {
  let loadedFromServer = false;

  try {
    const [statsRes, discRes, storesRes, prodsRes, promosRes, catalogRes, histRes] = await Promise.all([
      fetch(getApiUrl('/api/stats')),
      fetch(getApiUrl('/api/discounts')),
      fetch(getApiUrl('/api/new-stores')),
      fetch(getApiUrl('/api/new-products')),
      fetch(getApiUrl('/api/promotions')),
      fetch(getApiUrl('/api/products')),
      fetch(getApiUrl('/api/history')).catch(() => null)
    ]);

    if (statsRes && statsRes.ok) {
      APP_STATE.isServerMode = true;
      const statsData = await statsRes.json();
      updateStatsUI(statsData);

      if (discRes && discRes.ok) {
        const d = await discRes.json();
        APP_STATE.rawDiscounts = d.items || d || [];
      }
      if (storesRes && storesRes.ok) {
        const d = await storesRes.json();
        APP_STATE.newStores = d.items || d || [];
      }
      if (prodsRes && prodsRes.ok) {
        const d = await prodsRes.json();
        APP_STATE.newProducts = d.items || d || [];
      }
      if (promosRes && promosRes.ok) {
        const d = await promosRes.json();
        APP_STATE.promotions = d.items || d || [];
      }
      if (catalogRes && catalogRes.ok) {
        const d = await catalogRes.json();
        APP_STATE.allProducts = d.items || d || [];
      }
      if (histRes && histRes.ok) {
        const d = await histRes.json();
        APP_STATE.historyMap = d.history || {};
      }

      await fetchDiscounts(1);
      await fetchNewStores(1);
      await fetchNewProducts(1);
      await fetchPromotions(1);
      await fetchGlobalProducts(1);
      loadedFromServer = true;
    }
  } catch (err) {
    console.warn('無法連線靜態 API，切換為離線備援資料:', err);
  }

  if (!loadedFromServer) {
    APP_STATE.isServerMode = false;
    if (window.UBER_RADAR_DATA) {
      const data = window.UBER_RADAR_DATA;
      updateStatsUI(data.stats || {});
      APP_STATE.rawDiscounts = data.big_discounts || [];
      APP_STATE.newStores = data.new_stores || [];
      APP_STATE.newProducts = data.new_products || [];
      APP_STATE.promotions = data.promotions || [];
      APP_STATE.allProducts = data.all_products || [];
      APP_STATE.historyMap = data.history || {};
      await fetchDiscounts(1);
      await fetchNewStores(1);
      await fetchNewProducts(1);
      await fetchPromotions(1);
      await fetchGlobalProducts(1);
    } else {
      console.warn('未偵測到備援資料。');
    }
  }
}

// -----------------------------------------------------------------------------
// 2. 更新統計指標 UI
// -----------------------------------------------------------------------------
function updateStatsUI(stats) {
  APP_STATE.stats = stats;
  document.getElementById('batch-time-text').textContent = stats.latest_batch_formatted || stats.latest_batch || '已載入';
  document.getElementById('stat-big-discounts').textContent = (stats.big_discounts_count ?? 0).toLocaleString();
  document.getElementById('stat-new-stores').textContent = (stats.new_stores_count ?? stats.total_stores ?? 0).toLocaleString();
  document.getElementById('stat-new-products').textContent = (stats.new_products_count ?? 0).toLocaleString();
  document.getElementById('stat-promotions').textContent = (stats.promotions_count ?? 0).toLocaleString();

  document.getElementById('stat-max-savings').textContent = `現省最高 $${stats.max_savings_twd || 0}`;
  document.getElementById('stat-total-stores').textContent = `總監控 ${(stats.total_monitored_stores || stats.total_stores || 0).toLocaleString()} 間`;
  document.getElementById('stat-total-products').textContent = `總菜品 ${(stats.total_monitored_products || stats.total_products || 0).toLocaleString()} 項`;
}

// -----------------------------------------------------------------------------
// 通用分頁控制條產生器 (每頁 50 筆)
// -----------------------------------------------------------------------------
function renderPaginationComponent({
  containerId,
  currentPage,
  totalPages,
  totalItems,
  pageSize = PAGE_SIZE,
  hasNextPage = false,
  onPageChange
}) {
  const container = document.getElementById(containerId);
  if (!container) return;

  const numTotalItems = typeof totalItems === 'number' ? totalItems : (parseInt(totalItems, 10) || 0);

  if ((totalItems === 0 || totalItems === '0') && !hasNextPage && totalPages <= 1) {
    container.innerHTML = '';
    return;
  }

  if (currentPage === 1 && !hasNextPage && (totalPages <= 1 || (numTotalItems > 0 && numTotalItems <= pageSize))) {
    if (numTotalItems > 0) {
      container.innerHTML = `
        <div class="w-full flex items-center justify-between text-xs text-slate-500 dark:text-slate-400 py-3 border-t border-slate-100 dark:border-slate-800">
          <div>共 <strong class="font-mono text-slate-700 dark:text-slate-200">${typeof totalItems === 'number' ? totalItems.toLocaleString() : totalItems}</strong> 筆</div>
        </div>
      `;
    } else {
      container.innerHTML = '';
    }
    return;
  }

  const startIdx = (currentPage - 1) * pageSize + 1;
  const endIdx = typeof totalItems === 'number' ? Math.min(currentPage * pageSize, totalItems) : `${currentPage * pageSize}`;
  const totalDisplay = typeof totalItems === 'number' ? totalItems.toLocaleString() : totalItems;
  const canGoNext = hasNextPage || (totalPages && currentPage < totalPages);

  let html = `
    <div class="w-full flex flex-col sm:flex-row items-center justify-between gap-3 text-xs text-slate-500 dark:text-slate-400 py-3 border-t border-slate-100 dark:border-slate-800">
      <div>
        顯示第 <strong class="font-mono text-slate-700 dark:text-slate-200">${startIdx.toLocaleString()}</strong> - <strong class="font-mono text-slate-700 dark:text-slate-200">${typeof endIdx === 'number' ? endIdx.toLocaleString() : endIdx}</strong> 筆
        ${totalDisplay ? `，共 <strong class="font-mono text-slate-700 dark:text-slate-200">${totalDisplay}</strong> 筆` : ''} 
        (第 <strong class="font-mono text-slate-700 dark:text-slate-200">${currentPage}</strong>${totalPages && totalPages > 1 ? ` / ${totalPages}` : ''} 頁)
      </div>
      <div class="flex items-center gap-1 flex-wrap justify-center">
  `;

  // First page & Prev button
  if (currentPage > 1) {
    html += `<button onclick="${onPageChange}(1)" class="px-2.5 py-1 rounded-lg border border-slate-200 dark:border-slate-700 hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-600 dark:text-slate-300 font-medium transition-colors">首頁</button>`;
    html += `<button onclick="${onPageChange}(${currentPage - 1})" class="px-2.5 py-1 rounded-lg border border-slate-200 dark:border-slate-700 hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-600 dark:text-slate-300 font-medium transition-colors">上一頁</button>`;
  } else {
    html += `<button disabled class="px-2.5 py-1 rounded-lg border border-slate-100 dark:border-slate-800 text-slate-300 dark:text-slate-700 cursor-not-allowed">上一頁</button>`;
  }

  // Page numbers (smart window)
  const effTotalPages = totalPages || (canGoNext ? currentPage + 1 : currentPage);
  const maxButtons = 5;
  let startPage = Math.max(1, currentPage - Math.floor(maxButtons / 2));
  let endPage = Math.min(effTotalPages, startPage + maxButtons - 1);
  if (endPage - startPage + 1 < maxButtons) {
    startPage = Math.max(1, endPage - maxButtons + 1);
  }

  for (let p = startPage; p <= endPage; p++) {
    if (p === currentPage) {
      html += `<button class="px-3 py-1 rounded-lg bg-emerald-600 text-white font-bold font-mono shadow-sm shadow-emerald-600/20">${p}</button>`;
    } else {
      html += `<button onclick="${onPageChange}(${p})" class="px-3 py-1 rounded-lg border border-slate-200 dark:border-slate-700 hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-600 dark:text-slate-300 font-mono transition-colors">${p}</button>`;
    }
  }

  // Next and Last buttons
  if (canGoNext) {
    html += `<button onclick="${onPageChange}(${currentPage + 1})" class="px-2.5 py-1 rounded-lg border border-slate-200 dark:border-slate-700 hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-600 dark:text-slate-300 font-medium transition-colors">下一頁</button>`;
    if (totalPages && totalPages > 1) {
      html += `<button onclick="${onPageChange}(${totalPages})" class="px-2.5 py-1 rounded-lg border border-slate-200 dark:border-slate-700 hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-600 dark:text-slate-300 font-medium transition-colors">末頁</button>`;
    }
  } else {
    html += `<button disabled class="px-2.5 py-1 rounded-lg border border-slate-100 dark:border-slate-800 text-slate-300 dark:text-slate-700 cursor-not-allowed">下一頁</button>`;
  }

  // Quick jump input
  if (totalPages && totalPages > 5) {
    html += `
      <div class="flex items-center gap-1 ml-1 text-slate-400">
        <span>跳至</span>
        <input type="number" min="1" max="${totalPages}" class="w-12 px-1 py-0.5 text-center font-mono rounded border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-800 dark:text-slate-100 text-xs" onkeydown="if(event.key==='Enter'){const v=parseInt(this.value);if(v>=1&&v<=${totalPages})${onPageChange}(v);}" placeholder="${currentPage}">
        <span>頁</span>
      </div>
    `;
  }

  html += `</div></div>`;
  container.innerHTML = html;
}

function smoothScrollToTab(tabId) {
  const el = document.getElementById(tabId);
  if (el) {
    el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

// -----------------------------------------------------------------------------
// 3. TAB 1: 今日大特價 (-30%+) (每頁 50 筆)
// -----------------------------------------------------------------------------
async function fetchDiscounts(page = 1) {
  APP_STATE.discountsPage = page;
  const { discountMinPct, discountMinSavings, discountSort, discountCategory, discountSearch } = APP_STATE.filters;
  let items = APP_STATE.rawDiscounts && APP_STATE.rawDiscounts.length > 0 ? APP_STATE.rawDiscounts : (APP_STATE.discounts || []);

  items = items.filter(item => {
    if (!item.current_price || item.current_price <= 0) return false;
    if (item.discount_pct < discountMinPct) return false;
    if (item.savings_amount < discountMinSavings) return false;
    if (discountCategory !== '全部' && !item.category_name.includes(discountCategory)) return false;
    if (discountSearch) {
      const kw = discountSearch.toLowerCase();
      if (!item.product_name.toLowerCase().includes(kw) && !item.store_name.toLowerCase().includes(kw)) {
        return false;
      }
    }
    return true;
  });

  if (discountSort === 'discount_desc') {
    items.sort((a, b) => b.discount_pct - a.discount_pct || b.savings_amount - a.savings_amount);
  } else if (discountSort === 'savings_desc') {
    items.sort((a, b) => b.savings_amount - a.savings_amount || b.discount_pct - a.discount_pct);
  } else if (discountSort === 'price_asc') {
    items.sort((a, b) => a.current_price - b.current_price);
  } else if (discountSort === 'price_desc') {
    items.sort((a, b) => b.current_price - a.current_price);
  }

  APP_STATE.discounts = items;
  renderDiscounts();
}

function changeDiscountsPage(page) {
  fetchDiscounts(page);
  smoothScrollToTab('tab-discounts');
}

function renderDiscounts() {
  const container = document.getElementById('discounts-grid');
  const emptyView = document.getElementById('discounts-empty');
  const items = APP_STATE.discounts || [];
  const total = items.length;
  const page = APP_STATE.discountsPage || 1;
  const totalPages = Math.ceil(total / PAGE_SIZE) || 1;

  const statMaxSavingsEl = document.getElementById('stat-max-savings');
  if (statMaxSavingsEl && items.length > 0) {
    const maxSavings = Math.max(...items.map(i => i.savings_amount || 0));
    statMaxSavingsEl.textContent = `現省最高 $${Math.round(maxSavings)}`;
  }

  if (total === 0) {
    container.innerHTML = '';
    emptyView.classList.remove('hidden');
    renderPaginationComponent({
      containerId: 'discounts-pagination',
      currentPage: 1,
      totalPages: 0,
      totalItems: 0,
      pageSize: PAGE_SIZE,
      onPageChange: 'changeDiscountsPage'
    });
    return;
  }

  emptyView.classList.add('hidden');
  const offset = (page - 1) * PAGE_SIZE;
  const pageItems = items.slice(offset, offset + PAGE_SIZE);

  container.innerHTML = pageItems.map(item => {
    const orderUrl = item.order_action_url || '#';
    const hasPromo = item.promo_type && item.promo_type !== '無';
    const ratingBadge = item.rating_value 
      ? `<span class="inline-flex items-center gap-1 text-xs text-amber-500 font-medium"><i data-lucide="star" class="w-3.5 h-3.5 fill-amber-400"></i>${item.rating_value} (${item.review_count || 0})</span>` 
      : '';

    return `
      <div class="radar-card bg-white dark:bg-slate-900 rounded-2xl p-5 border border-slate-200 dark:border-slate-800 shadow-sm flex flex-col justify-between relative overflow-hidden group">
        <div>
          <div class="flex items-start justify-between gap-2 mb-2">
            <div class="flex-1 min-w-0">
              <span class="inline-block text-xs font-semibold px-2 py-0.5 rounded-md bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300 truncate max-w-full">
                ${escapeHtml(item.category_name || '餐飲主食')}
              </span>
              <h3 class="text-sm font-semibold text-slate-700 dark:text-slate-300 truncate mt-1 flex items-center gap-1.5" title="${escapeHtml(item.store_name)}">
                <i data-lucide="store" class="w-3.5 h-3.5 text-slate-400 shrink-0"></i>
                ${escapeHtml(item.store_name)}
              </h3>
            </div>
            ${ratingBadge}
          </div>

          <h4 class="text-base font-bold text-slate-900 dark:text-white line-clamp-2 mt-1 mb-2 group-hover:text-red-600 dark:group-hover:text-red-400 transition-colors" title="${escapeHtml(item.product_name)}">
            ${escapeHtml(item.product_name)}
          </h4>

          ${item.description ? `<p class="text-xs text-slate-500 dark:text-slate-400 line-clamp-2 mb-3">${escapeHtml(item.description)}</p>` : ''}
        </div>

        <div class="pt-3 border-t border-slate-100 dark:border-slate-800 space-y-3">
          <div class="flex items-baseline justify-between">
            <div>
              <div class="text-xs text-slate-400 line-through">原價 $${Math.round(item.original_price)}</div>
              <div class="text-2xl font-black text-red-600 dark:text-red-400 font-mono flex items-baseline gap-1">
                $${Math.round(item.current_price)}
                <span class="text-xs font-normal text-slate-500 dark:text-slate-400">/ 實質單件</span>
              </div>
            </div>

            <div class="text-right">
              <span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-xl text-xs font-bold text-white badge-discount shadow-sm shadow-red-500/30">
                <i data-lucide="trending-down" class="w-3.5 h-3.5"></i>
                -${item.discount_pct}%
              </span>
              <div class="text-xs font-semibold text-emerald-600 dark:text-emerald-400 mt-1">
                現省 $${Math.round(item.savings_amount)}
              </div>
            </div>
          </div>

          ${hasPromo ? `
            <div class="inline-flex items-center gap-1 px-2 py-0.5 rounded-lg text-xs font-medium bg-purple-50 text-purple-700 dark:bg-purple-950/80 dark:text-purple-300 border border-purple-200 dark:border-purple-800">
              <i data-lucide="gift" class="w-3 h-3"></i>
              ${escapeHtml(item.promo_type)}
            </div>
          ` : ''}

          <div class="grid grid-cols-2 gap-2 pt-1">
            <button data-action="history" data-args="${escapeHtml(JSON.stringify([item.product_id, item.product_name, item.store_name, orderUrl]))}" class="px-3 py-2 text-xs font-medium rounded-xl bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-200 hover:bg-slate-200 dark:hover:bg-slate-700 transition-colors flex items-center justify-center gap-1.5">
              <i data-lucide="line-chart" class="w-3.5 h-3.5"></i>
              價格走勢
            </button>

            <a href="${escapeHtml(safeOrderUrl(orderUrl))}" target="_blank" rel="noopener noreferrer" data-action="order" data-args="${escapeHtml(JSON.stringify([orderUrl, item.product_name, item.store_name]))}" class="px-3 py-2 text-xs font-semibold rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white shadow-md shadow-emerald-600/20 transition-all flex items-center justify-center gap-1.5 active:scale-95">
              <span>前往下單</span>
              <i data-lucide="external-link" class="w-3.5 h-3.5"></i>
            </a>
          </div>
        </div>
      </div>
    `;
  }).join('');

  renderPaginationComponent({
    containerId: 'discounts-pagination',
    currentPage: page,
    totalPages: totalPages,
    totalItems: total,
    pageSize: PAGE_SIZE,
    onPageChange: 'changeDiscountsPage'
  });

  lucide.createIcons();
}

// -----------------------------------------------------------------------------
// 4. TAB 2: 新進店家速報 (New Stores) (每頁 50 筆)
// -----------------------------------------------------------------------------
async function fetchNewStores(page = 1) {
  APP_STATE.storesPage = page;
  const { storeSearch, storeCity, storeSort } = APP_STATE.filters;
  let items = APP_STATE.newStores || [];

  // 地區篩選
  if (storeCity && storeCity !== '全部') {
    items = items.filter(s => matchCityInMemory(s, storeCity));
  }

  // 關鍵字搜尋
  if (storeSearch) {
    const kw = storeSearch.toLowerCase();
    items = items.filter(s => 
      (s.store_name && s.store_name.toLowerCase().includes(kw)) ||
      (s.locality && s.locality.toLowerCase().includes(kw)) ||
      (s.street_address && s.street_address.toLowerCase().includes(kw))
    );
  }

  // 排序
  if (storeSort === 'rating_desc') {
    items.sort((a, b) => (b.rating_value || 0) - (a.rating_value || 0) || (b.review_count || 0) - (a.review_count || 0));
  } else if (storeSort === 'reviews_desc') {
    items.sort((a, b) => (b.review_count || 0) - (a.review_count || 0) || (b.rating_value || 0) - (a.rating_value || 0));
  } else if (storeSort === 'items_desc') {
    items.sort((a, b) => (b.total_menu_items || 0) - (a.total_menu_items || 0));
  } else if (storeSort === 'name_asc') {
    items.sort((a, b) => (a.store_name || '').localeCompare(b.store_name || '', 'zh-TW'));
  }

  APP_STATE.filteredStores = items;
  renderNewStores();
}

function changeStoresPage(page) {
  fetchNewStores(page);
  smoothScrollToTab('tab-stores');
}

function renderNewStores() {
  const container = document.getElementById('new-stores-grid');
  const emptyView = document.getElementById('new-stores-empty');
  const items = APP_STATE.filteredStores || [];
  const total = items.length;
  const page = APP_STATE.storesPage || 1;
  const totalPages = Math.ceil(total / PAGE_SIZE) || 1;

  const counterEl = document.getElementById('new-stores-counter');
  if (counterEl) {
    counterEl.textContent = `${total.toLocaleString()} 間新店`;
  }

  if (total === 0) {
    container.innerHTML = '';
    emptyView.classList.remove('hidden');
    renderPaginationComponent({
      containerId: 'new-stores-pagination',
      currentPage: 1,
      totalPages: 0,
      totalItems: 0,
      pageSize: PAGE_SIZE,
      onPageChange: 'changeStoresPage'
    });
    return;
  }

  emptyView.classList.add('hidden');
  const offset = (page - 1) * PAGE_SIZE;
  const pageItems = items.slice(offset, offset + PAGE_SIZE);

  container.innerHTML = pageItems.map(store => {
    const ratingHtml = store.rating_value 
      ? `<span class="inline-flex items-center gap-1 text-xs text-amber-500 font-bold bg-amber-50 dark:bg-amber-950/80 px-2 py-0.5 rounded-lg"><i data-lucide="star" class="w-3.5 h-3.5 fill-amber-400"></i>${store.rating_value} (${store.review_count || 0})</span>` 
      : '<span class="text-xs text-slate-400">全新開張</span>';

    return `
      <div class="radar-card bg-white dark:bg-slate-900 rounded-2xl p-5 border border-emerald-100 dark:border-emerald-950/60 shadow-sm flex flex-col justify-between group">
        <div>
          <div class="flex items-start justify-between gap-2 mb-2">
            <span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-bold text-white bg-emerald-600">
              <i data-lucide="sparkles" class="w-3 h-3"></i>全新首度進駐
            </span>
            ${ratingHtml}
          </div>

          <h3 class="text-lg font-bold text-slate-900 dark:text-white group-hover:text-emerald-600 dark:group-hover:text-emerald-400 transition-colors">
            ${escapeHtml(store.store_name)}
          </h3>

          <div class="mt-2 space-y-1 text-xs text-slate-500 dark:text-slate-400">
            <div class="flex items-center gap-1.5 truncate">
              <i data-lucide="map-pin" class="w-3.5 h-3.5 text-slate-400 shrink-0"></i>
              <span>${escapeHtml(store.locality || '')} ${escapeHtml(store.street_address || '')}</span>
            </div>
            <div class="flex items-center gap-1.5 truncate">
              <i data-lucide="utensils-crossed" class="w-3.5 h-3.5 text-slate-400 shrink-0"></i>
              <span>${escapeHtml(store.cuisines || '特色餐飲')}</span>
            </div>
          </div>
        </div>

        <div class="pt-4 mt-3 border-t border-slate-100 dark:border-slate-800 flex items-center justify-between">
          <span class="text-xs font-medium text-slate-600 dark:text-slate-300">
            菜單共 <strong>${store.total_menu_items || 0}</strong> 道菜品
          </span>

          <a href="${escapeHtml(safeOrderUrl(store.order_action_url || store.store_url || '#'))}" target="_blank" rel="noopener noreferrer" data-action="order" data-args="${escapeHtml(JSON.stringify([store.order_action_url || store.store_url || '', '', store.store_name]))}" class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold bg-emerald-50 text-emerald-700 hover:bg-emerald-100 dark:bg-emerald-950 dark:text-emerald-300 dark:hover:bg-emerald-900 transition-colors">
            <span>瀏覽店家</span>
            <i data-lucide="arrow-up-right" class="w-3.5 h-3.5"></i>
          </a>
        </div>
      </div>
    `;
  }).join('');

  renderPaginationComponent({
    containerId: 'new-stores-pagination',
    currentPage: page,
    totalPages: totalPages,
    totalItems: total,
    pageSize: PAGE_SIZE,
    onPageChange: 'changeStoresPage'
  });

  lucide.createIcons();
}

// -----------------------------------------------------------------------------
// 5. TAB 3: 老店新菜推薦 (Tab 3) (每頁 50 筆)
// -----------------------------------------------------------------------------
async function fetchNewProducts(page = 1) {
  APP_STATE.productsPage = page;
  const { productSearch } = APP_STATE.filters;
  let items = APP_STATE.newProducts || [];

  if (productSearch) {
    const kw = productSearch.toLowerCase();
    items = items.filter(p => 
      (p.product_name && p.product_name.toLowerCase().includes(kw)) ||
      (p.store_name && p.store_name.toLowerCase().includes(kw))
    );
  }

  APP_STATE.filteredProducts = items;
  renderNewProducts();
}

function changeProductsPage(page) {
  fetchNewProducts(page);
  smoothScrollToTab('tab-products');
}

function renderNewProducts() {
  const container = document.getElementById('new-products-grid');
  const emptyView = document.getElementById('new-products-empty');
  const items = APP_STATE.filteredProducts || [];
  const total = items.length;
  const page = APP_STATE.productsPage || 1;
  const totalPages = Math.ceil(total / PAGE_SIZE) || 1;

  const counterEl = document.getElementById('new-products-counter');
  if (counterEl) {
    counterEl.textContent = `${total.toLocaleString()} 道新品`;
  }

  if (total === 0) {
    container.innerHTML = '';
    emptyView.classList.remove('hidden');
    renderPaginationComponent({
      containerId: 'new-products-pagination',
      currentPage: 1,
      totalPages: 0,
      totalItems: 0,
      pageSize: PAGE_SIZE,
      onPageChange: 'changeProductsPage'
    });
    return;
  }

  emptyView.classList.add('hidden');
  const offset = (page - 1) * PAGE_SIZE;
  const pageItems = items.slice(offset, offset + PAGE_SIZE);

  container.innerHTML = pageItems.map(prod => {
    const promoInfo = calculateEffectivePromo(prod.price, prod.promo_type, prod.quantity);
    const hasPromo = promoInfo.isPromo;
    const hasQtyPromo = promoInfo.totalQty > 1;
    const unitPrice = promoInfo.effPrice;
    const displayUnitPrice = Math.round(unitPrice);
    const discountLabel = promoInfo.discountText ? `折合 ${promoInfo.discountText}` : '';

    return `
      <div class="radar-card bg-white dark:bg-slate-900 rounded-2xl p-5 border border-blue-100 dark:border-blue-950/60 shadow-sm flex flex-col justify-between group">
        <div>
          <div class="flex items-center justify-between gap-2 mb-2">
            <div class="flex items-center gap-1.5 flex-wrap">
              <span class="inline-block text-xs font-semibold px-2 py-0.5 rounded-md bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-300">
                ${escapeHtml(prod.category_name || '新上架')}
              </span>
              ${hasPromo ? `
                <span class="inline-flex items-center gap-0.5 text-[11px] font-bold px-1.5 py-0.5 rounded-md bg-purple-50 text-purple-700 dark:bg-purple-950/80 dark:text-purple-300 border border-purple-200 dark:border-purple-800">
                  <i data-lucide="gift" class="w-3 h-3"></i>${escapeHtml(prod.promo_type)}
                </span>
              ` : ''}
              ${discountLabel ? `
                <span class="inline-block text-[11px] font-bold px-1.5 py-0.5 rounded-md bg-red-50 text-red-600 dark:bg-red-950/80 dark:text-red-300 border border-red-200 dark:border-red-800">
                  ${escapeHtml(discountLabel)}
                </span>
              ` : ''}
            </div>
            <div class="flex items-baseline gap-0.5">
              <span class="text-xs font-mono font-bold text-slate-900 dark:text-white bg-slate-100 dark:bg-slate-800 px-2 py-0.5 rounded-md">
                $${displayUnitPrice}
              </span>
              ${hasQtyPromo ? `<span class="text-[10px] text-slate-400">/件</span>` : ''}
            </div>
          </div>

          <h4 class="text-base font-bold text-slate-900 dark:text-white group-hover:text-blue-600 dark:group-hover:text-blue-400 transition-colors">
            ${escapeHtml(prod.product_name)}
          </h4>

          <div class="mt-1 flex items-center gap-1 text-xs text-slate-500 dark:text-slate-400 truncate">
            <i data-lucide="store" class="w-3.5 h-3.5 shrink-0"></i>
            <span>${escapeHtml(prod.store_name)}</span>
          </div>

          ${prod.description ? `<p class="text-xs text-slate-500 dark:text-slate-400 line-clamp-2 mt-2">${escapeHtml(prod.description)}</p>` : ''}
        </div>

        <div class="pt-3 mt-3 border-t border-slate-100 dark:border-slate-800 flex items-center justify-end">
          <a href="${escapeHtml(safeOrderUrl(prod.order_action_url || '#'))}" target="_blank" rel="noopener noreferrer" data-action="order" data-args="${escapeHtml(JSON.stringify([prod.order_action_url || '', prod.product_name, prod.store_name]))}" class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold bg-blue-600 hover:bg-blue-700 text-white shadow-md shadow-blue-600/20 transition-all active:scale-95">
            <span>前往點餐</span>
            <i data-lucide="external-link" class="w-3.5 h-3.5"></i>
          </a>
        </div>
      </div>
    `;
  }).join('');

  renderPaginationComponent({
    containerId: 'new-products-pagination',
    currentPage: page,
    totalPages: totalPages,
    totalItems: total,
    pageSize: PAGE_SIZE,
    onPageChange: 'changeProductsPage'
  });

  lucide.createIcons();
}

// -----------------------------------------------------------------------------
// 促銷計算與台灣用語折數工具
// -----------------------------------------------------------------------------
function formatTaiwanDiscount(ratio) {
  if (ratio <= 0 || ratio >= 1 || isNaN(ratio)) return '';
  const pct = Math.round(ratio * 100);
  if (pct % 10 === 0) {
    return `${pct / 10}折`;
  }
  return `${pct}折`;
}

function calculateEffectivePromo(price, promoType, quantity) {
  const p = Number(price) || 0;
  const qty = Number(quantity) || 1;
  const type = String(promoType || '').trim();

  // 1. 匹配「買X送Y」中文或數字
  const mBuy = type.match(/買\s*([0-9一二兩三四五])\s*送\s*([0-9一二兩三四五])/);
  if (mBuy) {
    const digitMap = { '1': 1, '2': 2, '3': 3, '4': 4, '5': 5, '一': 1, '二': 2, '兩': 2, '三': 3, '四': 4, '五': 5 };
    const b = digitMap[mBuy[1]] || 1;
    const f = digitMap[mBuy[2]] || 1;
    const totalQty = b + f;
    const totalCost = p * b;
    const eff = totalQty > 0 ? (totalCost / totalQty) : p;
    const ratio = totalQty > 0 ? (b / totalQty) : 1;
    return {
      effPrice: eff,
      totalQty: totalQty,
      buyQty: b,
      freeQty: f,
      ratio: ratio,
      discountText: formatTaiwanDiscount(ratio),
      rawPrice: p,
      isPromo: true
    };
  }

  // 2. 匹配「第2件半價」/「第2杯半價」
  if (/第\s*[2二兩]\s*[件杯項份]\s*半價/.test(type)) {
    const eff = (p * 1.5) / 2;
    const ratio = 0.75;
    return {
      effPrice: eff,
      totalQty: 2,
      buyQty: 2,
      freeQty: 0,
      ratio: ratio,
      discountText: '75折',
      rawPrice: p,
      isPromo: true
    };
  }

  // 3. 匹配「加1元多1件」
  if (/加\s*[1一]\s*元多\s*[1一]\s*件/.test(type)) {
    const eff = (p + 1) / 2;
    const ratio = p > 0 ? (eff / p) : 0.5;
    return {
      effPrice: eff,
      totalQty: 2,
      buyQty: 1,
      freeQty: 1,
      ratio: ratio,
      discountText: formatTaiwanDiscount(ratio),
      rawPrice: p,
      isPromo: true
    };
  }

  // 4. 匹配「N折」/「N折特惠」
  const mDisc = type.match(/([1-9](?:\.[1-9])?)\s*折/);
  if (mDisc) {
    const discNum = parseFloat(mDisc[1]);
    const ratio = discNum < 10 ? (discNum / 10) : (discNum / 100);
    return {
      effPrice: p,
      totalQty: qty,
      buyQty: qty,
      freeQty: 0,
      ratio: ratio,
      discountText: `${discNum}折`,
      rawPrice: p,
      isPromo: true
    };
  }

  // 5. 若 quantity > 1 (組合多入包裝)
  if (qty > 1) {
    const eff = p / qty;
    const ratio = 1 / qty;
    return {
      effPrice: eff,
      totalQty: qty,
      buyQty: qty,
      freeQty: 0,
      ratio: ratio,
      discountText: formatTaiwanDiscount(ratio),
      rawPrice: p,
      isPromo: true
    };
  }

  return {
    effPrice: p,
    totalQty: 1,
    buyQty: 1,
    freeQty: 0,
    ratio: 1,
    discountText: '',
    rawPrice: p,
    isPromo: (type !== '無' && type !== '')
  };
}

// -----------------------------------------------------------------------------
// 6. TAB 4: 折扣活動專區 (Promotions) (每頁 50 筆)
// -----------------------------------------------------------------------------
async function fetchPromotions(page = 1) {
  APP_STATE.promosPage = page;
  const { promoSearch, promoType, promoSort } = APP_STATE.filters;
  let items = (APP_STATE.promotions || []).filter(p => p.price > 0 && (p.quantity > 1 || (p.promo_type && p.promo_type !== '無' && p.promo_type !== '')));

  // 活動類型篩選
  if (promoType && promoType !== '全部') {
    if (promoType === '買一送一') {
      items = items.filter(p => /買.*送/.test(p.promo_type));
    } else if (promoType === '半價') {
      items = items.filter(p => /半價/.test(p.promo_type));
    } else if (promoType === '加1元') {
      items = items.filter(p => /加.*元多.*件/.test(p.promo_type));
    } else if (promoType === '折') {
      items = items.filter(p => /折/.test(p.promo_type));
    } else if (promoType === '多入') {
      items = items.filter(p => p.quantity > 1);
    }
  }

  // 關鍵字搜尋
  if (promoSearch) {
    const kw = promoSearch.toLowerCase();
    items = items.filter(p => 
      (p.product_name && p.product_name.toLowerCase().includes(kw)) ||
      (p.store_name && p.store_name.toLowerCase().includes(kw)) ||
      (p.category_name && p.category_name.toLowerCase().includes(kw))
    );
  }

  // 排序
  if (promoSort === 'price_asc') {
    items.sort((a, b) => (a.eff_price || a.price || 0) - (b.eff_price || b.price || 0));
  } else if (promoSort === 'price_desc') {
    items.sort((a, b) => (b.eff_price || b.price || 0) - (a.eff_price || a.price || 0));
  } else if (promoSort === 'rating_desc') {
    items.sort((a, b) => (b.rating_value || 0) - (a.rating_value || 0) || (a.eff_price || 0) - (b.eff_price || 0));
  } else if (promoSort === 'name_asc') {
    items.sort((a, b) => (a.product_name || '').localeCompare(p.product_name || '', 'zh-TW'));
  }

  APP_STATE.filteredPromotions = items;
  renderPromotions();
}

function changePromosPage(page) {
  fetchPromotions(page);
  smoothScrollToTab('tab-promos');
}

function renderPromotions() {
  const container = document.getElementById('promos-grid');
  const emptyView = document.getElementById('promos-empty');
  const items = APP_STATE.filteredPromotions || [];
  const total = items.length;
  const page = APP_STATE.promosPage || 1;
  const totalPages = Math.ceil(total / PAGE_SIZE) || 1;

  const counterEl = document.getElementById('promos-counter');
  if (counterEl) {
    counterEl.textContent = `${total.toLocaleString()} 筆特惠`;
  }

  if (total === 0) {
    container.innerHTML = '';
    emptyView.classList.remove('hidden');
    renderPaginationComponent({
      containerId: 'promos-pagination',
      currentPage: 1,
      totalPages: 0,
      totalItems: 0,
      pageSize: PAGE_SIZE,
      onPageChange: 'changePromosPage'
    });
    return;
  }

  emptyView.classList.add('hidden');
  const offset = (page - 1) * PAGE_SIZE;
  const pageItems = items.slice(offset, offset + PAGE_SIZE);

  container.innerHTML = pageItems.map(p => {
    const promoInfo = calculateEffectivePromo(p.price, p.promo_type, p.quantity);
    const eff = promoInfo.effPrice;
    const discountLabel = promoInfo.discountText ? `折合 ${promoInfo.discountText}` : '';
    
    return `
      <div class="radar-card bg-white dark:bg-slate-900 rounded-2xl p-5 border border-purple-100 dark:border-purple-950/60 shadow-sm flex flex-col justify-between group">
        <div>
          <div class="flex items-center justify-between gap-2 mb-2">
            <span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-bold text-white badge-promo">
              <i data-lucide="gift" class="w-3 h-3"></i>
              ${escapeHtml(p.promo_type)}
            </span>
            <span class="text-xs text-slate-400 truncate max-w-[120px]">${escapeHtml(p.category_name || '')}</span>
          </div>

          <h3 class="text-sm font-semibold text-slate-600 dark:text-slate-400 flex items-center gap-1 truncate mb-1">
            <i data-lucide="store" class="w-3.5 h-3.5 text-slate-400"></i>
            ${escapeHtml(p.store_name)}
          </h3>

          <h4 class="text-base font-bold text-slate-900 dark:text-white group-hover:text-purple-600 dark:group-hover:text-purple-400 transition-colors line-clamp-2">
            ${escapeHtml(p.product_name)}
          </h4>
        </div>

        <div class="pt-3 mt-3 border-t border-slate-100 dark:border-slate-800">
          <div class="flex items-baseline justify-between mb-3">
            <div>
              <div class="text-xs text-slate-400">標價 $${Math.round(p.price)}${promoInfo.totalQty > 1 ? ` (共 ${promoInfo.totalQty} 份)` : (p.quantity > 1 ? ` (共 ${p.quantity} 份)` : '')}</div>
              <div class="text-xl font-black text-purple-600 dark:text-purple-400 font-mono">
                實質單價 $${Math.round(eff)}
              </div>
            </div>
            ${discountLabel ? `
              <span class="text-xs font-bold px-2.5 py-1 rounded-lg bg-purple-50 text-purple-700 dark:bg-purple-950 dark:text-purple-300 border border-purple-200 dark:border-purple-800">
                ${escapeHtml(discountLabel)}
              </span>
            ` : ''}
          </div>

          <a href="${escapeHtml(safeOrderUrl(p.order_action_url || '#'))}" target="_blank" rel="noopener noreferrer" data-action="order" data-args="${escapeHtml(JSON.stringify([p.order_action_url || '', p.product_name, p.store_name]))}" class="w-full py-2 rounded-xl text-xs font-semibold bg-purple-600 hover:bg-purple-700 text-white shadow-md shadow-purple-600/20 transition-all flex items-center justify-center gap-1.5 active:scale-95">
            <span>前往搶購</span>
            <i data-lucide="external-link" class="w-3.5 h-3.5"></i>
          </a>
        </div>
      </div>
    `;
  }).join('');

  renderPaginationComponent({
    containerId: 'promos-pagination',
    currentPage: page,
    totalPages: totalPages,
    totalItems: total,
    pageSize: PAGE_SIZE,
    onPageChange: 'changePromosPage'
  });

  lucide.createIcons();
}

// -----------------------------------------------------------------------------
// 7. TAB 5: 全庫商品即時檢索 (DuckDB-WASM 百萬大數據湖倉 + 本地記憶體即打即搜)
// -----------------------------------------------------------------------------
let globalSearchController;
let globalSearchSequence = 0;

const BRAND_SYNONYMS = {
  'cost': ['costco', '好市多', 'cost'],
  'costco': ['costco', '好市多'],
  '好市多': ['costco', '好市多'],
  'cosico': ['costco', '好市多', 'cosico'],
  'mcdonald': ['mcdonald', '麥當勞'],
  'mcdonalds': ['mcdonald', '麥當勞'],
  '麥當勞': ['mcdonald', '麥當勞'],
  'kfc': ['kfc', '肯德基'],
  '肯德基': ['kfc', '肯德基'],
  'starbucks': ['starbucks', '星巴克'],
  '星巴克': ['starbucks', '星巴克'],
  '50嵐': ['50嵐', '五十嵐', '50lan'],
  '五十嵐': ['50嵐', '五十嵐', '50lan'],
  '50lan': ['50嵐', '五十嵐', '50lan'],
  '全聯': ['全聯', 'pxmart'],
  'pxmart': ['全聯', 'pxmart'],
  '家樂福': ['家樂福', 'carrefour'],
  'carrefour': ['家樂福', 'carrefour'],
  'subway': ['subway', '潛艇堡'],
  '摩斯': ['摩斯', 'mos burger', 'mos漢堡', 'mos'],
  'mos': ['摩斯', 'mos burger', 'mos漢堡', 'mos'],
  '漢堡王': ['漢堡王', 'burger king'],
  'burger': ['漢堡王', 'burger king', '漢堡'],
  '必勝客': ['必勝客', 'pizza hut', 'pizzahut'],
  'pizza': ['必勝客', '達美樂', '披薩', '比薩'],
  '達美樂': ['達美樂', 'dominos'],
  'dominos': ['達美樂', 'dominos']
};

function buildKeywordSqlCondition(rawSearch) {
  if (!rawSearch) return '';
  const tokens = rawSearch.toLowerCase().split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return '';

  const tokenClauses = tokens.map(token => {
    const syns = BRAND_SYNONYMS[token] || [token];
    const subClauses = syns.map(s => {
      const safe = s.replace(/'/g, "''");
      return `(product_name ILIKE '%${safe}%' OR store_name ILIKE '%${safe}%' OR category_name ILIKE '%${safe}%')`;
    });
    return subClauses.length === 1 ? subClauses[0] : `(${subClauses.join(' OR ')})`;
  });

  return tokenClauses.join(' AND ');
}

const CITY_DISTRICT_MAP = {
  '台北市': ['台北', '臺北', '大安', '信義', '中正', '中山', '松山', '萬華', '文山', '大同', '南港', '內湖', '士林', '北投'],
  '新北市': ['新北', '板橋', '三重', '中和', '永和', '新莊', '新店', '土城', '蘆洲', '樹林', '汐止', '鶯歌', '三峽', '淡水', '瑞芳', '五股', '泰山', '林口', '深坑', '八里'],
  '桃園市': ['桃園', '中壢', '平鎮', '八德', '楊梅', '蘆竹', '大溪', '龜山', '大園', '觀音', '新屋', '龍潭'],
  '台中市': ['台中', '臺中', '南屯', '西屯', '北屯', '豐原', '大里', '太平', '沙鹿', '清水', '大甲', '烏日', '霧峰', '潭子', '大雅', '后里', '神岡', '大肚', '龍井'],
  '台南市': ['台南', '臺南', '永康', '安南', '安平', '新營', '善化', '麻豆', '佳里', '新化', '歸仁', '仁德'],
  '高雄市': ['高雄', '新興', '前金', '苓雅', '鹽埕', '鼓山', '旗津', '前鎮', '三民', '楠梓', '小港', '左營', '仁武', '大社', '岡山', '鳳山', '大寮', '鳥松'],
  '新竹市': ['新竹', '竹北', '竹東', '新埔', '湖口', '新豐', '芎林', '香山'],
  '彰化縣': ['彰化', '員林', '和美', '鹿港', '溪湖', '二林', '田中', '北斗', '花壇', '大村'],
  '基隆市': ['基隆', '仁愛', '七堵', '安樂'],
  '宜蘭縣': ['宜蘭', '羅東', '蘇澳', '頭城', '礁溪', '冬山'],
  '花蓮縣': ['花蓮', '吉安', '新城', '壽豐', '玉里'],
  '屏東縣': ['屏東', '潮州', '東港', '恆春', '萬丹', '內埔'],
  '苗栗縣': ['苗栗', '竹南', '頭份', '後龍', '苑裡', '通霄'],
  '雲林縣': ['雲林', '斗六', '斗南', '虎尾', '西螺', '北港', '麥寮'],
  '嘉義市': ['嘉義', '太保', '朴子', '民雄', '水上', '中埔'],
  '南投縣': ['南投', '埔里', '草屯', '竹山'],
  '台東縣': ['台東', '臺東', '成功', '關山', '卑南'],
  '澎湖縣': ['澎湖', '金門', '連江', '馬公']
};

function buildCitySqlCondition(cityFilter) {
  if (!cityFilter || cityFilter === '全部') return '';
  const dists = CITY_DISTRICT_MAP[cityFilter];
  if (dists && dists.length > 0) {
    const likeClauses = dists.map(d => {
      const safe = d.replace(/'/g, "''");
      return `(city LIKE '%${safe}%' OR locality LIKE '%${safe}%' OR street_address LIKE '%${safe}%' OR store_name LIKE '%${safe}%')`;
    });
    return `(${likeClauses.join(' OR ')})`;
  }
  const safe = cityFilter.replace(/'/g, "''");
  return `(city ILIKE '%${safe}%' OR locality ILIKE '%${safe}%' OR street_address ILIKE '%${safe}%' OR store_name ILIKE '%${safe}%')`;
}

function matchCityInMemory(item, cityFilter) {
  if (!cityFilter || cityFilter === '全部') return true;
  const dists = CITY_DISTRICT_MAP[cityFilter];
  const full = `${item.city || ''} ${item.locality || ''} ${item.street_address || ''} ${item.store_name || ''}`.toLowerCase();
  if (dists) {
    return dists.some(d => full.includes(d.toLowerCase()));
  }
  return full.includes(cityFilter.toLowerCase());
}

function convertArrowTableToObjects(table) {
  if (!table) return [];
  const fields = table.schema ? table.schema.fields.map(f => f.name) : [];
  const rows = [];
  const numRows = table.numRows || 0;

  if (numRows > 0 && fields.length > 0) {
    try {
      const columns = {};
      for (const f of fields) {
        columns[f] = table.getChild(f);
      }
      for (let i = 0; i < numRows; i++) {
        const row = {};
        for (const f of fields) {
          let v = columns[f] ? columns[f].get(i) : null;
          if (typeof v === 'bigint') {
            v = Number(v);
          }
          row[f] = v;
        }
        rows.push(row);
      }
      return rows;
    } catch (e) {
      console.warn('Arrow fast column extract fallback:', e);
    }
  }

  try {
    return table.toArray().map(r => {
      const obj = typeof r.toJSON === 'function' ? r.toJSON() : Object.fromEntries(Object.entries(r));
      const clean = {};
      for (const [k, v] of Object.entries(obj)) {
        clean[k] = typeof v === 'bigint' ? Number(v) : v;
      }
      return clean;
    });
  } catch (e) {
    console.error('Arrow table conversion fallback error:', e);
    return [];
  }
}

const CITY_PARTITION_MAP = {
  '台北市': 'catalog_taipei.parquet',
  '新北市': 'catalog_newtaipei.parquet',
  '桃園市': 'catalog_taoyuan.parquet',
  '台中市': 'catalog_taichung.parquet',
  '台南市': 'catalog_tainan.parquet',
  '高雄市': 'catalog_kaohsiung.parquet',
  '新竹市': 'catalog_hsinchu.parquet',
  '彰化縣': 'catalog_other.parquet',
  '基隆市': 'catalog_other.parquet',
  '宜蘭縣': 'catalog_other.parquet',
  '花蓮縣': 'catalog_other.parquet',
  '屏東縣': 'catalog_other.parquet',
  '苗栗縣': 'catalog_other.parquet',
  '雲林縣': 'catalog_other.parquet',
  '嘉義市': 'catalog_other.parquet',
  '南投縣': 'catalog_other.parquet',
  '台東縣': 'catalog_other.parquet',
  '澎湖縣': 'catalog_other.parquet'
};

function executeInMemoryGlobalSearch(page = 1) {
  const sortMode = APP_STATE.filters.globalSort || 'rating_desc';
  const rawSearch = (APP_STATE.filters.globalSearch || '').trim();
  const cityFilter = APP_STATE.filters.globalCity || '全部';
  const limit = PAGE_SIZE;

  // 1. 取得品庫快照資料集 (20,000+ 筆)
  let items = APP_STATE.allProducts && APP_STATE.allProducts.length > 0 
    ? [...APP_STATE.allProducts] 
    : [...(APP_STATE.rawDiscounts || []), ...(APP_STATE.newProducts || []), ...(APP_STATE.promotions || [])];

  // 2. 關鍵字多語意與品牌同義字比對
  if (rawSearch) {
    const tokens = rawSearch.toLowerCase().split(/\s+/).filter(Boolean);
    items = items.filter(p => {
      const pName = (p.product_name || '').toLowerCase();
      const sName = (p.store_name || '').toLowerCase();
      const cName = (p.category_name || '').toLowerCase();
      const fullText = `${pName} ${sName} ${cName}`;

      return tokens.every(token => {
        const syns = BRAND_SYNONYMS[token] || [token];
        return syns.some(s => fullText.includes(s.toLowerCase()));
      });
    });
  }

  // 3. 縣市分區比對
  if (cityFilter && cityFilter !== '全部') {
    items = items.filter(p => matchCityInMemory(p, cityFilter));
  }

  // 4. 促銷活動過濾
  if (sortMode === 'promo_only') {
    items = items.filter(p => p.promo_type && p.promo_type !== '無' && p.promo_type !== '');
  }

  // 5. 排序演算法
  if (sortMode === 'price_asc') {
    items.sort((a, b) => (Number(a.eff_price || a.price) || 0) - (Number(b.eff_price || b.price) || 0) || (b.rating_value || 0) - (a.rating_value || 0));
  } else if (sortMode === 'price_desc') {
    items.sort((a, b) => (Number(b.eff_price || b.price) || 0) - (Number(a.eff_price || a.price) || 0) || (b.rating_value || 0) - (a.rating_value || 0));
  } else if (sortMode === 'name_asc') {
    items.sort((a, b) => String(a.product_name || '').localeCompare(String(b.product_name || '')));
  } else if (sortMode === 'promo_first') {
    items.sort((a, b) => {
      const aPromo = (a.promo_type && a.promo_type !== '無' && a.promo_type !== '') ? 0 : 1;
      const bPromo = (b.promo_type && b.promo_type !== '無' && b.promo_type !== '') ? 0 : 1;
      if (aPromo !== bPromo) return aPromo - bPromo;
      return (b.rating_value || 0) - (a.rating_value || 0) || (Number(a.eff_price || a.price) || 0) - (Number(b.eff_price || b.price) || 0);
    });
  } else {
    // 預設: 店家評分最高優先 (rating_desc)
    items.sort((a, b) => (b.rating_value || 0) - (a.rating_value || 0) || (Number(a.eff_price || a.price) || 0) - (Number(b.eff_price || b.price) || 0));
  }

  const total = items.length;
  const offset = (page - 1) * limit;
  const pageRows = items.slice(offset, offset + limit);
  const totalPages = Math.ceil(total / limit) || 1;

  APP_STATE.globalProducts = pageRows;
  APP_STATE.globalHasNext = page < totalPages;
  APP_STATE.globalTotalPages = totalPages;
  APP_STATE.globalTotalItems = total;

  renderGlobalProducts();
}

async function fetchGlobalProducts(page = 1) {
  globalSearchController?.abort();
  const controller = new AbortController();
  globalSearchController = controller;
  const sequence = ++globalSearchSequence;

  APP_STATE.globalPage = page;
  const sortMode = APP_STATE.filters.globalSort || 'rating_desc';
  const rawSearch = (APP_STATE.filters.globalSearch || '').trim();
  const cityFilter = APP_STATE.filters.globalCity || '全部';
  const limit = PAGE_SIZE;
  const offset = (page - 1) * limit;

  // 1. DuckDB 尚未就緒或不可用時，立即執行極速本地記憶體快照檢索 (0ms 延遲)
  if (!DUCKDB_READY || !DUCKDB_CONN) {
    executeInMemoryGlobalSearch(page);
    return;
  }

  // 2. 決定此縣市所對應的 Parquet 切片
  let targetTable = 'taiwan_catalog.parquet';
  if (cityFilter && cityFilter !== '全部' && CITY_PARTITION_MAP[cityFilter]) {
    targetTable = CITY_PARTITION_MAP[cityFilter];
  }

  // 3. 若當前畫面尚未有商品，先以本地記憶體快速預填，防止畫面空白卡頓
  if (!APP_STATE.globalProducts || APP_STATE.globalProducts.length === 0) {
    executeInMemoryGlobalSearch(page);
  }

  // 4. 動態掛載縣市切片並執行全量 DuckDB SQL 查詢 (100% 完整百萬品庫)
  try {
    const ok = await ensureParquetRegistered(targetTable);
    if (!ok || controller.signal.aborted || sequence !== globalSearchSequence) {
      if (!ok) executeInMemoryGlobalSearch(page);
      return;
    }

    let whereClauses = ["price >= 1"];

    // 若為全台總表或 other 分區，加入縣市行政區 LIKE 條件
    if (cityFilter && cityFilter !== '全部') {
      if (targetTable === 'taiwan_catalog.parquet' || targetTable === 'catalog_other.parquet') {
        const cityClause = buildCitySqlCondition(cityFilter);
        if (cityClause) whereClauses.push(cityClause);
      }
    }

    if (sortMode === 'promo_only') {
      whereClauses.push(`promo_type != '無' AND promo_type != ''`);
    }

    if (rawSearch) {
      const kwClause = buildKeywordSqlCondition(rawSearch);
      if (kwClause) whereClauses.push(`(${kwClause})`);
    }

    const whereSql = whereClauses.join(" AND ");

    let orderSql = "";
    if (sortMode === 'price_asc') orderSql = "ORDER BY eff_price ASC, rating_value DESC NULLS LAST";
    else if (sortMode === 'price_desc') orderSql = "ORDER BY eff_price DESC, rating_value DESC NULLS LAST";
    else if (sortMode === 'name_asc') orderSql = "ORDER BY product_name ASC, rating_value DESC NULLS LAST";
    else if (sortMode === 'promo_first') orderSql = "ORDER BY CASE WHEN promo_type != '無' AND promo_type != '' THEN 0 ELSE 1 END, rating_value DESC NULLS LAST, eff_price ASC";
    else orderSql = "ORDER BY rating_value DESC NULLS LAST, eff_price ASC";

    const fetchLimit = limit + 1;
    const sql = `SELECT product_id, store_id, store_name, category_name, product_name, price, quantity, promo_type, eff_price, description, order_action_url, rating_value, review_count, locality, street_address, city, is_open, crawled_time ` +
      `FROM '${targetTable}' ` +
      `WHERE ${whereSql} ${orderSql} LIMIT ${fetchLimit} OFFSET ${offset}`;

    const dataResult = await DUCKDB_CONN.query(sql);

    if (sequence !== globalSearchSequence) return;

    const allRows = convertArrowTableToObjects(dataResult);
    const hasNextPage = allRows.length > limit;
    const pageRows = allRows.slice(0, limit);

    APP_STATE.globalProducts = pageRows;
    APP_STATE.globalHasNext = hasNextPage;
    APP_STATE.globalTotalPages = hasNextPage ? Math.max(page + 1, APP_STATE.globalTotalPages || 1) : page;
    APP_STATE.globalTotalItems = hasNextPage ? `${page * limit}+` : `${(page - 1) * limit + pageRows.length}`;

    renderGlobalProducts();
  } catch (err) {
    if (sequence !== globalSearchSequence) return;
    console.warn('DuckDB 湖倉查詢未果，切換為本地記憶體即時檢索:', err);
    executeInMemoryGlobalSearch(page);
  }
}

function renderGlobalProducts() {
  const container = document.getElementById('global-products-grid');
  const items = APP_STATE.globalProducts;
  const page = APP_STATE.globalPage || 1;
  const totalPages = APP_STATE.globalTotalPages || 1;
  const totalItems = APP_STATE.globalTotalItems || 0;
  const hasNextPage = APP_STATE.globalHasNext || false;

  if (container) {
    container.style.opacity = '1';
  }

  if (!items || items.length === 0) {
    container.innerHTML = `
      <div class="col-span-1 md:col-span-2 lg:col-span-3 py-16 text-center">
        <div class="w-16 h-16 rounded-full bg-slate-100 dark:bg-slate-800 flex items-center justify-center text-slate-400 mx-auto mb-3">
          <i data-lucide="search-x" class="w-8 h-8"></i>
        </div>
        <h3 class="text-base font-bold text-slate-800 dark:text-slate-200">查無相符商品</h3>
        <p class="text-xs text-slate-500 dark:text-slate-400 mt-1">請嘗試縮短關鍵字、切換縣市為「全台灣」或選擇不同排序條件</p>
      </div>
    `;
    renderPaginationComponent({
      containerId: 'global-pagination',
      currentPage: 1,
      totalPages: 0,
      totalItems: 0,
      pageSize: PAGE_SIZE,
      hasNextPage: false,
      onPageChange: 'changeGlobalPage'
    });
    lucide.createIcons();
    return;
  }

  container.innerHTML = items.map(p => {
    const promoInfo = calculateEffectivePromo(p.price, p.promo_type, p.quantity);
    const hasPromo = promoInfo.isPromo;
    const hasQtyPromo = promoInfo.totalQty > 1;
    const unitPrice = promoInfo.effPrice;
    const displayUnitPrice = Math.round(unitPrice);
    const discountLabel = promoInfo.discountText ? `折合 ${promoInfo.discountText}` : '';
    const ratingHtml = p.rating_value 
      ? `<span class="inline-flex items-center gap-0.5 text-xs text-amber-500 font-bold"><i data-lucide="star" class="w-3 h-3 fill-amber-400"></i>${p.rating_value}</span>`
      : '';

    return `
      <div class="radar-card bg-white dark:bg-slate-900 rounded-2xl p-4 border border-slate-200 dark:border-slate-800 shadow-sm flex flex-col justify-between group">
        <div>
          <div class="flex items-center justify-between text-xs text-slate-500 dark:text-slate-400 mb-1.5 gap-2">
            <div class="flex items-center gap-1.5 truncate min-w-0 flex-1" title="${escapeHtml(p.store_name)}">
              <i data-lucide="store" class="w-3.5 h-3.5 shrink-0 text-slate-400"></i>
              <span class="truncate font-semibold text-slate-700 dark:text-slate-300">${escapeHtml(p.store_name)}</span>
              ${ratingHtml}
            </div>
            <div class="text-right shrink-0 flex items-baseline gap-0.5">
              <span class="font-bold text-slate-900 dark:text-white font-mono text-base ${hasQtyPromo ? 'text-purple-600 dark:text-purple-400' : ''}">
                $${displayUnitPrice}
              </span>
              ${hasQtyPromo ? `<span class="text-[11px] font-semibold text-purple-600 dark:text-purple-400">/件</span>` : ''}
            </div>
          </div>

          <h4 class="text-sm font-bold text-slate-900 dark:text-white line-clamp-2 group-hover:text-emerald-600 dark:group-hover:text-emerald-400 transition-colors mb-2" title="${escapeHtml(p.product_name)}">
            ${escapeHtml(p.product_name)}
          </h4>

          <div class="flex items-center gap-1.5 flex-wrap text-xs">
            ${hasPromo ? `
              <span class="inline-flex items-center gap-1 text-[11px] font-bold px-2 py-0.5 rounded-md bg-purple-50 text-purple-700 dark:bg-purple-950/80 dark:text-purple-300 border border-purple-200 dark:border-purple-800">
                <i data-lucide="gift" class="w-3 h-3"></i>${escapeHtml(p.promo_type)}
              </span>
            ` : ''}
            ${discountLabel ? `
              <span class="inline-block text-[11px] font-bold px-2 py-0.5 rounded-md bg-red-50 text-red-600 dark:bg-red-950/80 dark:text-red-300 border border-red-200 dark:border-red-800">
                ${escapeHtml(discountLabel)}
              </span>
            ` : ''}
            <span class="inline-block text-[11px] px-2 py-0.5 rounded-md bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-400">
              ${escapeHtml(p.category_name || '一般')}
            </span>
            ${p.city ? `
              <span class="inline-block text-[11px] px-2 py-0.5 rounded-md bg-emerald-50 text-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300">
                ${escapeHtml(p.city)}
              </span>
            ` : ''}
            ${hasQtyPromo ? `
              <span class="text-[11px] text-slate-400 dark:text-slate-500 font-mono">
                (標價 $${Math.round(p.price)} 共 ${promoInfo.totalQty} 件)
              </span>
            ` : ''}
          </div>
        </div>

        <div class="pt-3 mt-3 border-t border-slate-100 dark:border-slate-800 flex items-center justify-between">
          <button data-action="history" data-args="${escapeHtml(JSON.stringify([String(p.product_id), String(p.product_name), String(p.store_name), String(p.order_action_url || '')]))}" class="text-xs text-slate-500 hover:text-emerald-600 transition-colors flex items-center gap-1">
            <i data-lucide="line-chart" class="w-3.5 h-3.5"></i>走勢
          </button>
          <a href="${escapeHtml(safeOrderUrl(p.order_action_url || '#'))}" target="_blank" rel="noopener noreferrer" data-action="order" data-args="${escapeHtml(JSON.stringify([String(p.order_action_url || ''), String(p.product_name), String(p.store_name)]))}" class="text-xs text-emerald-600 hover:text-emerald-700 dark:text-emerald-400 font-bold flex items-center gap-1 active:scale-95 transition-transform">
            下單 <i data-lucide="arrow-up-right" class="w-3.5 h-3.5"></i>
          </a>
        </div>
      </div>
    `;
  }).join('');

  renderPaginationComponent({
    containerId: 'global-pagination',
    currentPage: page,
    totalPages: totalPages,
    totalItems: totalItems,
    pageSize: PAGE_SIZE,
    hasNextPage: hasNextPage,
    onPageChange: 'changeGlobalPage'
  });

  lucide.createIcons();
}

function changeGlobalPage(page) {
  fetchGlobalProducts(page);
  smoothScrollToTab('tab-global-search');
}

// -----------------------------------------------------------------------------
// 8. 價格走勢圖彈窗 (Price Trend Modal with Chart.js & DuckDB Edge SQL)
// -----------------------------------------------------------------------------
async function showPriceHistoryModal(productId, productName, storeName, orderUrl) {
  const modal = document.getElementById('price-history-modal');
  document.getElementById('modal-store-name').textContent = storeName;
  document.getElementById('modal-product-name').textContent = productName;
  document.getElementById('modal-product-id').textContent = `Product ID: ${productId}`;
  
  const orderBtn = document.getElementById('modal-order-btn');
  if (orderBtn) {
    orderBtn.href = safeOrderUrl(orderUrl);
    orderBtn.onclick = (e) => openUberEatsOrder(e, orderUrl, productName, storeName);
  }

  modal.classList.remove('hidden');
  modal.classList.add('flex');

  let history = (APP_STATE.historyMap && APP_STATE.historyMap[productId]) ? [...APP_STATE.historyMap[productId]] : [];
  
  // 若 history.json 未命中，嘗試從當前已載入之各資料集中尋找該商品的真實即時資訊
  if (history.length === 0) {
    const found = (APP_STATE.rawDiscounts || []).find(x => String(x.product_id) === String(productId))
      || (APP_STATE.promotions || []).find(x => String(x.product_id) === String(productId))
      || (APP_STATE.newProducts || []).find(x => String(x.product_id) === String(productId))
      || (APP_STATE.allProducts || []).find(x => String(x.product_id) === String(productId))
      || (APP_STATE.globalProducts || []).find(x => String(x.product_id) === String(productId));

    if (found) {
      const eff = found.eff_price || (found.quantity > 1 ? Math.round((found.price / found.quantity) * 10) / 10 : found.price);
      history = [{
        crawled_time: found.crawled_time || (APP_STATE.stats && APP_STATE.stats.latest_batch) || '最新',
        price: Number(found.price || eff),
        quantity: Number(found.quantity || 1),
        promo_type: found.promo_type || '無',
        eff_price: Number(eff)
      }];
    }
  }

  // 若歷史紀錄大於 1 筆，按採集時間升冪排序
  if (history.length > 1) {
    history.sort((a, b) => String(a.crawled_time).localeCompare(String(b.crawled_time)));
  }

  const tbody = document.getElementById('modal-history-tbody');
  if (history.length > 0) {
    const rowsHtml = history.map(h => `
      <tr class="hover:bg-slate-50 dark:hover:bg-slate-800/50 transition-colors">
        <td class="px-3 py-2 font-mono text-slate-600 dark:text-slate-300 text-xs">${formatBatchDate(h.crawled_time)}</td>
        <td class="px-3 py-2 font-mono font-medium text-xs">$${Math.round(h.price)}</td>
        <td class="px-3 py-2"><span class="px-2 py-0.5 rounded text-[11px] font-medium ${h.promo_type && h.promo_type !== '無' ? 'bg-purple-100 dark:bg-purple-950 text-purple-700 dark:text-purple-300' : 'bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-400'}">${escapeHtml(h.promo_type || '無')}</span></td>
        <td class="px-3 py-2 text-right font-mono font-bold text-emerald-600 dark:text-emerald-400 text-xs">$${Number(h.eff_price).toFixed(h.eff_price % 1 === 0 ? 0 : 1)}</td>
      </tr>
    `).join('');

    const hintHtml = history.length === 1 
      ? `<tr><td colspan="4" class="px-3 py-2 text-center text-xs text-slate-400 bg-slate-50/50 dark:bg-slate-800/30">📌 本品項首度收錄於此批次，後續每日採集將持續自動累積價格趨勢曲線</td></tr>`
      : '';

    tbody.innerHTML = rowsHtml + hintHtml;
  } else {
    tbody.innerHTML = `<tr><td colspan="4" class="px-3 py-4 text-center text-slate-400 text-xs">目前批次為最新快照記錄</td></tr>`;
  }

  const ctx = document.getElementById('priceHistoryChart').getContext('2d');
  if (APP_STATE.chartInstance) {
    APP_STATE.chartInstance.destroy();
  }

  const isDark = document.documentElement.classList.contains('dark');
  const gridColor = isDark ? 'rgba(255, 255, 255, 0.08)' : 'rgba(0, 0, 0, 0.05)';
  const textColor = isDark ? '#94a3b8' : '#64748b';

  const labels = history.length > 0 ? history.map(h => formatBatchDate(h.crawled_time)) : ['當前批次'];
  const prices = history.length > 0 ? history.map(h => Number(h.eff_price)) : [0];

  APP_STATE.chartInstance = new Chart(ctx, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [{
        label: '實質單價 (TWD)',
        data: prices,
        borderColor: '#10b981',
        backgroundColor: 'rgba(16, 185, 129, 0.12)',
        fill: true,
        tension: 0.3,
        pointBackgroundColor: '#10b981',
        pointBorderColor: '#ffffff',
        pointBorderWidth: 2,
        pointRadius: history.length > 1 ? 5 : 6,
        pointHoverRadius: 7
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => ` 實質單價: $${ctx.parsed.y} TWD`
          }
        }
      },
      scales: {
        x: {
          grid: { color: gridColor },
          ticks: { color: textColor, font: { size: 11 } }
        },
        y: {
          grid: { color: gridColor },
          ticks: { 
            color: textColor, 
            font: { size: 11 },
            callback: (v) => `$${v}`
          },
          suggestedMin: Math.max(0, Math.min(...prices) * 0.85),
          suggestedMax: Math.max(...prices) * 1.15
        }
      }
    }
  });

  lucide.createIcons();
}

function hidePriceHistoryModal() {
  const modal = document.getElementById('price-history-modal');
  modal.classList.add('hidden');
  modal.classList.remove('flex');
}

// -----------------------------------------------------------------------------
// 9. 事件監聽與控制器綁定
// -----------------------------------------------------------------------------
function initEventListeners() {
  // Tab 切換
  document.querySelectorAll('.nav-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.nav-tab').forEach(t => {
        t.classList.remove('active', 'bg-red-500', 'text-white', 'shadow-md');
        t.classList.add('text-slate-600', 'dark:text-slate-400');
      });
      tab.classList.add('active', 'bg-red-500', 'text-white', 'shadow-md');
      tab.classList.remove('text-slate-600', 'dark:text-slate-400');

      const target = tab.getAttribute('data-target');
      document.querySelectorAll('.tab-content').forEach(c => c.classList.add('hidden'));
      document.getElementById(target).classList.remove('hidden');
      APP_STATE.currentTab = target;

      if (target === 'tab-global-search') {
        fetchGlobalProducts(APP_STATE.globalPage || 1);
      }
    });
  });

  // 頂部四大指標卡片點擊直達 Tab
  document.getElementById('stat-card-discount')?.addEventListener('click', () => switchTab('tab-discounts'));
  document.getElementById('stat-card-stores')?.addEventListener('click', () => switchTab('tab-stores'));
  document.getElementById('stat-card-products')?.addEventListener('click', () => switchTab('tab-products'));
  document.getElementById('stat-card-promos')?.addEventListener('click', () => switchTab('tab-promos'));

  // Tab 1: 大特價篩選事件
  document.getElementById('discount-pct-select')?.addEventListener('change', e => {
    APP_STATE.filters.discountMinPct = parseFloat(e.target.value);
    fetchDiscounts(1);
  });

  document.getElementById('discount-savings-select')?.addEventListener('change', e => {
    APP_STATE.filters.discountMinSavings = parseFloat(e.target.value);
    fetchDiscounts(1);
  });

  document.getElementById('discount-sort-select')?.addEventListener('change', e => {
    APP_STATE.filters.discountSort = e.target.value;
    fetchDiscounts(1);
  });

  document.getElementById('discount-search-input')?.addEventListener('input', debounce(e => {
    APP_STATE.filters.discountSearch = e.target.value;
    fetchDiscounts(1);
  }, 100));

  // 分類標籤點擊
  document.querySelectorAll('.cat-tag').forEach(tag => {
    tag.addEventListener('click', () => {
      document.querySelectorAll('.cat-tag').forEach(t => {
        t.classList.remove('active', 'bg-red-100', 'text-red-700', 'dark:bg-red-950/80', 'dark:text-red-300', 'font-medium');
        t.classList.add('bg-slate-100', 'text-slate-600', 'dark:bg-slate-800', 'dark:text-slate-400');
      });
      tag.classList.add('active', 'bg-red-100', 'text-red-700', 'dark:bg-red-950/80', 'dark:text-red-300', 'font-medium');
      tag.classList.remove('bg-slate-100', 'text-slate-600', 'dark:bg-slate-800', 'dark:text-slate-400');

      APP_STATE.filters.discountCategory = tag.getAttribute('data-cat');
      fetchDiscounts(1);
    });
  });

  // Tab 2: 新店家篩選事件
  document.getElementById('store-search-input')?.addEventListener('input', debounce(e => {
    APP_STATE.filters.storeSearch = e.target.value;
    fetchNewStores(1);
  }, 150));

  document.getElementById('store-city-select')?.addEventListener('change', e => {
    APP_STATE.filters.storeCity = e.target.value;
    fetchNewStores(1);
  });

  document.getElementById('store-sort-select')?.addEventListener('change', e => {
    APP_STATE.filters.storeSort = e.target.value;
    fetchNewStores(1);
  });

  // Tab 4: 促銷活動篩選事件
  document.getElementById('promo-search-input')?.addEventListener('input', debounce(e => {
    APP_STATE.filters.promoSearch = e.target.value;
    fetchPromotions(1);
  }, 150));

  document.getElementById('promo-type-select')?.addEventListener('change', e => {
    APP_STATE.filters.promoType = e.target.value;
    fetchPromotions(1);
  });

  document.getElementById('promo-sort-select')?.addEventListener('change', e => {
    APP_STATE.filters.promoSort = e.target.value;
    fetchPromotions(1);
  });

  // Tab 5: 全庫搜尋事件
  const globalInput = document.getElementById('global-search-input');
  if (globalInput) {
    globalInput.addEventListener('input', debounce(e => {
      APP_STATE.filters.globalSearch = e.target.value;
      fetchGlobalProducts(1);
    }, 150));

    globalInput.addEventListener('keydown', e => {
      if (e.key === 'Enter') {
        APP_STATE.filters.globalSearch = e.target.value;
        fetchGlobalProducts(1);
      }
    });
  }

  document.getElementById('global-sort-select')?.addEventListener('change', e => {
    APP_STATE.filters.globalSort = e.target.value;
    fetchGlobalProducts(1);
  });

  document.getElementById('global-city-select')?.addEventListener('change', e => {
    APP_STATE.filters.globalCity = e.target.value;
    fetchGlobalProducts(1);
  });

  // 重新整理按鈕
  document.getElementById('btn-refresh')?.addEventListener('click', async () => {
    const icon = document.getElementById('refresh-icon');
    icon?.classList.add('animate-spin');
    await loadDashboardData();
    showToast('資料已刷新', '已為您同步最新快照情報！', 'external', 2000);
    setTimeout(() => icon?.classList.remove('animate-spin'), 600);
  });

  // 彈窗關閉
  document.getElementById('modal-close-btn')?.addEventListener('click', hidePriceHistoryModal);
  document.getElementById('price-history-modal')?.addEventListener('click', e => {
    if (e.target.id === 'price-history-modal') hidePriceHistoryModal();
  });
}

function switchTab(targetId) {
  const btn = document.querySelector(`.nav-tab[data-target="${targetId}"]`);
  if (btn) btn.click();
}

// -----------------------------------------------------------------------------
// 10. 主題與輔助工具
// -----------------------------------------------------------------------------
function initTheme() {
  const isDark = localStorage.getItem('uber_radar_theme') === 'dark' || 
    (!localStorage.getItem('uber_radar_theme') && window.matchMedia('(prefers-color-scheme: dark)').matches);
  
  if (isDark) {
    document.documentElement.classList.add('dark');
  } else {
    document.documentElement.classList.remove('dark');
  }

  document.getElementById('theme-toggle')?.addEventListener('click', () => {
    const dark = document.documentElement.classList.toggle('dark');
    localStorage.setItem('uber_radar_theme', dark ? 'dark' : 'light');
    if (APP_STATE.chartInstance) {
      showPriceHistoryModal(
        document.getElementById('modal-product-id').textContent.replace('Product ID: ', ''),
        document.getElementById('modal-product-name').textContent,
        document.getElementById('modal-store-name').textContent,
        document.getElementById('modal-order-btn').href
      );
    }
  });
}

function debounce(func, delay = 100) {
  let timer = null;
  return function(...args) {
    clearTimeout(timer);
    timer = setTimeout(() => func.apply(this, args), delay);
  };
}

function formatBatchDate(batchStr) {
  if (!batchStr || batchStr.length !== 14) return batchStr || '';
  return `${batchStr.slice(0, 4)}/${batchStr.slice(4, 6)}/${batchStr.slice(6, 8)} ${batchStr.slice(8, 10)}:${batchStr.slice(10, 12)}`;
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

// -----------------------------------------------------------------------------
// 11. Uber Eats 智慧下單跳轉與剪貼簿輔助
// -----------------------------------------------------------------------------
async function openUberEatsOrder(event, url, productName = '', storeName = '') {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }

  let targetUrl = safeOrderUrl(url);

  if (!targetUrl || targetUrl === '#' || targetUrl === '') {
    const query = (storeName || productName || '').trim();
    if (query) {
      targetUrl = `https://www.ubereats.com/tw/search?q=${encodeURIComponent(query)}`;
    } else {
      targetUrl = 'https://www.ubereats.com/tw';
    }
  }

  if (productName && productName.trim()) {
    const cleanName = productName.trim();
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(cleanName);
      } else {
        const textArea = document.createElement('textarea');
        textArea.value = cleanName;
        textArea.style.position = 'fixed';
        textArea.style.left = '-999999px';
        document.body.appendChild(textArea);
        textArea.focus();
        textArea.select();
        document.execCommand('copy');
        textArea.remove();
      }
      showToast(
        '已複製商品並開啟店家',
        `已複製「${cleanName}」！已為您開啟店家網頁，可直接在搜尋欄貼上下單。`,
        'copy',
        4000
      );
    } catch (e) {
      showToast(
        '正在前往 Uber Eats 店家',
        `已為您在新分頁開啟店家，請於菜單中選購「${cleanName}」`,
        'external',
        3500
      );
    }
  } else {
    showToast('前往 Uber Eats', '已為您在新分頁開啟 Uber Eats 店家網頁！', 'external', 3000);
  }

  window.open(targetUrl, '_blank', 'noopener,noreferrer');
}

function showToast(title, message, iconType = 'copy', duration = 4000) {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const toast = document.createElement('div');
  toast.className = 'pointer-events-auto bg-slate-900/95 dark:bg-slate-800/95 text-white p-4 rounded-2xl shadow-2xl border border-slate-700/50 backdrop-blur-md flex items-start gap-3 transition-all duration-300 transform translate-y-4 opacity-0';
  
  let iconHtml = '';
  if (iconType === 'copy') {
    iconHtml = `<div class="p-2 bg-emerald-500/20 text-emerald-400 rounded-xl shrink-0"><i data-lucide="clipboard-check" class="w-5 h-5"></i></div>`;
  } else if (iconType === 'external') {
    iconHtml = `<div class="p-2 bg-blue-500/20 text-blue-400 rounded-xl shrink-0"><i data-lucide="external-link" class="w-5 h-5"></i></div>`;
  } else {
    iconHtml = `<div class="p-2 bg-amber-500/20 text-amber-400 rounded-xl shrink-0"><i data-lucide="alert-circle" class="w-5 h-5"></i></div>`;
  }

  toast.innerHTML = `
    ${iconHtml}
    <div class="flex-1 min-w-0">
      <h5 class="text-xs font-bold text-slate-100">${escapeHtml(title)}</h5>
      <p class="text-[11px] text-slate-300 mt-0.5 leading-relaxed">${escapeHtml(message)}</p>
    </div>
    <button onclick="this.parentElement.remove()" class="text-slate-400 hover:text-white transition-colors p-1">
      <i data-lucide="x" class="w-3.5 h-3.5"></i>
    </button>
  `;

  container.appendChild(toast);
  lucide.createIcons({ root: toast });

  requestAnimationFrame(() => {
    toast.classList.remove('translate-y-4', 'opacity-0');
    toast.classList.add('translate-y-0', 'opacity-100');
  });

  setTimeout(() => {
    toast.classList.remove('translate-y-0', 'opacity-100');
    toast.classList.add('translate-y-4', 'opacity-0');
    setTimeout(() => toast.remove(), 300);
  }, duration);
}

function safeOrderUrl(value) {
  try {
    const url = new URL(String(value || '').replace(/&amp;/g, '&'));
    return url.protocol === 'https:' && ['www.ubereats.com', 'ubereats.com'].includes(url.hostname)
      && !url.username && !url.password ? url.href : '#';
  } catch { return '#'; }
}

// Dataset text is data, never executable HTML event-handler code.
document.addEventListener('click', event => {
  const element = event.target.closest('[data-action][data-args]');
  if (!element) return;
  try {
    const args = JSON.parse(element.dataset.args);
    if (element.dataset.action === 'history') showPriceHistoryModal(...args);
    else if (element.dataset.action === 'order') openUberEatsOrder(event, ...args);
  } catch (error) { console.error('Invalid action data', error); }
});
