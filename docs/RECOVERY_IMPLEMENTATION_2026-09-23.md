# 本次修正與執行方式

修改前 SHA：`5b9a496d1adf7a78ae1ceb6d1a4ceceef070ed2e`。

## 已實作

- crawler 到 Stage 5 完整 Raw 封存即完成；移除 30 GB serving.db 日常 cache、VACUUM、normalized Turso publish 與第二個 Pages deploy 入口。
- 新 Raw archive 與 complete.json 使用同一 HF commit，輸出帶 checksum/revision 的 release manifest。舊 Raw 保留可重播；retention 自動流程只 dry-run，未有持久 baseline 前禁止實際刪除。
- packed workflow 固定 HF revision，15 個 shard 嚴格重播、各自 lossless round-trip、檢查磁碟預算，merge 拒絕缺片/重複/跨版本，取消多份完整 DB rekey 複製及無條件 VACUUM。
- 加入完整 blob checksum、解壓長度、產品搜尋 reference、directory chunk/count 驗證。搜尋詞排序固定，版本升為 `2026-09-search-v3`。
- normalized 舊 publisher 硬拒絕，provision 禁止 reset/delete；packed publisher 只接受全新版本空庫，串流上傳，metadata 最後寫。原位 delta 暫停，不提高 250 MiB 上限。
- bootstrap 在任何 DB mutation 前檢查 confirmed plan/quota、組織 usage、新舊 DB 共存餘裕；不自動切換 web/config.js。
- 前端共用價格欄位契約，修正 curr_raw_price → price 缺漏、禁止缺價變 $0、有效單價不重複折算、保留小數避免 $0.5 顯成 $0。
- 城市正規化不再以「中山」等路名猜城市，優先完整地址，模糊舊備援城市留未知。修正歷史事件後續 chunks 遺失欄位定義的讀取問題。
- 舊版 v1 不含最新 batch/definition 時，顯示待更新/待同步，不混用 9/15 的統計冒充最新；保留相容的有限商品瀏覽與關鍵字搜尋。
- Pages 產物改為 `_site` 白名單，只保留每類至多 200 筆的小型、明確標日期的 fallback，沒有 SQLite/Raw/Parquet。候選未壓縮大小 **942,192 bytes**；正式 Pages artifact 大小須由執行後報告確認。
- Pages deploy 前執行 Node tests + CLI headless Chromium smoke；checkpoint 失敗不影響獨立 Pages workflow。

## 驗證

- Python：35 tests passed，含 Raw → 2 shards → packed → merge → checksum/引用驗證，以及損毀 blob / 跨 revision 必須失敗。
- Node：16 tests passed，含舊資料欄位轉換、無效價格、城市、原有前端規則。
- CLI Chromium：候選前端搭配目前 production read-only DB，檢查首頁、四情報頁籤、全庫卡片、前後分頁、價格走勢視窗、雞排關鍵字及台北篩選；未見卡片 `$NaN` / 錯誤 `$0` / 負價格或 JS errors。
- fixture merge 的 stores/products 各 4、唯一 batch 1。這是整合測試資料，不是最新全國 production counts。
- production 仍為 v1：盤點 stores 150,353、products metadata 8,184,725、buckets 8,192；未執行 DB migration。

## 此次啟動的 run

`gh workflow run publish_packed_turso.yml --ref main -f mode=plan`

plan 會真正重建與驗證 HF 資料，但不發布 Turso；run 成功只表示建置驗證通過。Push 的 web 變更另會觸發既有 Pages push workflow，通過 CLI smoke 才部署。依使用者要求，送出後不監測。

## 尚未完成、不可冒稱已修復的部分

實際 Turso plan 代碼是 starter，盤點已用 **4,097,568,768 bytes**；API 未提供可確認的 storage quota。不得假設 10 GB。要啟動 bootstrap，先設定經帳戶確認的 `TURSO_ORGANIZATION`、`TURSO_CONFIRMED_PLAN`、`TURSO_CONFIRMED_QUOTA_BYTES`。舊 candidate + 現有 used 約需 6.83 GB，尚未含 reserve，新 build 大小以 run 實測為準。

取得容量後，可手動 bootstrap 新版空庫；完整新庫/候選前端驗收通過後才切 config。日常原子 delta、持久分片 checkpoint、HF event 分區及 serving projection 瘦身仍屬下一階段；本次採可由保留 Raw 重建的分片流程，沒有假裝已實作這些優化。

回滾：目前 production DB/readonly config 不變；前端可回到上述修改前 SHA。v1 有既知資料定義缺陷，只是舊部署基準，並非已通過完整新版驗收的資料庫。
