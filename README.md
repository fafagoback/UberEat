# UberEat 全台資料管線

Production 目標架構分成三層：

- **RAW**：每次完整全台爬蟲的 JSON/tar.gz，位於 HF `TaiwanMenuSnapshots/`，只保留 60 天。
- **CURRENT**：Turso 的 normalized `stores` / `products`，每個官方 UUID 只有一筆 current。
- **EVENTS**：只保存 `NEW`、`PRICE_CHANGED`、`PROMOTION_CHANGED`、`CONTENT_CHANGED`、`REMOVED`、`REAPPEARED`，保留 60 天。

資料庫更新流：GitHub Actions → Turso。瀏覽器不含 Turso token，也不直接讀 Raw 或完整 Parquet。

目前 migration 維持可回滾：`web/data` 只作既有只讀 fallback，不再由 crawler workflow 每批 commit。GitHub Pages 沒有安全保存 Turso token 的能力，因此在未採用任何後端代理的前提下，網站暫不直連 Turso。

詳見 [儲存架構](docs/STORAGE_ARCHITECTURE.md) 與 [部署設定](docs/DEPLOYMENT.md)。
