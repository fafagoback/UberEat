# UberEat 全台資料管線

Production 目標架構分成三層：

- **RAW**：每次完整全台爬蟲的 JSON/tar.gz，位於 HF `TaiwanMenuSnapshots/`，只保留 60 天。
- **CURRENT**：Turso 的 normalized `stores` / `products`，每個官方 UUID 只有一筆 current。
- **EVENTS**：只保存 `NEW`、`PRICE_CHANGED`、`PROMOTION_CHANGED`、`CONTENT_CHANGED`、`REMOVED`、`REAPPEARED`，保留 60 天。

網站資料流：GitHub Pages → Cloudflare Worker → Turso。瀏覽器不含 Turso token，也不再直接讀 Raw 或完整 Parquet。

目前 migration 維持可回滾：`web/data` 只作既有只讀 fallback，不再由 crawler workflow 每批 commit；設定好 Worker URL 且驗證 Turso baseline 後再刪除。

詳見 [儲存架構](docs/STORAGE_ARCHITECTURE.md) 與 [部署設定](docs/DEPLOYMENT.md)。
