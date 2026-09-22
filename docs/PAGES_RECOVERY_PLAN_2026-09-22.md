# GitHub Pages、HF 與 Turso 調整計畫

盤點基準：Git HEAD `5b9a496d1adf7a78ae1ceb6d1a4ceceef070ed2e`。本輪只查核與規劃，未修改正式資料、workflow、網站程式，未重跑或部署。已閱讀根目錄 AGENTS.md。

## 1. 結論

問題同時存在於網站資料契約、發布版本、Actions 工作集容量三層。刪除一次 VACUUM 或提高 delta 上限，都不能讓網站正確運作。

建議正式鏈路維持 crawler → HF Raw → 分片 current/event 處理 → packed serving → Turso → Pages；但取消每次日常執行的全國 30 GB 單檔 checkpoint，讓 Pages 只有一個部署入口，並將格式 migration 與日常 delta 分開。

本次瀏覽器已重現 `$NaN`、錯誤 `$0`、明顯縣市錯置，依 AGENTS.md 屬發布阻擋。既有 v1 是可回復的部署基準，但不能稱為已知「功能正常」版本。

## 2. 已查證狀態與數字口徑

| 項目 | 本次查核結果 | 判讀 |
|---|---|---|
| Git | 本地 HEAD 與最新失敗 run 均為 `5b9a496…` | 修改前 SHA；本輪沒有修改後 commit |
| HF | `hub-google/UberEat`，revision `1292c093e155069e62cbb7df84666bf139f43797` | 以 HF dataset API 查到，後續重建應固定 revision |
| HF Raw | 60 個 tar.gz，合計 **12,168,409,103 bytes** | 壓縮檔總量，不是解壓工作集；60 份不代表 60 天 |
| 最新 Raw | `20260922183902`，**219,884,809 bytes** | SHA256 `4eeee2b76fb6fb914be90d2c9179884fc2021fe47c61d5009e82e1096c826b5f` |
| HF 現行檔案 | 共 65 個；主要路徑 TaiwanMenuSnapshots、TaiwanStores | 未看到既有 `v2/history/events/` 檔案，不能把程式中的預定路徑當成已存在事件備份 |
| Turso subscription | 組織 `eeetchen`，`plan/name=starter`、`overages=false` | 已查方案代碼，但 API 未給實際 storage quota；不能直接映射為現行公開 Free/Developer 額度 |
| Turso usage | 組織 **4,097,568,768 bytes**；本期讀 21,900,636 rows、寫 794,372 rows | 即時讀取會增加 reads；不是本地 packed 檔案大小 |
| Turso DB | `food`、`ubereats-packed-v1` | 不刪任一現存 DB；usage 中歷史已刪 DB 的條目不代表仍可用的回滾版本 |
| Production v1 | stores **150,353**，products metadata **8,184,725**，buckets **8,192**，chunks **150,618** | browser read-only token 可執行讀取；未以寫入測試權限 |
| v1 metadata | format=1；source_counts.events=**27,689,946** | 缺 latest_batch、latest_processed_at、definition_version、active counts；source_counts.crawl_batches=840 是 shard 累加，不能解讀為 840 個唯一批次 |
| 新版 build 失敗 run | merged packed **2,727,424,000 bytes**；rekey 後 **2,728,435,712 bytes** | run 35708276232 實際產物，比文件引用舊成功 4,077,486,080 bytes 小；尚未成功發布 |
| 最新 normalized build | 文件及 run 記錄約 **30 GB**；stores 150,513 / products 8,197,382 | 是離線 union state；不能拿去 Turso，也不能拿 cache 壓縮大小估工作集 |
| Actions cache | 2 份、**8,564,951,496 bytes** | API 壓縮儲存量；只可加速，不能做唯一 baseline |
| Artifacts 全部分頁 | 2,022 件，列示大小總和 **55,079,500,593 bytes** | 含 1,395 件 expired；不是當前計費容量 |
| Artifacts 未過期 | 627 件，**30,538,931,390 bytes** | 適合估算仍可下載的產物；仍不等同帳務計費讀數 |
| Pages artifact | **17,120,785 bytes** | run 35708253698 的 github-pages artifact，距 Pages 1 GB 限制很遠 |
| 網站畫面 | 首頁顯示 **2026-09-15 06:39**；全庫搜尋出現 `$NaN` / `$0` | 不是只有資料更新慢，已有正確性問題 |

