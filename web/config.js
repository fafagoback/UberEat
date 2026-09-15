/**
 * UberEats Radar - 全域前端配置
 * v9.0: GitHub Pages -> Cloudflare Worker -> Turso.
 */
window.UBER_RADAR_CONFIG = {
  API_BASE_URL: './data', // rollback source until Worker cutover is validated
  WORKER_API_BASE_URL: 'https://ubereat-api.YOUR-SUBDOMAIN.workers.dev',
  // Legacy URLs are kept only for rollback diagnostics during migration.
  ENABLE_DUCKDB: false
};

// 非阻塞載入 v2 搜尋覆寫；v2_search.js 會等待 app.js 完成初始化。
(function () {
  var script = document.createElement('script');
  script.src = 'v2_search.js?_t=' + Date.now();
  script.async = true;
  document.head.appendChild(script);
})();
