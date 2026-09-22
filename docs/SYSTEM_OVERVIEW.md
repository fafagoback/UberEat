# 系統架構與資料流程

## 1 系統目的

系統定期掃描台灣 Uber Eats 店家與菜單，保存完整原始快照，重建每間店和每項商品的目前狀態，記錄新增、異動、下架與重新出現事件，並提供網站搜尋、附近店家、特價、新店與新品等功能。

整體分成四層：

1. 採集層：找出店家並擷取完整菜單。
2. Raw 層：將每批完整 JSON 封裝後存到 Hugging Face。
3. Current 與 Events 層：按時間重播 Raw，產生正規化 SQLite，再發布至 Turso。
4. 展示層：GitHub Pages 的靜態網站優先讀取 Turso 壓縮資料庫，部分情報頁籤仍讀取 `web/data/*.json`。

## 2 定期採集流程

正式工作流是 `.github/workflows/taiwan_store_crawler.yml`，排程每天台灣時間 06:30 與 18:30 執行。使用 `uberaets-production` concurrency group 且不取消正在執行的批次，避免兩次全台資料互相覆蓋。

### 2.1 點位調度

- 預設掃描檔：`data/seeds/taiwan_scan_points_3km_land_only.csv`。
- 由 `src/taiwan_crawler_coordinator.py` 分割掃描點。
- 預設最多 15 個 worker。
- 批次 ID 使用台灣時間 `YYYYMMDDHHMMSS`，後續所有檔案及比較均以此作為時間順序。

### 2.2 店家探索

- 15 個 GitHub Actions matrix worker 平行執行 `src/taiwan_store_worker.py`。
- 每個 worker 讀取自己的點位分片，查找店家並輸出結果 artifact。
- worker 內部預設 concurrency 為 2。

### 2.3 全台店家合併與去重

- `src/taiwan_store_reducer.py` 合併所有店家探索結果。
- 產生全台店家資料集及菜單擷取任務。
- 全台店家資料可上傳至 Hugging Face 的 `TaiwanStores` 路徑。
- 同時將菜單工作再次分割成最多 15 份。

### 2.4 菜單擷取

- 15 個菜單 worker 平行執行 `src/taiwan_menu_worker.py`。
- 每個 worker 內部 concurrency 預設為 5。
- 每間店輸出一份 Schema.org Restaurant 風格 JSON，包含店家資料、菜單分類及商品。
- worker artifacts 保留 14 天，供該批次彙整或補救。

### 2.5 完整性檢核與 Raw 封存

`src/package_and_upload_menu_snapshot.py` 在上傳前執行以下檢核：

- 實際 worker artifact 數必須等於預期數。
- 店家身分、批次與 JSON schema 必須通過 `src/snapshot_validation.py`。
- 菜單 JSON 的唯一店家集合必須與指派店家集合一致。
- `inactive_account` 店家比例必須低於 10%。
- 建立 tar.gz 後會重新讀取，確認其中 JSON 數量等於店家數。
- 計算整個壓縮檔的 SHA-256。
- 上傳後再查詢 HF，確認遠端檔案確實存在。

成功的 Raw 檔案位置為：

```text
Hugging Face dataset repo
└─ TaiwanMenuSnapshots/
   └─ <batch_id>/
      └─ taiwan_menus_<batch_id>.tar.gz
```

壓縮檔內含 `manifest.json` 與 `Json/` 下的每店 JSON。

## 3 Current 與 Events 建置

### 3.1 全歷史重建

`src/rebuild_database.py`：

- 列出 Hugging Face 中所有符合路徑格式的 Raw 快照。
- 按批次時間由舊到新重播。
- 驗證及解開每一份快照。
- 呼叫 `src/serving_state.py` 的 `apply_snapshot` 更新 Current 狀態並建立 Events。
- 第一份快照是 baseline，不為其中所有既有資料產生 `NEW` 或 `STORE_NEW` 事件。
- 可依店家 ID 分成 15 shard 平行重建，最後由 `scripts/merge_rebuild_shards.py` 合併。

### 3.2 每日增量同步

`src/sync_database_from_hf.py` 讀取資料庫中的 `metadata.latest_batch`，只重播更晚的 HF 快照。批次 ID 必須嚴格大於目前 `latest_batch`，否則拒絕套用，以防倒序或重複寫入。

目前定期 crawler 的 Stage 6 會：

1. 從 GitHub Actions cache 還原 `serving.db`。
2. 檢查該 DB 曾由至少三份歷史快照完整重建。
3. 依序同步 HF 中所有未處理快照。
4. 驗證 `stores` 與 `products` 非空。
5. 當 `TURSO_ACTIVE_SCHEMA_VERSION` 為 `v10` 且憑證齊全時，發布正規化 Current/Events 到 Turso。
6. 執行 HF 60 天保留政策的 dry run。
7. 部署 `web` 至 GitHub Pages。

## 4 壓縮版 Turso 建置與發布

`.github/workflows/publish_packed_turso.yml` 是手動工作流，流程如下：

1. 15 個 shard 各自從全部 HF 歷史重建正規化資料庫。
2. 每個 shard 以 MessagePack 序列化、Zstandard level 10 壓縮。
3. 每個壓縮 shard 都與原始正規化資料逐表比對筆數與 SHA-256，確保可無損還原。
4. 合併 15 個 packed shard。
5. 強制最終檔小於 4.75 GB，以保留至少約 5% Turso 免費空間餘裕。
6. 建立全新的隔離 Turso database，整檔上傳。
7. 分別使用寫入權杖與瀏覽器唯讀權杖驗證遠端資料。
8. 自動更新 `web/config.js` 指向新資料庫，commit 回 `main`，再觸發 Pages 部署。

壓縮的目的不是刪除資料，而是把正規化多列資料包成以店家為單位的 blob，減少 Turso 列數與空間，同時保留全部 store、product、event、batch 與 metadata。

## 5 前端實際載入順序

`web/app.js` 的載入優先順序是：

1. 若 `ENABLE_TURSO=true`，使用 `web/packed-turso.js` 讀取 packed Turso。
2. 同時嘗試從 `web/data/` 讀取 `stats.json`、`discounts.json`、`new_stores.json`、`new_products.json`、`promotions.json`。
3. 若 Turso 失敗且未開啟附近範圍篩選，回退到整套靜態 JSON。
4. 若還有舊式內嵌 `window.UBER_RADAR_DATA`，再作最後離線備援。
5. 開啟附近篩選時若 Turso 失敗，不使用缺少可靠座標的舊靜態資料，會顯示空資料與錯誤提示。

因此，目前網站的全品搜尋與附近查詢主要來自 packed Turso；大特價、新店家、新商品、促銷清單則優先使用 `web/data` 下相對應 JSON。這是理解畫面數字與資料新鮮度時最重要的界線。

