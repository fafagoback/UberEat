/**
 * UberEats Radar - 全域前端配置
 * v8.0: HF v2 去重 current + 分片全文索引。
 * 舊 DuckDB/Parquet 路徑不再作為全庫關鍵字搜尋主路徑。
 */
window.UBER_RADAR_CONFIG = {
  API_BASE_URL: './data',
  V2_BASE_URL: 'https://huggingface.co/datasets/hub-google/UberEat/resolve/main/v2',
  // Legacy URLs are kept only for rollback diagnostics during migration.
  PARQUET_CATALOG_URL: 'https://huggingface.co/datasets/hub-google/UberEat/resolve/main/Parquet/taiwan_catalog_latest.parquet',
  PARQUET_PARTITIONS_BASE_URL: 'https://huggingface.co/datasets/hub-google/UberEat/resolve/main/Parquet/partitions',
  ENABLE_DUCKDB: false
};

// 非阻塞載入 v2 搜尋覆寫；v2_search.js 會等待 app.js 完成初始化。
(function () {
  var script = document.createElement('script');
  script.src = 'v2_search.js?_t=' + Date.now();
  script.async = true;
  document.head.appendChild(script);
})();
