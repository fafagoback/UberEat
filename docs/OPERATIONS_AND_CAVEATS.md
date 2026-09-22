# 執行維運與已知注意事項

## 1 排程與發布

| 工作流 | 觸發 | 作用 |
|---|---|---|
| `taiwan_store_crawler.yml` | 每日台灣時間 06:30、18:30，或手動 | 全台採集、Raw 上傳、增量 Current/Events、Turso v10 發布、Pages 部署 |
| `rebuild_turso.yml` | 手動 | 由全部保留 Raw 重建正規化 DB，可用 15 shard，驗證後發布隔離 Turso |
| `publish_packed_turso.yml` | 手動 | 全歷史重建、無損壓縮、發布新的 packed Turso，更新 web config |
| `hf_retention.yml` | crawler 成功後或手動 | 先 dry run，再刪除過期 Raw 與舊 HF event 檔 |
| `deploy_pages.yml` | `main` 的 `web/**` 變更或手動 | 將 `web` 部署至 GitHub Pages |
| `hf_inventory.yml` | 手動 | 列出 HF Dataset 儲存清單，不修改資料 |

## 2 保留政策

- `src/hf_retention.py` 只允許刪除 `TaiwanMenuSnapshots/` 與 `v2/history/events/` 下超過 60 天的項目。
- workflow 會先 dry run，並以正規表示式確認沒有其他 namespace 被列入刪除。
- `TaiwanStores`、Parquet 或其他 HF 路徑不在這支 retention 程式的刪除範圍。
- GitHub menu worker artifact 保留 14 天，封裝後 snapshot artifact 保留 30 天，全台店家 artifact 保留 90 天；packed shard artifact 保留 2 天。
- 正規化重建能得到的事件歷史受 Raw 保留範圍限制。若只保留 60 天 Raw，從零重建就無法恢復更早、且沒有其他保存副本的事件。

## 3 Baseline 與 first_seen 的意義

完整重建的第一份快照是 baseline：

- 資料仍會寫入 `stores` 與 `products`。
- `first_seen` 是最早可用 Raw 的時間。
- 不產生 `STORE_NEW` 與 `NEW` 事件，避免把基準快照全部誤報為新資料。

所以 `first_seen` 的語意是「在可重建資料範圍首次觀察」，不是 Uber 平台的絕對上架或開店日期。Raw 被 retention 移除後重新重建，最早 first_seen 也可能往後移。

## 4 前端資料新鮮度注意事項

目前有一個重要的混合來源：

- Turso packed DB 由手動 packed workflow 建置並切換。
- 正規化 Turso v10 可由每日 crawler 更新，但前端設定目前指向 packed DB。
- `web/data/*.json` 是 repo 內靜態備援；正式 crawler workflow 的 Stage 6 沒有呼叫靜態匯出器，也不 commit 這些檔案。
- 前端在 Turso 成功時，仍會用靜態 JSON 填入特價、新店、新品與促銷頁籤。

因此，畫面可能同時呈現 packed Turso 的全品搜尋與較舊的靜態情報清單。判讀問題時應先比對：

1. `web/version.json` 與 `web/data/stats.json` 的批次。
2. Packed auxiliary metadata 中的 `latest_batch`。
3. `web/config.js` 指向的 Turso database。

## 5 兩套特價口徑不可混用

| 項目 | Current products | 靜態 discounts JSON |
|---|---|---|
| 比較欄位 | `price` | `effective_price` |
| 參考值 | 最近三筆 price 中位數 | 最近最多七份歷史中的最近一筆有效快照 |
| 必須新價格 | 是，新價不可存在於前三筆 | 不要求 |
| 最低門檻 | 只要低於基準價 | 產生時至少 20% 且 20 元 |
| UI 預設 | 視使用搜尋條件 | 至少 30% 且 20 元 |
| 極端值保護 | 無 80% 規則 | 無促銷且降幅 > 80% 排除 |

若要對外定義唯一的「特價」，應先決定採哪套口徑，再統一資料產生器與 UI。