更正原 incident 文件：`10,414,216,094 bytes` 是 artifacts API 第一頁 100 件總和，不能與 total_count=2,022 合稱全庫總量。AGENTS.md 的該數字也需註明此口徑，但本輪不擅自覆寫使用者規範。

本次是盤點 smoke check，沒有宣稱全部 UI 驗收通過。瀏覽器已查看首頁、特價預設列表、全庫搜尋第一頁及 console；當時 console 只見 Tailwind CDN production warning，畫面資料錯誤仍已足以判定未通過。關鍵字、各縣市、分頁、四情報頁籤與價格走勢的完整矩陣留待修復後驗收。

## 3. 平台限制與遷移可行性

- GitHub public `ubuntu-latest` 官方配置仍是 **4 vCPU / 16 GB RAM / 14 GB SSD**。先前 145 GiB root、57 GiB available 只是一個 run 的實測，不能作為保證。
- 保留單檔 packed guard **4,750,000,000 bytes** 與日常 delta guard **262,144,000 bytes**。不提高數字。
- Turso 官方公開 Free=5 GB、Developer=9 GB included；實際帳戶回傳 starter，必須再取得 dashboard/平台確認的 quota，才可算實際剩餘及發布。
- 現有使用量 + 已測新版 candidate = **6,826,004,480 bytes**，還沒加匯入成長、暫存或其他 DB 用量。這是容量估算，不是可發布承諾。
- 假如實際額度只有 5,000,000,000 bytes，帳面剩 **902,431,232 bytes**，不足以保留 v1 再加目前 candidate。假如確認為 9,000,000,000 bytes，兩者合計後帳面剩 **2,173,995,520 bytes**，仍須實測遠端占用及保留餘裕。以上為條件計算，不是帳戶額度斷言。
- 若 quota 不足：保留 v1，先完成本地重建/驗證與縮小 serving projection；若仍不足，必須另取得容量。不能靠先刪 production 或覆寫它騰空間。
- cache 預設 10 GB；第三份同級 cache 會超過預設值。不能以可以加購為由提高本專案預算。
- Pages 限制 1 GB、部署 10 分鐘、soft bandwidth 100 GB/月；目前最主要問題不在 Pages 容量。

