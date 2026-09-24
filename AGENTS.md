# UberEat 專案強制規範
更新：2026-09-22。每次修改程式、資料庫、Actions 或 Pages 前必須閱讀本檔；限制有變時先查官方與帳戶實際用量，禁止猜測。

## 架構與硬體
- 正式鏈路：Uber crawler → GitHub Actions → HF Raw → packed DB → Turso → GitHub Pages `web/`。
- `serving.db` 是離線正規化 Current/Events 建置庫；禁止上傳 Turso、禁止放入 Pages。
- Repository 是 public；workflow 使用標準 `ubuntu-latest`，無 self-hosted runner。
- GitHub 官方 public `ubuntu-latest`：4 vCPU、16 GB RAM、14 GB SSD；每個 job 是即用即棄 VM。
- 實際 run 曾看到 15 GiB RAM、145 GiB root、57 GiB available；這不是保證，設計必須採官方下限並在 job 內用 `free/df/du` 實測。
- `serving.db` 解壓後約 29–30 GiB；Actions cache 顯示約 4.3 GB 是壓縮大小，兩者不得混用。
- 大型 SQLite 禁止無條件 `VACUUM`；只有 `free >= 2 × DB + 10 GiB` 才允許執行。

## 平台容量
- Turso 擁有者指定硬上限 10 GB；帳戶實際方案未知時，以 repository 現有 4,750,000,000 bytes guard 為準，禁止提高。
- Turso 官方目前 Free 5 GB、Developer 9 GB included；發布前必須查實際 plan、已用與剩餘容量。
- 已成功 packed DB：4,077,486,080 bytes；容量餘裕很小。`serving.db` 29–30 GiB 絕對不得發布。
- 日常 Turso delta 上限 `262144000` bytes（250 MiB），禁止為了綠燈調高。
- Actions cache 預設 10 GB；目前 2 份共 8,564,951,496 bytes，再存同級 cache 會淘汰／thrashing。
- Actions artifacts 目前 2,022 件、共 10,414,216,094 bytes；中間產物必須用最低必要 retention，不得當資料湖。
- GitHub Pages published site 上限 1 GB、部署 10 分鐘 timeout、soft bandwidth 100 GB/月；目前 Pages artifact 約 17.1 MB。

## 禁止事項
- 禁止執行 `publish_turso.mjs serving.db`；Turso 只接受驗證後的 `packed-serving.db`。
- 禁止先刪、reset 或覆蓋 production DB；必須先建 versioned 新 DB、驗證、切換並保留回滾。
- packed schema、payload、ID 或 search index 變更必須升版新 DB，不得對舊 DB 做近似全量原位更新。
- 禁止把 cache 當永久備份；baseline 必須可由 HF Raw 重建。
- 禁止把 SQLite、snapshot、packed DB 或大型 JSON 放入 `web/`；Pages 只部署靜態前端與小型 fallback。
- `web/config.js` 只能放 browser read-only token；write/platform/HF token 禁止進入 web、log、artifact 或 commit。
- 禁止用 Actions 綠燈、HTTP 200 或首頁可開啟宣稱「網站已修好」。
- 同一問題連續失敗兩次後停止 rerun，先取得完整 log、重現與容量證據。

## 每次發布必須通過
- 記錄 commit、run URL、source/latest batch、runner RAM/disk、DB 實體大小、cache 壓縮大小、artifact 增量與 retention。
- `packed-serving.db < 4,750,000,000 bytes`，並記錄 Turso plan／上限／已用／剩餘；查不到就不得發布或提高 guard。
- 驗證 store/product/bucket counts、checksum、解壓 round-trip、read-only token 與固定搜尋案例。
- 禁止 `$NaN`、`undefined`、錯誤 `$0`、負價格、明顯縣市錯置；任何一項出現即阻擋 production。
- 真實瀏覽器逐項驗證：首页統計、四個情報頁籤、全庫搜尋、關鍵字、縣市篩選、分頁、價格走勢、console 無 401/404/JS/解壓錯誤。
- 新 DB 全部通過後才能改 `web/config.js`；失敗時維持上一個已知正常版本。
- Pages 部署與 DB checkpoint 解耦；checkpoint 失敗不得阻止已驗證的靜態前端部署。
- 完成時回報修改前後 SHA、所有容量數字、資料筆數、Pages artifact、smoke-test 結果與回滾版本。

官方依據：GitHub hosted runners、Actions cache/billing、Pages limits、Turso pricing；平台規則可能更新，改版前必須重新核對。
