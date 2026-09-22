# 資料表與儲存位置

## 1 儲存分層

| 層級 | 內容 | 位置 | 保留方式 |
|---|---|---|---|
| Raw | 每批全台每店原始菜單 JSON 與 manifest 的 tar.gz | Hugging Face Dataset `TaiwanMenuSnapshots/<batch_id>/taiwan_menus_<batch_id>.tar.gz` | 自動保留 60 天 |
| 店家發現資料 | 合併後全台店家資料 | Hugging Face `TaiwanStores`；GitHub artifact 90 天 | 依各自政策 |
| Current | 每個店家一筆、每個店家商品一筆的目前狀態 | 建置時 `serving.db`，正式發布至 Turso | 持續 upsert，保留 first_seen |
| Events | 店家及商品新增、異動、下架、重現事件 | `serving.db.events` 與 Turso packed store bundles | 由保留 Raw 重建；目前正式重建不主動裁掉 |
| Packed | Current、Events、batch、metadata 的無損壓縮表示 | Turso `ubereats-packed-v1` 類型資料庫 | 每次 packed publish 建立隔離新庫並切換 |
| 前端備援 | 統計、特價、新店、新品、促銷、部分商品、歷史 | Git repo 的 `web/data/*.json`，由 GitHub Pages 發布 | 隨程式碼版本；目前主 crawler 不重建它們 |
| 暫存與本機 | SQLite、Parquet、壓縮包、worker 輸出 | 工作目錄或 GitHub artifacts/cache | 多數被 `.gitignore` 排除 |

密鑰放在 GitHub Secrets 或本機 `.env`，不應寫入文件或 commit。`web/config.js` 包含瀏覽器可見的唯讀 Turso token；它只應具讀取權限，不能當成秘密或寫入憑證。

## 2 正規化資料庫表

定義位於 `src/serving_state.py`。

### 2.1 metadata

| 欄位 | 型別 | 用途 |
|---|---|---|
| `key` | TEXT PRIMARY KEY | metadata 名稱 |
| `value` | TEXT NOT NULL | 值，以字串保存 |

重要鍵包括 `latest_batch`，以及全歷史重建所寫的快照數、shard 等標記。增量同步用 `latest_batch` 防止重複或倒序處理。

### 2.2 crawl_batches

| 欄位 | 型別 | 用途 |
|---|---|---|
| `batch_id` | TEXT PRIMARY KEY | `YYYYMMDDHHMMSS` 批次 ID |
| `processed_at` | TEXT NOT NULL | 批次轉成台灣時區 ISO 時間 |
| `stores_seen` | INTEGER | 該批讀到的唯一店家數 |
| `products_seen` | INTEGER | 該批讀到的唯一商品數 |
| `fallback_stores` | INTEGER | 使用 fallback 店家 ID 的筆數 |
| `fallback_products` | INTEGER | 使用 fallback 商品 ID 的筆數 |

### 2.3 stores

主鍵：`store_uuid`。

| 欄位 | 用途 |
|---|---|
| `store_uuid` | 正式 UUID 或 `fallback:store-url:*` |
| `name` | 店名 |
| `address` | 街道地址 |
| `city` | `addressRegion`，沒有時使用 `addressLocality` |
| `locality` | 行政區或 locality |
| `latitude`, `longitude` | 座標；新快照缺座標時保留舊值 |
| `rating`, `review_count` | 評分與評論數 |
| `order_url` | 下單 URL，優先 potentialAction，否則 `@id` |
| `first_seen` | 系統歷史第一次見到時間，不因更新或重現改寫 |
| `last_seen` | 最近一次在快照中見到時間 |
| `status` | `active` 或 `inactive` |
| `missing_streak` | 連續未出現批次數 |
| `state_hash` | canonical state 的 SHA-256，用於快速判斷內容是否改變 |
| `is_open` | 1 為營業或未明示關閉，0 為關閉 |

索引：`first_seen`、`city`、`name`、`(latitude, longitude)`、`is_open`。

### 2.4 products

複合主鍵：`(store_uuid, product_uuid)`；`store_uuid` 外鍵連到 `stores`。

| 欄位 | 用途 |
|---|---|
| `store_uuid` | 所屬店家 |
| `product_uuid` | 正式 UUID 或 `fallback:item-name:*` |
| `product_name` | 商品名稱 |
| `category` | 菜單分類 |
| `description` | 商品描述 |
| `price` | Raw 方案總價或標價 |
| `quantity` | 方案份數，至少 1 |
| `promo_type` | 促銷型態，預設 `無` |
| `effective_price` | 單份實質價格 |
| `order_url` | 所屬店家下單 URL |
| `first_seen` | 第一次見到時間 |
| `last_seen` | 最近一次見到時間 |
| `status` | `active` 或 `inactive` |
| `missing_streak` | 連續未出現批次數 |
| `state_hash` | 商品目前狀態的 SHA-256 |
| `recent_prices` | JSON 陣列，最多三筆最近有效 `price` |
| `price_novel_vs_previous_3` | 新價格是否不在前三筆中 |
| `reference_price` | 前三筆價格中位數 |
| `discount_amount` | `reference_price - price`，不符合時為 0 |
| `discount_pct` | 相對 `reference_price` 的降幅 |
| `is_price_deal` | 是否低於基準價 |
| `is_open` | 商品所屬店家當批營業狀態 |