官方來源：[runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)、[Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits)、[cache 政策](https://github.blog/changelog/2025-11-20-github-actions-cache-size-can-now-exceed-10-gb-per-repository/)、[Turso pricing](https://turso.tech/pricing)、[subscription API](https://docs.turso.tech/api-reference/organizations/subscription)、[usage API](https://docs.turso.tech/api-reference/organizations/usage)。

## 4. Runs 失敗原因與修正位置

| Run | 證據與原因 | 明確修正 |
|---|---|---|
| [35717116437](https://github.com/fafagoback/UberEat/actions/runs/35717116437) | 本次重讀 failed log：checkpoint `database or disk is full`；workflow 無條件 VACUUM | 刪除日常全國 serving.db 還原/壓縮路徑；Pages 拆離 checkpoint。僅跳過 VACUUM 不解決 30 GB > 官方 SSD 的架構矛盾 |
| [35708276232](https://github.com/fafagoback/UberEat/actions/runs/35708276232) | 本次重讀 failed log：delta **986,068,214** > **262,144,000** | definition/index migration 建新版本 DB；禁止直接在 v1 ALTER 後再嘗試全量 delta |
| [35704115588](https://github.com/fafagoback/UberEat/actions/runs/35704115588) | API 確认 success | 只證明當次發布工作完成，不能推論今天前端正確 |
| [35708253698](https://github.com/fafagoback/UberEat/actions/runs/35708253698) | API 確认 Pages success | 補 UI/資料 smoke tests，不能只驗 upload/deploy |
| [35696024819](https://github.com/fafagoback/UberEat/actions/runs/35696024819) | 既有 incident 記錄 expected 15 shards, found 0；本次確認 run failure，未重讀全 log | reuse_run_id 前先查 artifact 名稱、過期、完整 shard 集合與版本 manifest，不相容即停止 |
| [35671209057](https://github.com/fafagoback/UberEat/actions/runs/35671209057) | incident 記錄 row index/count 錯誤 | 保留 zero-based 修正，增加 0、1、缺號、末 row 邊界測試 |
| [35675719906](https://github.com/fafagoback/UberEat/actions/runs/35675719906) | incident 記錄上傳成功後 push/trigger 收尾失敗 | 將 DB 驗證、config commit、Pages dispatch 記錄為不同狀態；dispatch 重試不重傳 DB |
| [35663638985](https://github.com/fafagoback/UberEat/actions/runs/35663638985) | incident 記錄 normalized publish HTTP 404 | 移除舊 normalized 發布路徑；目標 URL、org、DB name 與 release manifest 要一致 |

同一問題已兩次失敗時，停止 rerun。後續以既有 Raw 重建，不要求再爬一次來測部署。

## 5. 分階段修改清單

### P0：先封住已知錯誤並拆開發布責任

1. `.github/workflows/taiwan_store_crawler.yml`
   - 保留 Stage 1–5 採集、完整性檢查與 HF archive；Stage 5 輸出 release manifest：batch、HF revision/path/checksum、store/product count、worker IDs、source SHA。
   - 移除 Stage 6 的 `publish_turso.mjs serving.db`，不保留用變數 v10 啟用的後門。
   - 取消 daily 單檔 serving cache、VACUUM 與內嵌 Pages deploy；改接獨立分片 build 工作。checkpoint 失敗保留明確 failure/警告，不用 continue-on-error 假裝已更新資料。
   - 調整 retention：已由 HF 驗證封存的 stores/menu/archive artifacts 預設 1–2 天；失敗診斷另存小型 manifest/log。現有 90/14/30 天不得繼續當資料湖；已有 artifacts 的刪除另行規劃，不在本輪執行。
2. `.github/workflows/deploy_pages.yml`
   - 作為唯一 Pages deploy 入口，統一 concurrency。
   - deploy 前檢查 web/ 白名單、總量、無 DB/archive/大型資料；驗證前端支援 release 定義。
   - 增加前端 contract tests 與真實瀏覽器 smoke；單純部署修正前端可獨立於 HF/checkpoint。
3. `.github/workflows/rebuild_turso.yml`、`scripts/publish_turso.mjs`、`scripts/provision_turso_rebuild.mjs`
   - 取消 normalized 全國 DB 合併後發布與 `TURSO_RESET_TARGET=1`。
   - 舊 publisher 的 production 使用應硬拒絕；只保留必要離線用途或停用。只檢查檔名不夠，還需 packed schema/metadata fingerprint。
   - provisioning 僅允許新命名 `ubereats-packed-v2-<release>`；已存在的不相容目標拒絕，永不 delete/reset production。
4. `web/app.js`、`web/packed-turso.js`
   - 對缺少 definition_version 的 v1 做明確兼容檢查；缺批次時不得採用 9/15 JSON 時間宣稱當前資料。
   - 統一 `price/effective_price/quantity/promo_type` 輸入 DTO，`Number.isFinite` 與正價格守門；缺價顯示「價格未提供」且不進優惠/價格排序，不能 fallback 成 $0。
   - 促銷單價只計算一次，不在 renderer 再套一次買送折算。全庫 renderer 目前直接 `Math.round(p.price)`，是 `$NaN` 的可見輸出點；需 trace Raw → normalized → packed → DTO 後用固定樣本重現，不能只刪字樣。
   - 同批次統計與列表一起更新；目前僅清空 mismatch 清單卻仍讀 baselineStats，且 v1 無 batch 時 mismatch guard 不會生效。

### P1：HF 成為可重建基礎，日常只处理增量

5. `src/package_and_upload_menu_snapshot.py`、`src/snapshot_validation.py`
   - 保留不可变 Raw；增加外置 manifest 與批次完成標記，內容包括 archive SHA256、實際唯一 counts、抓取失敗/缺失率、schema version、時間區。
   - 完成標記最後寫入；下游只取完整 batch。日常與重建皆 pin HF revision + source batch，不讓 15 workers 各自讀不同時間的 main。
   - 後續追加按 store UUID 穩定 hash 的 Raw 分片（不是按城市或 crawler worker 隨機任務分片），以減少每個 rebuild worker 下載全部 12.17 GB 歷史。
6. `src/rebuild_database.py`、`src/sync_database_from_hf.py`
   - 增加 `--revision`、`--through-batch`、`--manifest`、`--event-retention-days` 與預算參數；禁止未知最新批次自行前進。
   - 重建第一次可串流讀舊 tar，每次只保留當批、限制 prefetch；其後讀持久化 shard checkpoint + 未處理 Raw。
   - cache miss 時由 HF 已驗證 checkpoint 或 Raw replay 恢復，不再只報「先跑另一條 workflow」。
   - 目前 rebuild `event_retention_days=None`；與 daily 60 天一致化，且保留 first_seen、missing_streak、最近三次有效價格及 identity continuity。
   - invalid/missing snapshot 不可靜默跳過後宣稱完整 history；報告來源缺口，必要時阻擋發布。
7. `src/serving_state.py`、新增 `src/checkpoint_store.py` / `src/export_event_partitions.py`
   - Current checkpoint 按穩定 UUID hash 分片；只留下一輪 diff 必需的狀態及 identity ledger，不能把 inactive 清空後破壞「新品/重新出現」定義。
   - 詳細事件以 batch/shard 分區寫 HF，events 用可重播的穩定 ID，避免自增 row ID 因重建/retention 漂移。
   - normalized 全歷史 archive 與 browser serving projection 分開：Raw 保留完整原文；Turso 只留網站需要的 current 欄位、優惠與可驗證的價格時間序列。移除完整 old/new JSON 前先訂投影契約與重播 equivalence tests。
   - 禁止宣稱縮小後一定幾 GB；先量測最大 shard 的 DB/WAL、RAM、事件量與實際 packed 體積。
8. `src/hf_retention.py`、`.github/workflows/hf_retention.yml`
   - 現行 batch 命名是 Asia/Taipei，但 `batch_time` 直接標 UTC，需修正並測試 cutoff 邊界。
   - retention 前必須驗證持久 baseline + 後續 Raw 可重建。若只保留 60 天 Raw，必須保存 cutoff 前的可重建 baseline/identity ledger 及所需 Raw 依據；不能再聲稱可重建任意全歷史。
   - 新 baseline 用隔離路徑完成重播驗證後才能換 pointer；保護仍在 build、production、rollback 的來源。初期只 dry-run，未證明可恢復前不刪 Raw。

### P1：packed 版本、容量、增量與發布一致性

9. `scripts/prototype_pack_db.py`、`scripts/merge_packed_shards.py`、`scripts/rekey_packed_db.py`
   - metadata 必含 format、definition_version、source revision、latest_batch、active/union counts、唯一 batches、各 shard checksum；merge 拒絕不同版本/批次或 shard 缺漏/重複。
   - 目前 prototype/merge 也有無條件 VACUUM，全部套用 `free >= 2 × DB + 10 GiB` 才允許；更適合 fresh build 預設不做 VACUUM。
   - 避免同時保存全部 packed shards、merged、stable copy 與 SQLite 暫存；分段下載/合併後釋放已驗證輸入，或在 shard 直接套 stable mapping，避免完整 rekey 複製。
   - 除 stable store IDs，也穩定 product refs、chunk boundaries、token ordering、event ID；volatile last_seen 等資訊與大 blob 拆分，減少每日全桶 checksum 抖動。
   - 目前 packed 雖包含完整 events，但這不是必須放 Turso 的網站需求；新 serving projection 以固定價格走勢/優惠 fixture 與 Raw 比對完整性，不以較少筆數掩飾資料遺失。
10. `scripts/publish_packed_turso.mjs`
   - 新增只讀 `plan` 階段：先比版本與計算各表 changed/deleted rows/bytes、峰值/最終空間與限額，再任何遠端 DDL/DML。現在 CREATE/ALTER 發生在 delta guard 前，超限也可能已修改 schema。
   - 用 SQLite iterator/keyset pagination 取代對全部 blob `.all()`；逐批按 rows 和 bytes 雙限制傳輸，遠端 checksum 分頁。
   - `bootstrap` 僅限經 quota 查核、空的新版本 DB；`delta` 僅同 definition 既有目標且 <=250 MiB。全量 bootstrap 是獨立模式，不是提高日常 guard。
   - metadata 最新批次目前最先寫入，應最後完成且有 release 狀態。分多個 transaction 寫活庫仍有部分更新風險，僅「metadata 最後」不能保證原子性。
   - 新版採 release-addressed changed rows + active release pointer（讀取固定 release、未變資料可引用舊版）；最後驗證後原子切 pointer，保留上一 release 至安全回收。容量包含兩版共存/差異，不符合則阻擋。此 schema 改動也必須在新版本 DB 完成，不能補到 v1。
11. `.github/workflows/publish_packed_turso.yml`、新增 `scripts/check_capacity.py` / `scripts/check_turso_capacity.mjs`
   - pipeline 以 Stage 5 已驗證 manifest 成功為輸入，與 normalized checkpoint 成敗解耦；可先將重建移出 crawler 使 workflow_run success 真正代表 archive 成功。
   - 新增 mode=plan/bootstrap/delta；manual reuse 要先校驗 artifact 存在、未過期、全部 shard、來源版本及 checksum。
   - 上傳前查 actual plan/quota、org storage、剩餘 reads/writes；guard 檢查 rekey 後最終檔案及遠端預估總量。
   - 每個 job 開始及大步驟前記錄 `free/df/du`；預估超標在下载/還原前擋下。所有 publisher 使用同一 concurrency，阻止重建與日常互相覆寫。
   - 改 dependencies 為固定版本/lockfile，避免 rerun 同 SHA 得不同 codec/序列化輸出。
12. `scripts/verify_uploaded_packed_turso.mjs`、`scripts/update_web_turso_config.mjs`
   - 現有 verifier 只比各表 count + metadata，不會檢出相同 count 的錯誤 blob。
   - 補全量 checksum 對照、分批遠端解壓 round-trip/語意驗證、固定查詢 fixtures 與 read-only token 驗證。監控讀取預算，不能只驗第一個 bucket。
   - 僅新版本 DB 和候選前端一起通過後產生 config commit；保存 before/after SHA、DB、release、batch。git push/dispatch 失敗可重試收尾，不能重新 bootstrap。
   - 回滾需成對回復前端 SHA + DB/release，不能只換 URL。現有 v1 只標記為舊部署基準，待修復前端完成兼容驗收後才標記已知正常。

### P1：地理與完整網站驗收

13. `src/serving_state.py`、`src/export_static_snapshots.py`、新增共用城市正規化模組
   - 現行 serving 將 addressRegion/locality 直接當 city；static exporter 對「中山」等行政區字串做單一城市猜測，都不可靠。
   - city 使用台灣縣市 canonical code；優先可信完整地址/region，再用明確行政區組合或座標行政邊界。模糊值留未知並計入品質報告，不能默認台北市。
   - 保留 Raw original address，變更正規化後 rebuild 新版本；前端城市過濾按 canonical code，不能以店名猜城市。
14. `web/app.js`、`web/packed-turso.js`、`src/export_static_snapshots.py`
   - 四情報頁籤、全庫搜尋與首頁統計使用相同 release/batch/規則。
   - 固定 50,000 結果上限不能冒充全庫精確總筆數；改 cursor 分頁和可正確計算的總數，或明確標示截斷與可查范围。
   - 小型 fallback 帶相同 release ID、資料時間及降級提示；清除依賴舊大型 parquet/JSON 的正式流程，再從 Pages 白名單移除。
15. `tests/frontend.test.cjs`、`tests/test_serving_state.py`、`tests/test_packed_search_index.py`、`tests/test_pipeline_safety.py`、新增 `tests/browser_smoke.spec.*`
   - fixtures：缺價/0/負值/字串、買一送一、一般品、關閉/下架品、相同商品跨 chunk、新店新品、三次價格觀測、台東中山門市。
   - recovery：空 cache、缺/重複 shard、跨 definition/revision、超限零遠端寫入、中途上傳失敗、metadata 未切換、保留回滾、retention cutoff。
   - UI：首頁統計、四情報頁籤、空白/一字/二字/多字搜尋、固定「雞排/珍珠奶茶/寵物公園」案例及 normalized 預期集合、縣市、前後頁、價格走勢、console 與解壓錯誤。

## 6. 工作集設計與執行順序

每個 job 的磁碟峰值模型：依賴與 checkout + 同時存在的壓縮 Raw + shard DB + WAL + packed 輸出 + 下載/上傳暫存 + 安全餘裕。不能只拿「30 GB ÷ 15 = 2 GB」保證各 shard 均勻或成功。

先以最大事件量/商品量 shard 試建，記錄 peak RSS、DB/WAL/暫存與時間。暫以 15 shards 起步；若單 worker 或 merge 峰值超過官方配置預算，增加 shard 或改串流輸出。每次預算以實測可用磁碟再檢查；不以清除 runner 預裝工具換取額外磁碟作基本設計。

1. 建立固定 Raw/錯誤 UI fixtures 和 release contract；完成前端價格/城市/版本防護與測試。
2. 清除兩條 normalized publish/reset 路徑，拆離 crawler archive、build、Pages，加入容量與 artifact preflight。
3. 使用現有 HF revision，先跑 isolated shard/pack/merge 的 plan，完成 source counts、checksum、round-trip 與容量報告。**不 rerun crawler 來修發布。**
4. 取得 starter 實際 storage quota；若不容納新舊 DB + 餘裕，保持 production，先調整 projection 或容量再繼續。
5. 新版本 bootstrap → read-only API/候選前端 browser smoke → 切 config → Pages deploy → 線上完整驗收。任一步失敗不切換。
6. 用相同 batch 重跑 plan，預期 delta=0；再用下一完整 batch 驗證 stable IDs 與 <=250 MiB，才能啟用定期 delta。
7. 拿掉 cache 模擬恢復、模擬 publish 中断/回滾；最後才啟用依賴 baseline 的 retention。

## 7. 完成定義

成功不能只填 Actions 綠燈。每次發布要保存：before/after SHA、全部 run URLs、source revision/latest batch、runner RAM/disk 峰值、最終 DB bytes、cache compressed bytes、artifact 增量/retention、Turso plan/quota/used/remaining、store/product/bucket counts、checksum/round-trip 報告、Pages artifact bytes、browser smoke 結果、上一個可回滾 release。

本輪交付：查核與上述逐檔方案；尚未實作、migration 或完整 smoke，因此不宣稱网站已修好。眼前硬阻塞是錯價/錯城市/版本混用，以及未確認的 Turso 雙庫共存額度；Actions 則必須先移除全國 30 GB 日常 checkpoint 才能符合標準 runner 的設計下限。
