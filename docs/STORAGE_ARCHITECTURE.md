# Storage architecture v10

## Identity

實際爬蟲 schema 已確認：`src/location_batch_scraper.py` 從 `getStoreV1` 接收探索階段的 Uber `store_uuid`，並把 `catalogItems[].uuid`（其次 `itemUuid` / `id`）寫到 MenuItem `identifier`。

- store primary key：`store_uuid`
- product primary key：`(store_uuid, product_uuid)`，不可假設 product UUID 跨店唯一
- 只有 HTML JSON-LD 未提供 UUID 時才 fallback：`fallback:store-url:<sha256>` 與 `fallback:item-name:<sha256(store_uuid + name)>`。batch report 會列出 fallback 數量，方便逐步消除。

## Current / Events

`src/rebuild_database.py` 依時間由舊到新重播 HF 所有可用且通過 manifest、JSON 數量及最低店數檢核的完整 Raw tar.gz。異常小批次記錄在 `rebuild_skipped_snapshots`，不可參與 missing/event 判斷。`stores` 與 `products` 是所有有效快照 identity 的聯集，不是最新快照的覆蓋檔；第一批只建立 baseline，後續每批才產生事件。因此 `first_seen` / `last_seen`、inactive 與重新出現都來自完整可用歷史。

`src/serving_state.py` 同時是重建與每天兩批增量更新共用的唯一規則來源。相同 identity 且 `state_hash` 相同只更新 `last_seen`，不寫 event。消失一次只增加 `missing_streak`；預設連續 3 批才 inactive。

價格型特價必須已經有完整三次先前價格，且本次價格未曾在該三次出現，並低於三次價格的中位數。新品即使帶有買一送一 `promo_type`，在歷史不足三次時 `is_price_deal=0`，不宣稱價格型特價。`recent_prices` 保存滾動三次觀察值，`reference_price` / `discount_amount` / `discount_pct` / `is_price_deal` 保存當批可發布判斷。

新品、新店 API 都查 `first_seen >= now - 7 days`。重新出現不改 `first_seen`，只寫 `REAPPEARED`。Raw 依 HF retention 保存；已由完整 Raw 推導出的 Turso events 不再自動刪除，避免失去可用的完整分析歷史。

SQLite schema 將店家欄位只放在 `stores`；`products` 以外鍵關聯，並建有 first_seen、price、promotion、event history 與 FTS5 索引。全文索引只含店名、商品名、category，不含 description。

## Retention

`src/hf_retention.py` 僅允許處理 `TaiwanMenuSnapshots/` 與 `v2/history/events/` 中可解析 14 碼 batch timestamp 的檔案。正式刪除前 workflow 必跑 dry-run、namespace validation；刪後重新列檔，並驗證所有非 allowlist 路徑完全未變。

`TaiwanStores` 是 crawler reducer/menu dispatcher 的輸入，目前不能刪。後續可在 reducer 改成覆寫 `TaiwanStores/latest/` 後再清舊批次。

## Turso publish

`rebuild_turso.yml` 與 crawler Stage 6 都只在 GitHub-hosted Ubuntu runner 運算。重建 workflow 先產生隔離的新資料庫並驗證，再按明確的 `publish` input 寫入 `TURSO_REBUILD_DATABASE_URL`。它同時建立 `serving-db-rebuilt-*` cache；每天的增量流程拒絕任何沒有 `rebuild_snapshot_count` marker 的舊 baseline。使用者電腦不參與 production Turso 寫入。

## Legacy dependency status

- `Parquet/taiwan_catalog_latest.parquet`、`Parquet/partitions/`：新主流程不再產生或讀取，可在 Turso baseline 驗證後刪。
- `Parquet/history/`：新版 API 不需要；但正式 Turso cutover 前仍保留 rollback。
- `v2/current` / `v2/search`：舊 HF browser serving；Worker cutover後可刪。
- `web/data/*.json`：workflow 已停止更新；目前仍是 credential/cutover 前的 rollback fallback，因此還不能刪。
