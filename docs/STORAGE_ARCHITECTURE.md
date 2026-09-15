# Storage architecture v9

## Identity

實際爬蟲 schema 已確認：`src/location_batch_scraper.py` 從 `getStoreV1` 接收探索階段的 Uber `store_uuid`，並把 `catalogItems[].uuid`（其次 `itemUuid` / `id`）寫到 MenuItem `identifier`。

- store primary key：`store_uuid`
- product primary key：`(store_uuid, product_uuid)`，不可假設 product UUID 跨店唯一
- 只有 HTML JSON-LD 未提供 UUID 時才 fallback：`fallback:store-url:<sha256>` 與 `fallback:item-name:<sha256(store_uuid + name)>`。batch report 會列出 fallback 數量，方便逐步消除。

## Current / Events

`src/serving_state.py` 直接讀完整 Raw 目錄或 tar.gz。相同 identity 且 `state_hash` 相同只更新 `last_seen`，不寫 event。首次 baseline 不產生數百萬筆 `NEW`。消失一次只增加 `missing_streak`；預設連續 3 批才 inactive，設定為 `MISSING_STREAK_THRESHOLD`。

新品、新店 API 都查 `first_seen >= now - 7 days`。重新出現不改 `first_seen`，只寫 `REAPPEARED`。Events 超過 60 天於每次更新時清除。

SQLite schema 將店家欄位只放在 `stores`；`products` 以外鍵關聯，並建有 first_seen、price、promotion、event history 與 FTS5 索引。全文索引只含店名、商品名、category，不含 description。

## Retention

`src/hf_retention.py` 僅允許處理 `TaiwanMenuSnapshots/` 與 `v2/history/events/` 中可解析 14 碼 batch timestamp 的檔案。正式刪除前 workflow 必跑 dry-run、namespace validation；刪後重新列檔，並驗證所有非 allowlist 路徑完全未變。

`TaiwanStores` 是 crawler reducer/menu dispatcher 的輸入，目前不能刪。後續可在 reducer 改成覆寫 `TaiwanStores/latest/` 後再清舊批次。

## Legacy dependency status

- `Parquet/taiwan_catalog_latest.parquet`、`Parquet/partitions/`：新主流程不再產生或讀取，可在 Turso baseline 驗證後刪。
- `Parquet/history/`：新版 API 不需要；但正式 Turso cutover 前仍保留 rollback。
- `v2/current` / `v2/search`：舊 HF browser serving；Worker cutover後可刪。
- `web/data/*.json`：workflow 已停止更新；目前仍是 credential/cutover 前的 rollback fallback，因此還不能刪。
