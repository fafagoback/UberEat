/**
 * UberEats Radar - 全域前端配置
 * 支援動態指定 Cloudflare Worker API 網址，達成 100% 即時向 D1 抓取資料。
 */
window.UBER_RADAR_CONFIG = {
  // 自動判斷本地或線上 API 網址
  API_BASE_URL: (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1')
    ? '' // 本機開發環境：使用本地 Python Server 相對路徑 (/api/...)
    : 'https://ubereats-monitor-api.fafagoback.workers.dev' // 線上環境：直連 Cloudflare Worker API
};
