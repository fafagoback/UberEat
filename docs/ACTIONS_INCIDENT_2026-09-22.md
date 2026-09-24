# GitHub Actions 調整與失敗盤點（2026-09-22）

> 盤點時間：2026-09-22（Asia/Taipei）  
> Repository：`fafagoback/UberEat`  
> Actions：<https://github.com/fafagoback/UberEat/actions>

## 結論

- 今天 `main` 有 **13 次 commit**：其中 **11 次人工程式／文件調整**，另有 **2 次 GitHub Actions bot 自動更新 `web/config.js`**。
- 以台北時間 2026-09-22 00:00 起算，已完成的 workflow run 共 **15 次**：**5 成功、7 失敗、3 skipped**。
- GitHub Pages 的 3 次部署全部成功。現在 Pages 狀態是 `built`，首頁與 `config.js` 均回應 HTTP 200。
- 最新失敗不是網站 build 失敗，而是資料 pipeline 在最後建立下一輪 checkpoint 時，對 **30 GB** 的 `serving.db` 執行 `VACUUM`，SQLite 因暫存空間不足而回報 `database or disk is full`。
- 最新 pipeline 在失敗前已完成兩批 HF snapshot 同步，並成功驗證 `150,513` 家店、`8,197,382` 項產品；但 checkpoint、Pages artifact 與 deploy 步驟沒有完成。
- 目前線上網站仍由 17:04 完成的 Pages 部署提供，指向 16:42 成功發布的 packed Turso database。網站殼可開啟，但最新 18:39 批次沒有透過這次失敗的 pipeline 發布。

## 今天改了幾次、每次改了什麼

下表只列 `main` first-parent 歷史，避免把 stash/WIP 物件誤算成正式發布。

| 台北時間 | Commit | 執行者 | 調整內容 | 結果／目的 |
|---|---|---|---|---|
| 08:15 | `ba7ec4d` | fafagoback | packed Turso 改為更新既有 DB，不再每次 reset；調整 provision/publish scripts | 避免破壞式整庫重建，但第一次仍因 row index 假設錯誤失敗 |
| 09:25 | `532830a` | fafagoback | packed row 改成 zero-based 發布 | 修正 `store_directory: source ended before expected row count`；後續資料上傳成功 |
| 10:46 | `5749f05` | github-actions bot | 更新 `web/config.js` 指向新 Turso DB | 自動部署設定；Pages 成功 |
| 14:41 | `b8a0e58` | hub-google | deployment trigger 加入 GitHub token 認證 | 嘗試解決 workflow dispatch 無權限／認證問題 |
| 14:41 | `bdee321` | hub-google | 縮小 GH token 使用範圍，只留在部署 trigger | 修正上一版 token scope 放置方式 |
| 14:40 | `82eba0f` | fafagoback | serving state/sync 邏輯、crawler workflow 與文件大幅整理 | 導入／整理完整歷史 serving DB 路徑；也帶入最新 30 GB checkpoint 問題所在流程 |
| 15:00 | `449143b` | hub-google | packed publish workflow 改成 stable delta 流程 | 目標是只發布差異，不再近似整庫上傳 |
| 15:00 | `dff2772` | hub-google | publisher 只上傳 changed packed rows | 降低 Turso 寫入量 |
| 15:00 | `00e62e8` | hub-google | 新增已發布 store ID map 匯出 | 為 stable ID 對照提供來源 |
| 15:00 | `2ade8ec` | hub-google | 新增 rekey，跨 rebuild 保留 packed store IDs | 避免 row ID 全部漂移而形成巨量假差異 |
| 16:18 | `c955526` | hub-google | changed rows 改為原位 update | packed publish 成功；16:42 自動切換 production config |
| 16:42 | `4b6b1f6` | github-actions bot | 更新 `web/config.js` 指向成功重建的 Turso DB | 自動部署設定；Pages 成功 |
| 17:02 | `5b9a496` | fafagoback | packed search index、前端查詢一致性、發布腳本、驗證測試與文件整批修正 | Pages 成功；手動 packed publish 因差異仍達 986 MB，被 250 MiB guard 擋下 |

因此如果「改了多少次」是指人工修改：**11 次**。如果連自動 deployment commit 一起算：**13 次**。

## 今天每一次 workflow run

時間皆為台北時間。

