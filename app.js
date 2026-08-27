/**
 * UberEats Radar - 前端分析儀表板核心互動邏輯
 * 支援:
 * 1. Live Server 模式 (透過 REST API 即時查詢 SQLite)
 * 2. Standalone 離線模式 (透過 dashboard_data.js 直接開啟)
 */

let APP_STATE = {
  isServerMode: false,
  currentTab: 'tab-discounts',
  stats: {},
  discounts: [],
  newStores: [],
  newProducts: [],
  promotions: [],
  globalProducts: [],
  globalPage: 1,
  globalTotalPages: 1,
  chartInstance: null,
  filters: {
    discountMinPct: 30,
    discountMinSavings: 20,
    discountSort: 'discount_desc',
    discountCategory: '全部',
    discountSearch: '',
    globalSearch: '',
    globalSort: 'rating_desc'
  }
};

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
      showToast('發現新版本發佈', '系統正在為您載入最新架構...', 'external', 2500);
      setTimeout(() => {
        window.location.reload();
      }, 1200);
    }
  } catch (e) {
    // 忽略檢查錯誤 (例如離線時)
  }
}

function startVersionWatcher() {
  // 1. 初始化讀取當前版本
  checkVersionUpdate(true);

  // 2. 當使用者在手機/電腦切換回此分頁時立即檢查是否有更新
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      checkVersionUpdate(false);
    }
  });

  // 3. 每 2 分鐘在背景輪詢一次版本更新
  setInterval(() => {
    checkVersionUpdate(false);
  }, 2 * 60 * 1000);
}

// -----------------------------------------------------------------------------
// 初始化啟動 (支援動態載入與 DOMContentLoaded 相容模式)
// -----------------------------------------------------------------------------
async function bootstrap() {
  initTheme();
  initEventListeners();
  await loadDashboardData();
  if (window.lucide) {
    lucide.createIcons();
  }
  startVersionWatcher();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bootstrap);
} else {
  bootstrap();
}

// -----------------------------------------------------------------------------
// 1. API 網址產生與資料載入 (附加動態時間戳記避免快取)
// -----------------------------------------------------------------------------
function getApiUrl(endpoint) {
  const base = (window.UBER_RADAR_CONFIG && window.UBER_RADAR_CONFIG.API_BASE_URL) || '';
  const separator = endpoint.includes('?') ? '&' : '?';
  return `${base}${endpoint}${separator}_t=${Date.now()}`;
}