索引：`first_seen`、`(effective_price, price)`、`promo_type`、`product_name`、`category`、`(is_price_deal, discount_pct DESC, discount_amount DESC)`、`is_open`。

### 2.5 events

| 欄位 | 用途 |
|---|---|
| `id` | 自動遞增主鍵 |
| `event_time` | 事件所屬批次的台灣時區 ISO 時間 |
| `store_uuid` | 店家 ID |
| `product_uuid` | 商品事件時有值；店家事件為 NULL |
| `event_type` | 事件種類 |
| `old_state` | 變更前 JSON，新增事件可為 NULL |
| `new_state` | 變更後 JSON，下架事件為狀態 JSON |

事件種類：

| 類型 | 意義 |
|---|---|
| `STORE_NEW` | 全歷史首次見到店家 |
| `STORE_CHANGED` | 店家內容改變 |
| `STORE_REMOVED` | 店家連續缺失達門檻 |
| `STORE_REAPPEARED` | inactive 店家重新出現 |
| `NEW` | 全歷史首次見到商品 |
| `PRICE_CHANGED` | 符合新價格條件的價格變動 |
| `PROMOTION_CHANGED` | 促銷文字或份數改變 |
| `CONTENT_CHANGED` | 商品名稱、分類、描述或 URL 改變 |
| `REMOVED` | 商品連續缺失達門檻 |
| `REAPPEARED` | inactive 商品重新出現 |

索引：`(store_uuid, product_uuid, event_time DESC)`、`event_time`、`(event_type, event_time DESC)`。

### 2.6 product_search

FTS5 虛擬表，欄位為 `store_uuid`、`product_uuid`、`store_name`、`product_name`、`category`，使用 `unicode61` tokenizer。每次完整套用 snapshot 後會先清空，再從 active products 重建。

## 3 Packed Turso 資料表

定義位於 `scripts/prototype_pack_db.py`，資料並未丟失，只改為壓縮表示。

### 3.1 metadata

記錄 packed format、bucket 數、chunk 數、商品數、最大 bundle 大小，以及來源各表的筆數與 SHA-256。

### 3.2 store_directory

提供不用解壓 blob 就能做店家與地理篩選的目錄。

| 欄位 | 用途 |
|---|---|
| `store_id` | packed DB 內部整數 ID |
| `store_uuid` | 原始店家 ID，唯一 |
| `name`, `city`, `locality` | 搜尋與顯示 |
| `rating`, `review_count` | 排序與顯示 |
| `chunk_count` | 該店 bundle 數 |
| `latitude`, `longitude`, `address`, `order_url` | 合併階段補入，支援地理及顯示 |
| `first_seen`, `last_seen` | 新店與時間資訊 |

### 3.3 store_bundles

每店一個或多個 chunk。每個 payload 是 MessagePack 後以 Zstandard 壓縮的 blob，內含：

- 店家完整欄位與一筆店家資料。
- 該店全部商品陣列。
- 該店全部事件陣列。
- 每個 blob 的未壓縮大小與 SHA-256 checksum。

單一 chunk 最多 2048 項商品或事件，避免大型店家產生過大的 blob。

### 3.4 search_buckets

8192 個 bucket 的倒排索引。索引項包括正規化完整字、單字元、bigram、trigram，以及：

- `f:price:<50元級距>`
- `f:promo`
- `f:discount:<5百分點級距>`
- 合併後加入的 `f:new`

中文與其他文字先做 Unicode NFKC、casefold、移除空白。候選結果取回 bundle 後仍會做精確條件檢查，不只依賴粗略 bucket。

### 3.5 auxiliary_bundles

保存完整 `crawl_batches` 與 `metadata`。同樣使用 MessagePack、Zstandard、SHA-256 checksum。

### 3.6 _packed_publish_progress

由 `scripts/publish_packed_turso.mjs` 建立，用於大檔分批發布的續傳進度，記錄每個來源與資料表已完成的 rowid、筆數、位元組及更新時間。整檔 upload 路徑不以它作為業務資料表。

## 4 靜態前端資料檔

`web/data` 目前包含：

| 檔案 | 內容 |
|---|---|
| `stats.json` | 批次時間與各類統計 |
| `discounts.json` | 跨快照實質單價降價清單 |
| `new_stores.json` | 相較前一歷史 Parquet 新出現的店家 |
| `new_products.json` | 老店相較前一歷史 Parquet新出現的商品 |
| `promotions.json` | 促銷商品 |
| `products.json` | 最多 20,000 筆精選離線全品庫 |
| `history.json` | 特價、新品、促銷與精選商品的部分價格歷史 |

這些檔案會被 GitHub Pages 原樣部署，但目前的正式 crawler Stage 6 不執行 `export_static_snapshots.py`，所以不能假設它們與 Turso 的 `latest_batch` 同步。

## 5 舊式資料表

`src/alert_engine.py` 可建立 `alerts_history`，欄位包含 alert type、target、店家、商品、原價、現價、折扣、現省、促銷、URL 與批次；唯一鍵為 `(alert_type, target_id, crawled_time)`。這是舊式快照分析器使用的表，不屬於目前 v10 正規化 `serving_state` schema。

`src/export_static_snapshots.py` 也會建立一次性暫存 SQLite，表名同樣有 `crawl_batches`、`stores`、`products`，但它們是以 `(id, crawled_time)` 為主鍵的匯出工作表，執行完成後若為暫存 DB 會刪除，不能與正式 Current 表混為一談。

