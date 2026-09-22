# Uber Eats 全台監控系統文件

本目錄說明目前系統的資料採集、資料處理、比對規則、資料表、儲存位置、發布方式，以及前端實際使用資料的優先順序。

## 文件索引

1. [系統架構與資料流程](SYSTEM_OVERVIEW.md)
   - 從 GitHub Actions 爬蟲到 Hugging Face、SQLite、Turso 與 GitHub Pages 的完整流程
   - 目前定期排程、平行工作方式、失敗保護與發布流程

2. [商品與店家判定規則](BUSINESS_RULES.md)
   - 如何辨識同一店家與同一商品
   - 如何判定特價、新店家、新商品、促銷、下架與重新出現
   - Current/Events 與靜態 JSON 兩套口徑的差異

3. [資料表與儲存位置](DATA_MODEL_AND_STORAGE.md)
   - 正規化資料庫與壓縮版 Turso 的所有資料表
   - 欄位用途、主鍵、索引、事件種類
   - Raw、Current、Events、前端靜態備援各自存在哪裡

4. [執行維運與已知注意事項](OPERATIONS_AND_CAVEATS.md)
   - 排程、保留期限、重建、發布與前端回退行為
   - 目前程式實作中需要特別注意的口徑差異與限制

5. [網站搜尋與資料正確性需求](SYSTEM_REQUIREMENTS.md)
   - Current 搜尋集合、正式主鍵、各頁籤一致定義
   - 發布批次契約、長期歷史保存與必要驗收條件

## 最重要的結論

- 原始完整菜單快照放在 Hugging Face Dataset 的 `TaiwanMenuSnapshots/<批次>/`，批次格式為台灣時間 `YYYYMMDDHHMMSS`。
- 正規化歷史狀態由本地建置時的 `serving.db` 保存，再發布至 Turso；資料分為目前狀態 `stores`、`products`，以及變更歷史 `events`。
- 正式身分以 Uber 的 `store_uuid` 和商品 `identifier` UUID 為準。缺少 UUID 時才使用具命名空間的 SHA-256 fallback ID。
- 正規化資料庫中的「價格優惠」以最近三個有效原價的中位數為基準；新價格必須不在前三個價格中且低於中位數。
- Turso 啟用時，各情報頁籤的搜尋會保留自身條件：新店只搜尋最近 7 天新店、新品只搜尋 `f:new`、促銷只搜尋正式促銷集合、大特價只搜尋歷史降價集合。
- 靜態 JSON 是 Turso 失敗時的備援。正式發布必須確保 packed 與靜態摘要批次一致；不得再把不同日期資料混成同一畫面。

## 主要程式入口

| 用途 | 程式 |
|---|---|
| 全台採集排程 | `.github/workflows/taiwan_store_crawler.yml` |
| 快照封裝與上傳 | `src/package_and_upload_menu_snapshot.py` |
| 正規化 Current/Events | `src/serving_state.py` |
| 從 HF 全歷史重建 | `src/rebuild_database.py` |
| 增量補齊新快照 | `src/sync_database_from_hf.py` |
| 壓縮資料庫建置 | `scripts/prototype_pack_db.py` |
| 壓縮分片合併 | `scripts/merge_packed_shards.py` |
| Turso 發布 | `scripts/publish_turso.mjs`、`scripts/publish_packed_turso.mjs` |
| 前端壓縮資料讀取 | `web/packed-turso.js` |
| 前端資料載入與篩選 | `web/app.js` |
| 靜態 JSON 情報產生器 | `src/export_static_snapshots.py` |