async function loadDashboardData() {
  let loadedFromServer = false;

  try {
    const res = await fetch(getApiUrl('/api/stats'));
    if (res.ok) {
      APP_STATE.isServerMode = true;
      const statsData = await res.json();
      updateStatsUI(statsData);
      await fetchDiscounts();
      await fetchNewStores();
      await fetchNewProducts();
      await fetchPromotions();
      await fetchGlobalProducts();
      loadedFromServer = true;
    }
  } catch (err) {
    console.warn('無法連線動態 API，切換為離線備援資料:', err);
  }

  if (!loadedFromServer) {
    // 離線備援模式: 僅在無網路或 API 離線時讀取 window.UBER_RADAR_DATA
    APP_STATE.isServerMode = false;
    if (window.UBER_RADAR_DATA) {
      const data = window.UBER_RADAR_DATA;
      updateStatsUI(data.stats || {});
      APP_STATE.discounts = data.big_discounts || [];
      APP_STATE.newStores = data.new_stores || [];
      APP_STATE.newProducts = data.new_products || [];
      APP_STATE.promotions = data.promotions || [];
      renderDiscounts();
      renderNewStores();
      renderNewProducts();
      renderPromotions();
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
  document.getElementById('stat-big-discounts').textContent = stats.big_discounts_count ?? 0;
  document.getElementById('stat-new-stores').textContent = stats.new_stores_count ?? 0;
  document.getElementById('stat-new-products').textContent = stats.new_products_count ?? 0;
  document.getElementById('stat-promotions').textContent = stats.promotions_count ?? 0;

  document.getElementById('stat-max-savings').textContent = `現省最高 $${stats.max_savings_twd || 0}`;
  document.getElementById('stat-total-stores').textContent = `總監控 ${stats.total_monitored_stores || stats.total_stores || 0} 間`;
  document.getElementById('stat-total-products').textContent = `總菜品 ${stats.total_monitored_products || stats.total_products || 0} 項`;

  document.getElementById('badge-discounts').textContent = stats.big_discounts_count ?? 0;
  document.getElementById('badge-stores').textContent = stats.new_stores_count ?? 0;
  document.getElementById('badge-products').textContent = stats.new_products_count ?? 0;
  document.getElementById('badge-promos').textContent = stats.promotions_count ?? 0;
}

// -----------------------------------------------------------------------------
// 3. 大特價資料處理與渲染 (Tab 1)
// -----------------------------------------------------------------------------
async function fetchDiscounts() {
  if (!APP_STATE.isServerMode) {
    renderDiscounts();
    return;
  }
  const { discountMinPct, discountMinSavings, discountSort, discountCategory, discountSearch } = APP_STATE.filters;
  const params = new URLSearchParams({
    min_discount: discountMinPct,
    min_savings: discountMinSavings,
    sort: discountSort,
    category: discountCategory,
    q: discountSearch
  });

  try {
    const res = await fetch(getApiUrl(`/api/discounts?${params.toString()}`));
    if (res.ok) {
      const data = await res.json();
      APP_STATE.discounts = data.items || [];
      renderDiscounts();
    }
  } catch (err) {
    console.error('載入大特價失敗:', err);
  }
}

function renderDiscounts() {
  const container = document.getElementById('discounts-grid');
  const emptyView = document.getElementById('discounts-empty');
  let items = APP_STATE.discounts;

  // 若在離線模式，執行本機篩選
  if (!APP_STATE.isServerMode) {
    const { discountMinPct, discountMinSavings, discountSort, discountCategory, discountSearch } = APP_STATE.filters;
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
      items.sort((a, b) => b.discount_pct - a.discount_pct);
    } else if (discountSort === 'savings_desc') {
      items.sort((a, b) => b.savings_amount - a.savings_amount);
    } else if (discountSort === 'price_asc') {
      items.sort((a, b) => a.current_price - b.current_price);
    } else if (discountSort === 'price_desc') {
      items.sort((a, b) => b.current_price - a.current_price);
    }
  }

  // 同步分頁徽章與頂部大特價統計指標
  const badgeDiscountsEl = document.getElementById('badge-discounts');
  if (badgeDiscountsEl) badgeDiscountsEl.textContent = items.length;

  const statBigDiscountsEl = document.getElementById('stat-big-discounts');
  if (statBigDiscountsEl) statBigDiscountsEl.textContent = items.length;

  const statMaxSavingsEl = document.getElementById('stat-max-savings');
  if (statMaxSavingsEl) {
    const maxSavings = items.length > 0 ? Math.max(...items.map(i => i.savings_amount || 0)) : 0;
    statMaxSavingsEl.textContent = `現省最高 $${Math.round(maxSavings)}`;
  }

  if (items.length === 0) {
    container.innerHTML = '';
    emptyView.classList.remove('hidden');
    return;
  }

  emptyView.classList.add('hidden');
  container.innerHTML = items.map(item => {
    const orderUrl = item.order_action_url || '#';
    const hasPromo = item.promo_type && item.promo_type !== '無';
    const ratingBadge = item.rating_value 
      ? `<span class="inline-flex items-center gap-1 text-xs text-amber-500 font-medium"><i data-lucide="star" class="w-3.5 h-3.5 fill-amber-400"></i>${item.rating_value} (${item.review_count || 0})</span>` 
      : '';

    return `
      <div class="radar-card bg-white dark:bg-slate-900 rounded-2xl p-5 border border-slate-200 dark:border-slate-800 shadow-sm flex flex-col justify-between relative overflow-hidden group">
        
        <!-- 頂部店家與分類 -->
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

          <!-- 商品名稱 -->
          <h4 class="text-base font-bold text-slate-900 dark:text-white line-clamp-2 mt-1 mb-2 group-hover:text-red-600 dark:group-hover:text-red-400 transition-colors" title="${escapeHtml(item.product_name)}">
            ${escapeHtml(item.product_name)}
          </h4>

          <!-- 商品描述 -->
          ${item.description ? `<p class="text-xs text-slate-500 dark:text-slate-400 line-clamp-2 mb-3">${escapeHtml(item.description)}</p>` : ''}
        </div>

        <!-- 價格與折數區域 -->
        <div class="pt-3 border-t border-slate-100 dark:border-slate-800 space-y-3">
          
          <div class="flex items-baseline justify-between">
            <div>
              <div class="text-xs text-slate-400 line-through">原價 $${Math.round(item.original_price)}</div>
              <div class="text-2xl font-black text-red-600 dark:text-red-400 font-mono flex items-baseline gap-1">
                $${Math.round(item.current_price)}
                <span class="text-xs font-normal text-slate-500 dark:text-slate-400">/ 實質單件</span>
              </div>
            </div>

            <!-- 折扣標籤 -->
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

          <!-- 促銷活動標籤 -->
          ${hasPromo ? `
            <div class="inline-flex items-center gap-1 px-2 py-0.5 rounded-lg text-xs font-medium bg-purple-50 text-purple-700 dark:bg-purple-950/80 dark:text-purple-300 border border-purple-200 dark:border-purple-800">
              <i data-lucide="gift" class="w-3 h-3"></i>
              ${escapeHtml(item.promo_type)}
            </div>
          ` : ''}

          <!-- 操作按鈕 (價格歷程 & Uber Eats 下單) -->
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

  lucide.createIcons();
}

// -----------------------------------------------------------------------------
// 4. 新進店家情報 (Tab 2)
// -----------------------------------------------------------------------------
async function fetchNewStores() {
  if (!APP_STATE.isServerMode) return;
  try {
    const res = await fetch(getApiUrl('/api/new-stores'));
    if (res.ok) {
      const data = await res.json();
      APP_STATE.newStores = data.items || [];
      renderNewStores();
    }
  } catch (err) {
    console.error('載入新店家失敗:', err);
  }
}

function renderNewStores() {
  const container = document.getElementById('new-stores-grid');
  const emptyView = document.getElementById('new-stores-empty');
  const items = APP_STATE.newStores;

  const badgeStoresEl = document.getElementById('badge-stores');
  if (badgeStoresEl) badgeStoresEl.textContent = items.length;

  const statNewStoresEl = document.getElementById('stat-new-stores');
  if (statNewStoresEl) statNewStoresEl.textContent = items.length;

  document.getElementById('new-stores-counter').textContent = `${items.length} 間新店`;

  if (items.length === 0) {
    container.innerHTML = '';
    emptyView.classList.remove('hidden');
    return;
  }

  emptyView.classList.add('hidden');
  container.innerHTML = items.map(store => {
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

          <a href="${escapeHtml(safeOrderUrl(store.order_action_url || store.store_url || '#'))}" target="_blank" rel="noopener noreferrer" data-action="order" data-args="${escapeHtml(JSON.stringify([store.order_action_url || store.store_url || '', "", store.store_name]))}" class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold bg-emerald-50 text-emerald-700 hover:bg-emerald-100 dark:bg-emerald-950 dark:text-emerald-300 dark:hover:bg-emerald-900 transition-colors">
            <span>瀏覽店家</span>
            <i data-lucide="arrow-up-right" class="w-3.5 h-3.5"></i>
          </a>
        </div>
      </div>
    `;
  }).join('');

  lucide.createIcons();
}

// -----------------------------------------------------------------------------
// 5. 老店新菜推薦 (Tab 3)
// -----------------------------------------------------------------------------
async function fetchNewProducts() {
  if (!APP_STATE.isServerMode) return;
  try {
    const res = await fetch(getApiUrl('/api/new-products'));
    if (res.ok) {
      const data = await res.json();
      APP_STATE.newProducts = data.items || [];
      renderNewProducts();
    }
  } catch (err) {
    console.error('載入新品失敗:', err);
  }
}

function renderNewProducts() {
  const container = document.getElementById('new-products-grid');
  const emptyView = document.getElementById('new-products-empty');
  const items = APP_STATE.newProducts;

  const badgeProductsEl = document.getElementById('badge-products');
  if (badgeProductsEl) badgeProductsEl.textContent = items.length;

  const statNewProductsEl = document.getElementById('stat-new-products');
  if (statNewProductsEl) statNewProductsEl.textContent = items.length;

  document.getElementById('new-products-counter').textContent = `${items.length} 道新品`;

  if (items.length === 0) {
    container.innerHTML = '';
    emptyView.classList.remove('hidden');
    return;
  }

  emptyView.classList.add('hidden');
  container.innerHTML = items.map(prod => {
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

  // 1. 匹配「買X送Y」中文或數字 (例: 買1送1, 買2送1, 買3送1, 買一送一, 買二送一)
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
      discountText: "75折",
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
// 6. 折扣活動專區 (Tab 4)
// -----------------------------------------------------------------------------
async function fetchPromotions() {
  if (!APP_STATE.isServerMode) return;
  try {
    const res = await fetch(getApiUrl('/api/promotions'));
    if (res.ok) {
      const data = await res.json();
      APP_STATE.promotions = data.items || [];
      renderPromotions();
    }
  } catch (err) {
    console.error('載入促銷失敗:', err);
  }
}

function renderPromotions() {
  const container = document.getElementById('promos-grid');
  // 嚴格過濾非商品廣告與 price <= 0 之項目
  const items = APP_STATE.promotions.filter(p => p.price > 0 && (p.quantity > 1 || (p.promo_type && p.promo_type !== '無' && p.promo_type !== '')));

  const badgePromosEl = document.getElementById('badge-promos');
  if (badgePromosEl) badgePromosEl.textContent = items.length;

  const statPromosEl = document.getElementById('stat-promotions');
  if (statPromosEl) statPromosEl.textContent = items.length;

  const counterEl = document.getElementById('promos-counter');
  if (counterEl) counterEl.textContent = `${items.length} 筆特惠`;

  if (items.length === 0) {
    container.innerHTML = '<div class="col-span-3 py-12 text-center text-slate-400">目前沒有促銷活動</div>';
    return;
  }

  container.innerHTML = items.map(p => {
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

  lucide.createIcons();
}

// -----------------------------------------------------------------------------
// 7. 全庫商品搜尋 (Tab 5)
// -----------------------------------------------------------------------------
let globalSearchController;
let globalSearchSequence = 0;
async function fetchGlobalProducts(page = 1) {
  globalSearchController?.abort();
  const controller = new AbortController();
  globalSearchController = controller;
  const sequence = ++globalSearchSequence;
  if (!APP_STATE.isServerMode) {
    document.getElementById('global-total-count').textContent = '僅在伺服器模式支援全品檢索';
    return;
  }
  APP_STATE.globalPage = page;
  const container = document.getElementById('global-products-grid');
  const countEl = document.getElementById('global-total-count');
  if (container) {
    container.style.opacity = '0.4';
    container.style.transition = 'opacity 0.2s ease';
  }
  if (countEl) {
    countEl.textContent = '搜尋中...';
  }

  const sortMode = APP_STATE.filters.globalSort || 'rating_desc';
  const rawSearch = APP_STATE.filters.globalSearch || '';

  const params = new URLSearchParams({
    q: rawSearch,
    sort: sortMode,
    page: page,
    limit: 24
  });

  try {
    const res = await fetch(getApiUrl(`/api/products?${params.toString()}`), { signal: controller.signal });
    if (res.ok) {
      const data = await res.json();
      let items = data.items || [];

      // 前端健全性二次過濾與排序保證 (防後端快取或 API 延遲)
      if (sortMode === 'promo_only') {
        items = items.filter(p => {
          const info = calculateEffectivePromo(p.price, p.promo_type, p.quantity);
          return (p.quantity > 1 || (p.promo_type && p.promo_type !== '無' && p.promo_type !== '') || info.isPromo);
        });
      }

      // 前端二次排序校準
      items.sort((a, b) => {
        const infoA = calculateEffectivePromo(a.price, a.promo_type, a.quantity);
        const infoB = calculateEffectivePromo(b.price, b.promo_type, b.quantity);
        const effA = infoA.effPrice;
        const effB = infoB.effPrice;

        if (sortMode === 'promo_first') {
          const promoA = (a.quantity > 1 || (a.promo_type && a.promo_type !== '無' && a.promo_type !== '')) ? 1 : 0;
          const promoB = (b.quantity > 1 || (b.promo_type && b.promo_type !== '無' && b.promo_type !== '')) ? 1 : 0;
          if (promoB !== promoA) return promoB - promoA;
          return (b.rating_value || 0) - (a.rating_value || 0) || effA - effB;
        } else if (sortMode === 'price_asc') {
          return effA - effB || (b.rating_value || 0) - (a.rating_value || 0);
        } else if (sortMode === 'price_desc') {
          return effB - effA || (b.rating_value || 0) - (a.rating_value || 0);
        } else if (sortMode === 'name_asc') {
          return (a.product_name || '').localeCompare(b.product_name || '', 'zh-TW');
        } else if (sortMode === 'rating_desc') {
          return (b.rating_value || 0) - (a.rating_value || 0) || effA - effB;
        }
        return 0;
      });

      if (sequence !== globalSearchSequence) return;
      APP_STATE.globalProducts = items;
      APP_STATE.globalTotalPages = data.total_pages || 1;
      if (countEl) {
        countEl.textContent = `${(sortMode === 'promo_only' ? items.length : (data.total !== undefined ? data.total : items.length))} 筆`;
      }
      renderGlobalProducts();
      renderGlobalPagination();
    }
  } catch (err) {
    if (err.name !== 'AbortError') console.error('全庫搜尋失敗:', err);
  } finally {
    if (container && sequence === globalSearchSequence) {
      container.style.opacity = '1';
    }
  }
}

function renderGlobalProducts() {
  const container = document.getElementById('global-products-grid');
  const items = APP_STATE.globalProducts;

  if (container) {
    container.style.opacity = '1';
  }

  if (items.length === 0) {
    container.innerHTML = '<div class="col-span-3 py-16 text-center text-slate-400">查無相符商品</div>';
    return;
  }

  container.innerHTML = items.map(p => {
    const promoInfo = calculateEffectivePromo(p.price, p.promo_type, p.quantity);
    const hasPromo = promoInfo.isPromo;
    const hasQtyPromo = promoInfo.totalQty > 1;
    const unitPrice = promoInfo.effPrice;
    const displayUnitPrice = Math.round(unitPrice);
    const discountLabel = promoInfo.discountText ? `折合 ${promoInfo.discountText}` : '';

    return `
      <div class="radar-card bg-white dark:bg-slate-900 rounded-2xl p-4 border border-slate-200 dark:border-slate-800 shadow-sm flex flex-col justify-between group">
        <div>
          <!-- 頂部：店家名稱 & 實質單價 -->
          <div class="flex items-center justify-between text-xs text-slate-500 dark:text-slate-400 mb-1.5 gap-2">
            <div class="flex items-center gap-1.5 truncate min-w-0" title="${escapeHtml(p.store_name)}">
              <i data-lucide="store" class="w-3.5 h-3.5 shrink-0 text-slate-400"></i>
              <span class="truncate font-medium">${escapeHtml(p.store_name)}</span>
            </div>
            <div class="text-right shrink-0 flex items-baseline gap-0.5">
              <span class="font-bold text-slate-900 dark:text-white font-mono text-base ${hasQtyPromo ? 'text-purple-600 dark:text-purple-400' : ''}">
                $${displayUnitPrice}
              </span>
              ${hasQtyPromo ? `<span class="text-[11px] font-semibold text-purple-600 dark:text-purple-400">/件</span>` : ''}
            </div>
          </div>

          <!-- 商品名稱 -->
          <h4 class="text-sm font-bold text-slate-900 dark:text-white line-clamp-2 group-hover:text-emerald-600 dark:group-hover:text-emerald-400 transition-colors mb-2" title="${escapeHtml(p.product_name)}">
            ${escapeHtml(p.product_name)}
          </h4>

          <!-- 活動與分類標籤 -->
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
            ${hasQtyPromo ? `
              <span class="text-[11px] text-slate-400 dark:text-slate-500 font-mono">
                (標價 $${Math.round(p.price)} 共 ${promoInfo.totalQty} 件)
              </span>
            ` : ''}
          </div>
        </div>

        <!-- 底部按鈕：走勢 & 直達 Uber Eats 下單 -->
        <div class="pt-3 mt-3 border-t border-slate-100 dark:border-slate-800 flex items-center justify-between">
          <button data-action="history" data-args="${escapeHtml(JSON.stringify([p.product_id, p.product_name, p.store_name, p.order_action_url || '']))}" class="text-xs text-slate-500 hover:text-emerald-600 transition-colors flex items-center gap-1">
            <i data-lucide="line-chart" class="w-3.5 h-3.5"></i>走勢
          </button>
          <a href="${escapeHtml(safeOrderUrl(p.order_action_url || '#'))}" target="_blank" rel="noopener noreferrer" data-action="order" data-args="${escapeHtml(JSON.stringify([p.order_action_url || '', p.product_name, p.store_name]))}" class="text-xs text-emerald-600 hover:text-emerald-700 dark:text-emerald-400 font-bold flex items-center gap-1 active:scale-95 transition-transform">
            下單 <i data-lucide="arrow-up-right" class="w-3.5 h-3.5"></i>
          </a>
        </div>
      </div>
    `;
  }).join('');

  lucide.createIcons();
}

function renderGlobalPagination() {
  const container = document.getElementById('global-pagination');
  const current = APP_STATE.globalPage;
  const total = APP_STATE.globalTotalPages;

  if (total <= 1) {
    container.innerHTML = '';
    return;
  }

  let html = '';
  if (current > 1) {
    html += `<button onclick="fetchGlobalProducts(${current - 1})" class="px-3 py-1.5 rounded-lg border border-slate-200 dark:border-slate-700 text-xs text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800">上一頁</button>`;
  }
  html += `<span class="text-xs text-slate-500 font-mono px-2">第 ${current} / ${total} 頁</span>`;
  if (current < total) {
    html += `<button onclick="fetchGlobalProducts(${current + 1})" class="px-3 py-1.5 rounded-lg border border-slate-200 dark:border-slate-700 text-xs text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800">下一頁</button>`;
  }

  container.innerHTML = html;
}

// -----------------------------------------------------------------------------
// 8. 價格走勢圖彈窗 (Price Trend Modal with Chart.js)
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

  // 取得歷史資料
  let history = [];
  if (APP_STATE.isServerMode) {
    try {
      const res = await fetch(getApiUrl(`/api/history?product_id=${productId}`));
      if (res.ok) {
        const data = await res.json();
        history = data.history || [];
      }
    } catch (e) {
      console.error(e);
    }
  }

  // 繪製表格
  const tbody = document.getElementById('modal-history-tbody');
  if (history.length > 0) {
    tbody.innerHTML = history.map(h => `
      <tr class="hover:bg-slate-50 dark:hover:bg-slate-800/50">
        <td class="px-3 py-2 font-mono text-slate-600 dark:text-slate-300">${formatBatchDate(h.crawled_time)}</td>
        <td class="px-3 py-2 font-mono font-medium">$${h.price}</td>
        <td class="px-3 py-2"><span class="px-2 py-0.5 rounded text-[11px] bg-slate-100 dark:bg-slate-800">${escapeHtml(h.promo_type || '無')}</span></td>
        <td class="px-3 py-2 text-right font-mono font-bold text-emerald-600 dark:text-emerald-400">$${h.eff_price}</td>
      </tr>
    `).join('');
  } else {
    tbody.innerHTML = `<tr><td colspan="4" class="px-3 py-4 text-center text-slate-400">目前批次為最新快照記錄</td></tr>`;
  }

  // 繪製 Chart.js
  const ctx = document.getElementById('priceHistoryChart').getContext('2d');
  if (APP_STATE.chartInstance) {
    APP_STATE.chartInstance.destroy();
  }

  const isDark = document.documentElement.classList.contains('dark');
  const gridColor = isDark ? 'rgba(255, 255, 255, 0.08)' : 'rgba(0, 0, 0, 0.05)';
  const textColor = isDark ? '#94a3b8' : '#64748b';

  const labels = history.length > 0 ? history.map(h => formatBatchDate(h.crawled_time)) : ['當前批次'];
  const prices = history.length > 0 ? history.map(h => h.eff_price) : [100];

  APP_STATE.chartInstance = new Chart(ctx, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [{
        label: '實質單價 (TWD)',
        data: prices,
        borderColor: '#10b981',
        backgroundColor: 'rgba(16, 185, 129, 0.1)',
        fill: true,
        tension: 0.3,
        pointBackgroundColor: '#10b981',
        pointRadius: 5,
        pointHoverRadius: 7
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false }
      },
      scales: {
        x: {
          grid: { color: gridColor },
          ticks: { color: textColor, font: { size: 11 } }
        },
        y: {
          grid: { color: gridColor },
          ticks: { color: textColor, font: { size: 11 } }
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
    });
  });

  // 頂部四大指標卡片點擊直達 Tab
  document.getElementById('stat-card-discount')?.addEventListener('click', () => switchTab('tab-discounts'));
  document.getElementById('stat-card-stores')?.addEventListener('click', () => switchTab('tab-stores'));
  document.getElementById('stat-card-products')?.addEventListener('click', () => switchTab('tab-products'));
  document.getElementById('stat-card-promos')?.addEventListener('click', () => switchTab('tab-promos'));

  // 大特價篩選事件
  document.getElementById('discount-pct-select').addEventListener('change', e => {
    APP_STATE.filters.discountMinPct = parseFloat(e.target.value);
    fetchDiscounts();
  });

  document.getElementById('discount-savings-select').addEventListener('change', e => {
    APP_STATE.filters.discountMinSavings = parseFloat(e.target.value);
    fetchDiscounts();
  });

  document.getElementById('discount-sort-select').addEventListener('change', e => {
    APP_STATE.filters.discountSort = e.target.value;
    fetchDiscounts();
  });

  document.getElementById('discount-search-input').addEventListener('input', debounce(e => {
    APP_STATE.filters.discountSearch = e.target.value;
    fetchDiscounts();
  }, 250));

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
      fetchDiscounts();
    });
  });

  // 全庫搜尋事件
  document.getElementById('global-search-input')?.addEventListener('input', debounce(e => {
    APP_STATE.filters.globalSearch = e.target.value;
    fetchGlobalProducts(1);
  }, 300));

  document.getElementById('global-sort-select')?.addEventListener('change', e => {
    APP_STATE.filters.globalSort = e.target.value;
    fetchGlobalProducts(1);
  });

  // 重新整理按鈕
  document.getElementById('btn-refresh')?.addEventListener('click', async () => {
    const icon = document.getElementById('refresh-icon');
    icon?.classList.add('animate-spin');
    if (APP_STATE.isServerMode) {
      try {
        await fetch(getApiUrl('/api/refresh'), { method: 'POST' });
        await loadDashboardData();
      } catch (e) {
        console.error(e);
      }
    } else {
      location.reload();
    }
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
      // 重新渲染圖表適配深淺色
      showPriceHistoryModal(
        document.getElementById('modal-product-id').textContent.replace('Product ID: ', ''),
        document.getElementById('modal-product-name').textContent,
        document.getElementById('modal-store-name').textContent,
        document.getElementById('modal-order-btn').href
      );
    }
  });
}

function debounce(func, delay = 250) {
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

function escapeJs(str) {
  if (!str) return '';
  return String(str).replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/"/g, '\\"');
}

// -----------------------------------------------------------------------------
// 11. Uber Eats 智慧下單跳轉與剪貼簿輔助
// -----------------------------------------------------------------------------
async function openUberEatsOrder(event, url, productName = '', storeName = '') {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }

  // 清理 URL 實體編碼
  let targetUrl = safeOrderUrl(url);

  // 若目標 URL 為空或無效，自動 fallback 透過 Uber Eats 搜尋店家或商品
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
        // Fallback for non-https/local
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

  // 在新分頁開啟 Uber Eats 店家頁面
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

  // 動畫淡入
  requestAnimationFrame(() => {
    toast.classList.remove('translate-y-4', 'opacity-0');
    toast.classList.add('translate-y-0', 'opacity-100');
  });

  // 自動計時消失
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