## 6 新店與新品口徑不可混用

| 項目 | Current/Events | 靜態 JSON |
|---|---|---|
| 新店 | 全歷史中首次出現的店家 ID | 最新快照有、前一歷史 Parquet 沒有 |
| 新品 | 全歷史中首次出現的 `(店家, 商品)` | 老店中最新快照有、前一歷史 Parquet 沒有 |
| 重現 | `REAPPEARED`，不是新資料 | 若前一批缺少，可能再次被列新 |
| baseline | 不產生新增事件 | 沒前批時全部店家可被列新 |

## 7 缺失與誤判保護

- 連續三批缺失才下架，降低單次擷取失敗的誤判。
- 店家明確關閉時不累加其商品缺失，避免空菜單造成大量下架。
- 店家座標若新快照缺失，保留既有座標。
- 使用 UUID；fallback 次數另行統計，避免無聲地把名稱當正式 ID。
- 批次只能依時間向前套用。
- Raw 上傳前要求 worker 完整、店家集合一致、失效率低於 10%、封裝回讀正確。
- Packed DB 每個 blob 有 checksum，發布前做逐表筆數與 SHA-256 無損驗證。

## 8 安全與設定

- 寫入 Turso 的 token、Turso platform token、HF token 都由 GitHub Secrets 或本機 `.env` 提供。
- `.env` 已列入 `.gitignore`；文件不記錄任何實際秘密值。
- `web/config.js` 的唯讀 token 會交付瀏覽器，因此安全性完全依賴其權限只能讀取。它不應擁有 schema、insert、update、delete 或管理權限。
- GitHub Pages 是純靜態站，沒有後端可秘密代理資料庫憑證。
- 前端只允許 Uber Eats HTTPS 網址，且 hostname 必須是 `www.ubereats.com` 或 `ubereats.com`，避免資料中的 URL 造成任意跳轉。
- 前端把資料視為文字並做 HTML escape，事件參數以 JSON data attribute 處理，不把資料直接當可執行 handler。

## 9 建議的資料稽核查詢

以下查詢適用於未壓縮的 `serving.db`：

```sql
-- 最新批次
SELECT value FROM metadata WHERE key = 'latest_batch';

-- 最近批次的量與 fallback 比率
SELECT * FROM crawl_batches ORDER BY batch_id DESC LIMIT 10;

-- 最近首次出現的店家
SELECT store_uuid, name, first_seen, status
FROM stores
ORDER BY first_seen DESC
LIMIT 100;

-- 最近首次出現的商品
SELECT store_uuid, product_uuid, product_name, first_seen, status
FROM products
ORDER BY first_seen DESC
LIMIT 100;

-- Current 規則判定的價格優惠
SELECT store_uuid, product_uuid, product_name, price,
       reference_price, discount_amount, discount_pct
FROM products
WHERE status = 'active' AND is_price_deal = 1
ORDER BY discount_pct DESC, discount_amount DESC;

-- 最近事件
SELECT event_time, event_type, store_uuid, product_uuid
FROM events
ORDER BY event_time DESC, id DESC
LIMIT 200;
```

Packed Turso 不直接提供正規化 `products` 表；應透過 `store_directory`、`search_buckets` 找候選店家與商品，再取 `store_bundles` 解壓。前端實作可參考 `web/packed-turso.js`。

## 10 修改規則時要同步檢查的檔案

若調整特價、新店、新品或資料欄位，至少同步檢查：

- `src/serving_state.py`
- `src/rebuild_database.py`
- `src/sync_database_from_hf.py`
- `scripts/merge_rebuild_shards.py`
- `scripts/prototype_pack_db.py`
- `scripts/merge_packed_shards.py`
- `web/packed-turso.js`
- `web/app.js`
- `src/export_static_snapshots.py`
- 相關 `tests/*.py` 與 `tests/frontend.test.cjs`

規則修改後應同時驗證正規化 DB、packed round trip、前端查詢，以及靜態備援的欄位相容性。

