/**
 * UberEats Radar - 全域前端配置
 * v7.0 現代大數據湖倉 (Hugging Face Datasets + DuckDB-WASM 邊緣 SQL 引擎)
 */
window.UBER_RADAR_CONFIG = {
  API_BASE_URL: './data',
  // Hugging Face Parquet Data Lake (全台百萬商品列式大數據庫)
  PARQUET_CATALOG_URL: 'https://huggingface.co/datasets/hub-google/UberEat/resolve/main/Parquet/taiwan_catalog_latest.parquet',
  PARQUET_PARTITIONS_BASE_URL: 'https://huggingface.co/datasets/hub-google/UberEat/resolve/main/Parquet/partitions',
  ENABLE_DUCKDB: true
};