| 開始時間 | Run | Workflow | 結果 | 說明 |
|---|---:|---|---|---|
| 06:37 | [35663638985](https://github.com/fafagoback/UberEat/actions/runs/35663638985) | Taiwan pipeline | 失敗 | 發布 Current/Events 到 Turso 時 HTTP 404；目標 DB/URL 不存在或已被切換 |
| 07:24 | [35667447566](https://github.com/fafagoback/UberEat/actions/runs/35667447566) | HF retention | 成功 | retention 完成 |
| 08:08 | [35670646835](https://github.com/fafagoback/UberEat/actions/runs/35670646835) | HF retention | skipped | 上游失敗後依條件跳過，並非執行錯誤 |
| 08:15 | [35671209057](https://github.com/fafagoback/UberEat/actions/runs/35671209057) | Packed Turso | 失敗 | `store_directory: source ended before expected row count`，row index/count 邏輯錯誤 |
| 09:25 | [35675719906](https://github.com/fafagoback/UberEat/actions/runs/35675719906) | Packed Turso | 失敗 | DB 上傳和驗證成功，但 push production config 回傳 exit code 4；實際 bot commit 已產生，屬 trigger/push 收尾問題 |
| 14:40 | [35695888005](https://github.com/fafagoback/UberEat/actions/runs/35695888005) | Deploy Pages | 成功 | 網站部署完成 |
| 14:42 | [35696024819](https://github.com/fafagoback/UberEat/actions/runs/35696024819) | Packed Turso | 失敗 | 重用 run artifact 時找不到任何 shard：expected 15, found 0 |
| 15:01 | [35697539385](https://github.com/fafagoback/UberEat/actions/runs/35697539385) | Packed Turso | 失敗 | estimated delta 1,443,136,692 bytes，超過 262,144,000 guard |
| 16:18 | [35704115588](https://github.com/fafagoback/UberEat/actions/runs/35704115588) | Packed Turso | 成功 | stable ID + changed-row 更新成功 |
| 16:42 | [35706232194](https://github.com/fafagoback/UberEat/actions/runs/35706232194) | Deploy Pages | 成功 | 網站部署完成 |
| 17:03 | [35708253698](https://github.com/fafagoback/UberEat/actions/runs/35708253698) | Deploy Pages | 成功 | 最新 commit 的網站部署完成 |
| 17:04 | [35708276232](https://github.com/fafagoback/UberEat/actions/runs/35708276232) | Packed Turso | 失敗 | packed delta 986,068,214 bytes，仍超過 262,144,000 guard |
| 18:38 | [35717116437](https://github.com/fafagoback/UberEat/actions/runs/35717116437) | Taiwan pipeline | 失敗 | 30 GB `serving.db` 執行 `VACUUM` 時磁碟空間不足 |
| 19:29 | [35721718935](https://github.com/fafagoback/UberEat/actions/runs/35721718935) | Packed Turso | skipped | 上游 Taiwan pipeline 失敗，依條件跳過 |
| 19:29 | [35721718948](https://github.com/fafagoback/UberEat/actions/runs/35721718948) | HF retention | skipped | 上游 Taiwan pipeline 失敗，依條件跳過 |

## 最新一次為什麼失敗

Run [35717116437](https://github.com/fafagoback/UberEat/actions/runs/35717116437) 的實際順序如下：

1. 15 個探索 worker 與後續 menu 工作完成。
2. Stage 6 從 HF 補上 `20260922073632`、`20260922183902` 兩批快照。
3. 同步完成後 `serving.db` 大小為 **30 GB**；runner 顯示磁碟 **145 GB total / 89 GB used / 57 GB available**。
4. DB 驗證成功：`stores=150,513`、`products=8,197,382`。
5. Current/Events Turso 發布因 `TURSO_ACTIVE_SCHEMA_VERSION` 空白而被安全跳過。
6. workflow 接著執行：

   ```sql
   DROP TABLE IF EXISTS product_search;
   PRAGMA wal_checkpoint(TRUNCATE);
   VACUUM;
   ```

7. SQLite `VACUUM` 不是原地壓縮；它會建立接近完整大小的新 DB，再取代舊檔。30 GB 主檔加上暫存 DB、runner 既有用量與 SQLite 工作空間，57 GB 餘量不足，最後報：

   ```text
   sqlite3.OperationalError: database or disk is full
   ```

8. 因這一步是 blocking step，後面的 Pages configure/upload/deploy 全部沒執行。

### 為何這是設計問題，不是偶發問題

目前 checkpoint 把完整 60 天 events 與 current state 放在同一個會持續膨脹的 SQLite 檔，再要求 GitHub-hosted runner 對 30 GB 檔案做全量 `VACUUM`。資料量已跨過 runner 可安全進行雙份重寫的界線；單純 rerun 很可能再次失敗。

## 為什麼原本約 3–4 GB，現在顯示 30 GB

這裡同時發生了「口徑不同」與「資料模型膨脹」兩件事。

### 1. 3–4 GB 是 Actions cache 壓縮後大小，30 GB 是 SQLite 解壓後大小

GitHub API 顯示最近兩份 cache 為：

- `serving-db-rebuilt-20260921185333`：4,258,557,520 bytes
- `serving-db-rebuilt-20260922063749`：4,306,393,976 bytes

這是 Actions cache 壓縮後的儲存／傳輸體積。最新 run 還原後執行 `du -h serving.db`，實際 SQLite 主檔是 30 GB。兩者不是同一口徑，不能直接拿 4 GB 與 30 GB 判斷單批增加了 26 GB。

### 2. `serving.db` 已從 current snapshot 變成歷史 union database

先前的約 3 GB 語意接近「最新狀態」。後來的 full-history rebuild/catch-up 改為保留所有歷史出現過的 store/product identity：

- 最新單批 `20260922183902`：55,184 stores、2,511,975 products。
- 目前 union DB：150,513 stores、8,197,382 products。
- 歷史商品消失後不刪 row，只改成 `inactive`，因此 products 數量只會累積。
- 現在 union products 約為最新單批 products 的 **3.26 倍**。

這個架構轉折主要來自先前的 `ed8caad`（full-history rebuild）與 `3b7e0f9`（catch up every unprocessed snapshot），不是 2026-09-22 當天才突然產生。2026-09-21 的 `51d89f9` 又把它固定為 60-day event retention 與 persistent checkpoint。

### 3. 每個 product 不只存一列原始資料

`products` 本身包含名稱、分類、description、URL、hash、recent prices 等文字欄位，且另有多個 B-tree index：

- primary key `(store_uuid, product_uuid)`
- `first_seen`
- `price`
- `promo_type`
- `product_name`
- `category`
- deals index
- open-status index

8.2 million rows 的文字內容與八組左右的索引，會遠大於原始 JSON 壓縮檔。

### 4. events 重複保存完整狀態 JSON

每次 `NEW`、`CHANGED`、`REMOVED`、`REAPPEARED` 都會寫入 event；`CHANGED` 會保存完整 `old_state` 與 `new_state` JSON，不只是差異欄位。每輪約數十萬事件，60 天累積後會再次複製大量商品名稱、description、URL 與狀態欄位。此外 events 還有三組索引。

### 5. 刪除舊 event 不會自動縮小 SQLite 檔案

`DELETE FROM events WHERE event_time < cutoff` 只把 page 放進 SQLite freelist，不會把檔案還給 filesystem。原本想靠最後的 `VACUUM` 真正縮檔，但 30 GB DB 做 VACUUM 需要另一份接近完整大小的暫存 DB，於是反過來造成最新的 disk-full failure。

### 判定

30 GB 不是單一壞 batch 寫了十倍資料；它是以下設計疊加的結果：

1. 歷史 stores/products 永久 union，不刪 inactive rows。
2. 60 天 event 保存完整 before/after JSON。
3. products/events 建立多組大型索引。
4. SQLite DELETE 不縮檔。
5. Actions cache 顯示壓縮大小，log 顯示解壓大小，先前沒有清楚區分。

這個資料模型不適合繼續作為每輪 GitHub Actions 的單檔 checkpoint。

## 應該怎麼改

### P0：先讓 pipeline 與網站發布解耦

1. 把 Pages deploy 移到 checkpoint/VACUUM 前，或拆成獨立 workflow。靜態網站不應因 30 GB DB 壓縮失敗而無法部署。
2. checkpoint 步驟暫時移除 `VACUUM`。保留 `DROP TABLE`、`commit`、`wal_checkpoint(TRUNCATE)`、`integrity_check` 即可；先確保下一批有可用 checkpoint。
3. 在 checkpoint 前加入容量 guard：估算至少需要 `2 × serving.db + 安全餘量`；不夠就跳過 VACUUM 並寫 warning，不能讓整條 pipeline 失敗。

建議立即改成的核心邏輯：

```python
import os, shutil, sqlite3

path = 'serving.db'
c = sqlite3.connect(path)
c.execute('DROP TABLE IF EXISTS product_search')
c.commit()
c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
c.execute('PRAGMA optimize')
assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
c.close()

db_bytes = os.path.getsize(path)
free_bytes = shutil.disk_usage('.').free
if free_bytes >= db_bytes * 2 + 10 * 1024**3:
    c = sqlite3.connect(path)
    c.execute('VACUUM')
    c.close()
else:
    print(f'Skip VACUUM: db={db_bytes}, free={free_bytes}')
```

### P1：修正資料架構，避免 checkpoint 永久長大

1. 將 Current state 與 60-day Events 拆成兩個 DB；crawler checkpoint 只保存下一批計算真正需要的 current state。
2. events 應放 HF 分區檔／Turso event store，不要每輪把完整 60 天歷史塞回 GitHub Actions cache。
3. 確認 retention 是在 SQLite 寫入前或每批 commit 後真正刪除舊 events，而不只是 HF dry-run。
4. 對 cache 設最大體積與 schema/version key；超過上限改走分片重建，不做單檔 VACUUM。

### P1：修正 packed publish 的 986 MB delta

`5b9a496` 改了 search index 定義與 packed payload。這類 schema/index 定義變更本來就會讓大量 bucket checksum 改變，不應硬套 250 MiB 日常 delta 上限。

應區分兩種發布：

- **日常資料更新**：維持 stable IDs，只更新 changed rows，250 MiB guard 合理。
- **格式／index migration**：建立 versioned 新 DB（例如 `ubereats-packed-v2`），完整上傳、驗證 browser read-only token，成功後一次切換 `web/config.js`；不要對舊 v1 DB 做巨量原位更新。

不要直接把 `TURSO_MAX_DELTA_BYTES` 從 250 MiB 拉到 1 GB 來硬闖。這只會繞過保護，仍可能撞上 Turso quota、交易時間或部分更新風險。

### P1：補上真正的網站 smoke test

每次切換 production config 後，至少自動驗證：

1. Pages 首頁與 `config.js` HTTP 200。
2. 使用公開 read-only token 對新 Turso DB 查 `metadata`。
3. 解壓一個 `store_bundle` 與一個 `search_bucket`。
4. 跑 3 個固定搜尋案例，確認結果非空且 store/product ID 能互相對上。
5. 以上全過才切換 config；失敗就保留上一版已知正常 DB。

## 要讓網頁正常運行，實際操作順序

1. **先保住現況**：不要刪除或覆蓋目前 `web/config.js` 指向的 `ubereats-packed-v1`；它是今天已成功驗證與部署的版本。
2. **修 crawler workflow**：移除無條件 `VACUUM`，把 Pages deploy 與 checkpoint 解耦。
3. **重跑 Taiwan pipeline**：應從既有 cache/HF snapshot 恢復；確認 Stage 6 能完成並產生 checkpoint。
4. **packed v2 完整 migration**：因 search index 格式改變，走新 DB 全量建置，不走 250 MiB delta 路徑。
5. **完成 smoke test 後切 config**：讓 bot commit 新 URL/token，再跑 Deploy GitHub Pages。
6. **瀏覽器驗收**：首頁、縣市篩選、關鍵字搜尋、店家展開、歷史價格各測一次；確認 console 無 Turso 401/404、checksum mismatch 或解壓錯誤。

## 驗收標準

- Taiwan pipeline、Packed Turso、Deploy Pages 三條 workflow 各自綠燈。
- checkpoint 小於設計上限，或至少不再要求在空間不足時 VACUUM。
- Pages 回應 HTTP 200，且畫面可載入非零店家／商品。
- packed metadata 的 `latest_batch` 等於預期最新 HF batch。
- 固定搜尋案例結果與 normalized source 一致。
- 新 DB 驗證失敗時，production config 不會被切換。

---

## 追加事故：Packed plan 因 3 個舊的不完整 Raw archives 失敗（2026-09-23）

### 事故資訊

- Workflow：`Build and Publish Packed Turso`
- Run：[35798330128](https://github.com/fafagoback/UberEat/actions/runs/35798330128)
- 首個明確失敗的 job：[pack-shards (10) / 106982795061](https://github.com/fafagoback/UberEat/actions/runs/35798330128/job/106982795061)
- 事故 commit：`fdf8d83bd7ba644e55cb99c8ddeafbb73e84a664`
- 固定 HF revision：`e368571a03f62d7e667d42e066b9ebebaf3943fd`
- 固定的 Raw 數量：61；最新 batch：`20260923063709`
- Run 開始：2026-09-23 07:37:49（Asia/Taipei）
- Run 結果：`source` 成功；15 個 `pack-shards` 中 shard 10 失敗、其餘 14 個因 `fail-fast` 取消；`merge-and-validate` 未執行。
- 此次是 `plan` 驗證流程，沒有建立／覆寫 Turso production DB，也沒有切換 `web/config.js`。

### 直接錯誤

shard 10 讀完 61 個 Raw 路徑後，`src/rebuild_database.py --strict` 因為 3 個 archive 低於 `minimum_stores=10000` 而拒絕產出 shard：

| Batch | Archive 實際店家數 | 最低要求 | 結果 |
|---|---:|---:|---|
| `20260827150641` | 3,964 | 10,000 | rejected |
| `20260827153508` | 29 | 10,000 | rejected |
| `20260827154155` | 29 | 10,000 | rejected |

最後錯誤為：

```text
RuntimeError: Incomplete source history: 3 archives rejected
```

### 根因

`scripts/pin_hf_source.py` 與 `src/rebuild_database.py::hf_snapshot_paths()` 都只要看到符合
`TaiwanMenuSnapshots/<batch>/taiwan_menus_<batch>.tar.gz` 的檔名，就將 archive 納入完整歷史。它們沒有要求同一目錄必須存在已驗證的 `complete.json`，也沒有驗證 `complete=true`、archive path/bytes/SHA-256。

上述 3 個 2026-08-27 舊 archive 雖然有檔名，但明顯是不完整的採集結果。新 Stage 5 已把 archive 與 `complete.json` 放在同一個 HF commit，但 packed source pin/replay 尚未改成只信任該完成記號。因此 `source` job 錯把 61 個「存在的 tar.gz」當成 61 個「已完成 Raw release」；到 shard 重播 24 分鐘後才由 strict guard 正確擋下。

shard 10 只是最先回報同一個確定性錯誤的 matrix job，不是第 10 分片的資料損壞。每個 shard 都會先驗證整份 archive 的全國店家數，所以相同的 3 個 archive 必然會在 15 個 job 全部被拒絕。

### 當初為什麼會上傳這些壞檔

事故當天的 Git 歷史顯示，掃描點 CSV 曾被錯誤縮減：

- `2305a7d`（2026-08-27 15:20）明確修復 `data/seeds/taiwan_scan_points_3km_land_only.csv`，將檔案從只剩 1 個資料點恢復為 1,558 個掃描點。
- `9291239`（2026-08-27 15:34）又將同一檔案從 1,558 個資料點改回只剩 1 個資料點（Git diff 為 1,558 deletions / 1 insertion）。
- 這會使上游「全台店家探索」實際只掃一個局部區域，因此產生 29 家店的 batch，而不是全台快照。

當時 Stage 5 其實有做「完整性檢查」，但它的比較基準是錯的：

```text
expected = 這一輪上游 reducer 探索到的店家數
pass = 菜單 JSON 數 == expected
```

所以當上游只找到 29 家時，Stage 5 看到的是「29 個預期店家、29 個菜單 JSON」，結果是 29/29 = 100% 而通過。它只證明「菜單工作沒有丟掉這 29 家」，沒有證明「上游真的掃了全台」。

當時缺少的保護包括：

- 掃描點數量/檔案 checksum 必須符合已知正常 baseline。
- 全台店家數必須超過絕對下限，且不能相對上一個健康 batch 暴跌。
- 發現數量暴跌時必須阻擋 HF upload，而不是只檢查下游 JSON 是否齊全。
- HF archive 與「全部跨階段檢查通過」的完成記號必須同一 commit 寫入。當時只要 archive 上傳後遠端看得到檔名，就當成成功。

檔案 SHA-256 與 tar 回讀檢查也有通過，但它們只能證明「29 家的壓縮檔沒有在壓縮或傳輸過程損壞」，不能證明「這是完整的全台資料」。因此這些 archive 是「技術上可讀、業務上不完整」，而不是 tar 檔本身破損。

### 不是根因的警告

- `actions/checkout@v4` / `actions/setup-python@v5` 的 Node.js 20 deprecation 是警告，不是本次 exit code 1 來源。
- runner 磁碟預算檢查已通過，沒有 `database or disk is full`。
- 尚未進入 pack、merge、Turso capacity 或 upload 步驟，所以不是 Turso 250 MiB delta guard 或 4.75 GB packed guard 失敗。

### 正確修法

1. 把 `complete.json` 設為 HF Raw release 的權威清單。`pin_hf_source.py` 只納入同 batch 目錄下同時存在 archive 與 `complete.json` 的項目，並驗證 `complete=true`、`batch_id`、`archive_path`、`archive_bytes`、`sha256`。
2. `source-manifest.json` 要輸出通過上述驗證的精確 archive 清單；15 個 shard 必須使用這份清單，不可各自再列舉 HF 全部 tar.gz。
3. 舊資料需要一次性盤點。對實際完整但比 `complete.json` 機制更早的 archive，離線驗證 store count、manifest count、archive bytes 與 SHA-256 後生成不可變 legacy baseline manifest；上述 3 個不完整 batch 要明確列為 rejected/quarantined，不可偽造 `complete.json`。
4. 在啟動 15 個 matrix job 前，`source` job 先做快速 preflight：每個清單項目的 complete metadata 與 HF LFS bytes/SHA 必須一致；不一致就當場失敗，避免 15 個 runner 各跑約 24 分鐘。
5. 保留 `--strict`、`minimum_stores=10000` 與現有失敗保護。不要把 minimum 降到 29、移除 `--strict` 或把錯誤改成 warning；這會讓不完整快照進入價格事件歷史，製造大量假 `REMOVED` / `REAPPEARED`。
6. 新增測試：無 `complete.json`、`complete=false`、checksum/bytes/path/batch 不一致必須在 source preflight 失敗；已核准 legacy manifest 可重播；rejected batch 不得出現在 pinned archive 清單。

### 修復後的執行與驗收

1. 先只在本地／單一 preflight job 產生 pinned manifest，確認上述 3 個 batch 不在 replay list，並記錄排除理由。
2. 確認 latest batch 仍是 `20260923063709`，source revision 仍是明確的 immutable SHA，且每個納入 archive 的 checksum 可回讀。
3. 重跑 `mode=plan`；驗收 15 個 shard 都上傳、merge 找到正好 15 片、lossless round-trip/checksum/store/product/bucket counts 通過。
4. 記錄 runner RAM/disk、每片與 merged DB 實體 bytes、artifact 大小與 2 天 retention。
5. `plan` 成功只代表建置驗證通過，不得宣稱 production 已修好。`bootstrap` 仍必須另行取得 Turso 實際 plan/已用/剩餘容量，建立 versioned 新 DB，通過固定搜尋與真實瀏覽器 smoke test 後才能切換 config。

### 立即操作結論

不要對相同 commit/HF revision 直接 rerun；錯誤是確定性的，只會再浪費約 15 × 24 分鐘的 runner 時間。先修正權威 source manifest 與 preflight，再跑 `plan`。本次沒有 production 資料庫或 Pages 切換，因此不需要 production rollback。

### 2026-09-23 程式修正記錄

- 正式 crawler 固定使用 repository 內的全台掃描 CSV，移除 workflow 的任意 `scan_file` 輸入。
- Coordinator 在派工前強制至少 1,500 個有效點位與 20 個縣市；1 點 CSV 不會再啟動下游 runner。
- Stage 5 強制全台快照至少 10,000 家店；即使上游與下游都只有 29 家、29/29 也不能上傳 HF。
- 三個既知不完整 batch 加入 checked-in rejection audit。
- 所有 2026-09-23 以後的 Raw 必須有 `complete.json`；否則 manual rebuild 的 source job 直接失敗。
- 每個 rebuild shard 必須讀取 source job 產生的同一份 immutable manifest，不再自行列舉 HF 全部 tar.gz。
- 原 `Build and Publish Packed Turso` 改名為 `Manual Packed Recovery and Bootstrap`，並移除 `workflow_run` crawler 自動觸發。全歷史重建現在只能手動啟動，不再冒充每日增量。
- 驗證：Python 38 tests passed；Node 16 tests passed；workflow YAML 可解析；正式 1,558 點輸入成功產生 15 個不重複分片。
- 沒有發布 Turso、沒有切換 Pages/config，production 仍保持原狀。
