# UberEat v2 儲存與全台搜尋架構

## 目標

1. **爬蟲照舊完整掃描**：不降低全台掃描頻率、店家覆蓋率或菜單完整性。
2. **不再每天重複保存相同商業狀態**：價格、名稱、促銷、上下架未改變時，不新增一份歷史資料。
3. **每日爬蟲原始封存保留**：`TaiwanMenuSnapshots/**` 仍作為完整稽核/災難復原來源。
4. **任意關鍵字查詢全台真實資料**：搜尋不依賴熱門商品清單，也不掃描/下載整個全台 Parquet。
5. **使用者只下載必要小分片**：搜尋索引與 current data 均以 256 個 hash shard 發布。

## Hugging Face 目錄

```text
TaiwanMenuSnapshots/              # 保留：每日完整爬蟲封存
TaiwanStores/                     # 保留：店家探索來源

v2/
  current/
    manifest.json
    shards/
      00.json.gz ... ff.json.gz   # 最新完整商品狀態
  search/
    manifest.json
    shards/
      00.json.gz ... ff.json.gz   # NFKC/lower CJK+Latin n-gram 倒排索引
  history/
    events/
      <batch>.parquet             # 只有真正新增/修改/移除的商品事件
  scans/
    <batch>.json                   # 每次完整掃描的證據與變更統計
```

## 去重規則

每個商品狀態都產生穩定 hash。歷史 hash 只包含商品狀態：

- 商品名稱
- 分類
- 描述
- 價格
- 數量/促銷
- 實質單價
- 上下架狀態

**店家評分、地址等店家層資訊不納入商品 history hash**，避免店家評分改一次就把整間店幾千個商品誤記為商品變更。

### 第一次遷移

第一次建立 v2 是 baseline：

- 建立完整 current
- 建立完整 search index
- 建立 scan manifest
- **不把全部商品再寫一份 history**

之後每批才寫真正的 `added / updated / removed` 事件。

## 搜尋流程

例如使用者輸入 `costco`：

```text
costco
  -> NFKC + lowercase
  -> cos / ost / stc / tco
  -> 各 token SHA1 前兩碼定位 search shard
  -> posting lists 交集
  -> 得到候選 doc IDs
  -> doc ID 前兩碼定位 current shards
  -> 只下載命中的 current shards
  -> 對完整 search_text 做 exact substring verification
  -> 城市篩選 / 排序 / 分頁
```

因此查詢不會下載 `Parquet/taiwan_catalog_latest.parquet`，也不會只查 20,000 筆熱門/精選商品。

索引的來源是該次 **完整全台 current catalog**。

## 舊格式清理安全規則

`src/cleanup_hf_legacy.py` 只允許刪除明確 allow-list 的淘汰 serving artifact，而且必須先確認以下檔案已存在：

- `v2/current/manifest.json`
- `v2/search/manifest.json`

永久保護：

- `TaiwanMenuSnapshots/**`
- `TaiwanStores/**`
- `v2/**`

遷移期暫時保護：

- `Parquet/history/**`：舊版特價/新品情報引擎目前仍讀這裡，待情報引擎改讀 v2 events 後再移除。

## GitHub Pages

`web/v2_search.js` 是全庫關鍵字搜尋的 serving layer。

`web/config.js` 已將 `ENABLE_DUCKDB` 設為 `false`，避免瀏覽器啟動 DuckDB-WASM 並碰大型 Parquet。

舊靜態 JSON 暫時保留作 dashboard 與 rollback fallback；待 v2 搜尋與 v2 history 穩定後，再停止 GitHub Actions 每批把大型 JSON commit 回 Git repository。
