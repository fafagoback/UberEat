# 網站搜尋與資料正確性需求

## 1. 目的

本文件是網站搜尋、情報頁籤、資料發布與歷史保存的驗收契約。程式、工作流程與其他文件若有衝突，以本文件及 `BUSINESS_RULES.md` 為準。

## 2. Current 搜尋集合

- 店家搜尋只回傳 `status='active'` 且 `is_open=1` 的店家。
- 商品搜尋只回傳商品與所屬店家皆 active、商品 `is_open=1`、有效單價大於 0 的資料。
- 歷史／inactive 資料必須保留在 normalized bundle 與 events，但不得進入 current 倒排索引。
- 店家唯一鍵是 `store_uuid`；商品唯一鍵是 `(store_uuid, product_uuid)`。名稱、地址及近似座標不得用於正式去重。
- 所有搜尋排序必須具有穩定 tie-breaker；分頁不得重複或遺漏。
- 顯示的結果總數不得以全庫統計或預載樣本數冒充。

## 3. 功能定義

### 3.1 新店

新店是目前 active，且 `first_seen` 位於最新發布時間往前 7 天內的店家。輸入關鍵字、套用縣市或距離篩選不得改變此定義。

### 3.2 老店新菜

Packed `f:new` 索引代表「商品最近 7 天首次出現，且店家早於商品存在」的 active 商品。新店第一批整份菜單不進入此索引；重新出現的商品也不得算 NEW。

### 3.3 促銷

符合任一條件即為促銷：

- `promo_type` 不為空且不為 `無`；
- `quantity > 1`。

搜尋前後、全品庫的只看促銷及促銷頁籤必須使用同一判定。

### 3.4 大特價

大特價是歷史有效單價下降，不等同多入促銷的單份折算。預設展示門檻為降幅至少 30% 且省至少 20 元；後端最低收錄門檻為 20% 且省 20 元。`discount_pct` 與 `discount_amount` 必須來自相同歷史基準，不得以 `price-effective_price` 替代。

## 4. 搜尋文字規則

- 使用 Unicode NFKC 與不分大小寫正規化。
- 空白分隔的多個詞採 AND；每個詞可在商品名、店名或分類中命中。
- 品牌同義詞應在取得遠端候選集合前展開，不能只在候選取得後處理。
- 索引候選取得後仍須以原始欄位精確確認，避免 n-gram 假陽性。

## 5. 發布一致性

每次 production release 必須具有同一份 manifest，至少包含：

- `release_id`
- `latest_batch`
- `latest_processed_at`
- `schema_version`
- `definition_version`
- active store/product counts
- packed 與靜態摘要所使用的來源批次

Packed、靜態 JSON 與 Pages 必須來自同一批次。批次不一致時 CI 必須失敗，不得靜默部署。資料庫驗證成功後才能切換 `web/config.js`。

## 6. 歷史保存

- GitHub Actions cache 不是永久備份。
- 60 天 Raw 保留不足以重建永久 `first_seen` 與完整 events。
- 系統必須永久保存 identity registry、Current checkpoint 與 append-only events archive；Raw 可依成本採有限期保留。
- 重建不得改寫既有 `first_seen`，同一 snapshot 重播不得產生重複事件。

## 7. 必要驗收

- inactive 店家／商品無法由任何 current 搜尋找到。
- 第 101 間以後的店家商品可透過索引搜尋。
- 同名分店與同名商品不會合併。
- 新店、新品、促銷、大特價在輸入關鍵字前後維持相同定義。
- 不同店家的相同商品 ID 不會共享新品判定或價格歷史。
- 每個 release 可由 metadata 得知真實批次，不依賴舊 `stats.json` 猜測。
- Packed reference 超過 20-bit 商品 index 容量時建置必須失敗。
