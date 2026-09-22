# 商品與店家判定規則

> 搜尋與發布的強制驗收契約另見 `SYSTEM_REQUIREMENTS.md`。Current 搜尋一律使用正式 ID 去重，並排除 inactive／關閉／無有效價格資料。

## 1 身分識別規則

所有跨批次比較都先依賴穩定 ID。名稱只用於顯示或 fallback，不是正式主要鍵。

### 1.1 店家 ID

正規化 Current/Events 流程使用：

1. 優先取 Raw JSON 的 `store_uuid`。
2. 必須符合 UUID 格式才視為正式店家 ID。
3. 若缺少有效 UUID，取 `@id`；若沒有則取下單 URL。
4. 將 URL 做 SHA-256，取前 32 個十六進位字元，組成 `fallback:store-url:<hash>`。
5. fallback 次數記在 `crawl_batches.fallback_stores`，方便稽核。

### 1.2 商品 ID

正規化流程使用：

1. 優先取菜單商品的 `identifier`。
2. 必須符合 UUID 格式才視為正式商品 ID。
3. 若缺少有效 UUID，使用「店家 ID + 分隔字元 + HTML unescape 後商品名稱」計算 SHA-256。
4. fallback ID 為 `fallback:item-name:<hash>`。
5. fallback 次數記在 `crawl_batches.fallback_products`。

商品資料庫主鍵是 `(store_uuid, product_uuid)`，所以即使兩間店使用相同商品 UUID 或相同名稱，也不會互相覆蓋。

### 1.3 同批次去重

- 同一批次若同一 `store_uuid` 出現多次，只處理第一次。
- 同一批次若同一 `(store_uuid, product_uuid)` 出現多次，只處理第一次。
- 商品名稱為空白時跳過。
- `quantity` 最小為 1。

## 2 價格欄位與實質單價

主要價格欄位：

- `price`：頁面顯示或方案總價。
- `quantity`：促銷一次取得的份數，至少 1。
- `promo_type`：如買一送一等促銷文字；無促銷預設為 `無`。
- `effective_price`：單份實質價格。Raw 若已提供則使用；否則計算 `round(price / quantity, 2)`。

例：商品標價 200 元，買一送一，`quantity=2`，實質單價為 100 元。

## 3 正規化 Current 資料庫的特價判定

這是 `src/serving_state.py` 寫入 `products` 的目前價格異常判定，與靜態 `discounts.json` 的規則不同。

對已存在的商品，每次遇到店家營業中且 `price > 0` 時：

1. 取該商品先前保存的最近三個有效批次 `price`，即 `recent_prices[-3:]`；相同價格可重複，代表價格已連續穩定多批。
2. 必須已累積滿三筆，否則不判為價格優惠。
3. 新 `price` 必須不等於前三筆中的任何一個價格，才設 `price_novel_vs_previous_3=1`。
4. 以前三筆價格的中位數作為 `reference_price`。
5. 只有在新價格低於基準價時才計算：

```text
discount_amount = reference_price - price
discount_pct = discount_amount / reference_price × 100
is_price_deal = 1 if discount_amount > 0 else 0
```

6. 本規則沒有 20% 或 30% 的最低折扣門檻；只要符合新價格且低於中位數，`is_price_deal` 就是 1。
7. 這套判定目前比較的是 `price`，不是 `effective_price`。促銷份數變化另由 `PROMOTION_CHANGED` 記錄。
8. 商品關閉、店家關閉或價格不大於 0 時，不產生特價，且不把該價格加入最近三筆有效價格。
9. 新商品初始化時只建立價格歷史，不立即判為特價。

### 3.1 PRICE_CHANGED 事件

只有同時符合以下條件才記錄 `PRICE_CHANGED`：

- 店家目前營業。
- 新 `price > 0`。
- `price` 或 `effective_price` 與舊狀態不同。
- 新 `price` 不在前三筆價格中，亦即 `price_novel_vs_previous_3=1`。

因此，小幅變動也可成為 `PRICE_CHANGED`；反覆回到前三筆已有價格則不記此事件，以降低價格來回震盪的噪音。

## 4 前端大特價清單的判定

`web/data/discounts.json` 的產生器位於 `src/export_static_snapshots.py`。其跨快照規則為：

1. 從 Hugging Face `Parquet/history/taiwan_catalog_*.parquet` 找出早於本批次的歷史檔。
2. 最多下載最近 7 份歷史 Parquet，不是嚴格的 7 個日曆日。
3. 對同一 `(store_id, product_id)`，從這些歷史資料中取時間最近且營業中的一筆。
4. 比較歷史 `eff_price` 與目前 `eff_price`。
5. 目前與歷史實質單價都必須大於 0，且目前店家必須營業。
6. 產生器預設門檻為降幅至少 20% 且每份至少省 20 元。
7. 若目前 `promo_type='無'` 且降幅大於 80%，視為可能漏打 0 的異常標價，排除。
8. 結果依折扣百分比降冪，再依省下金額降冪排序。

計算式：

```text
savings_amount = previous_effective_price - current_effective_price
discount_pct = savings_amount / previous_effective_price × 100
```

前端 `web/app.js` 對大特價頁籤的預設篩選再收緊為：

- `discount_pct >= 30`
- `savings_amount >= 20`

使用者可透過 UI 調整門檻。頁面也可依類別與關鍵字過濾，並依折扣、省下金額、價格排序。

### 4.1 舊 Alert Engine 規則

`src/alert_engine.py` 是仍保留的舊式 SQLite 快照分析器。它比較最新批次與指定前一批，使用實質單價，預設也是至少降 30% 且省 20 元，並排除「無促銷但降幅超過 80%」。目前主要 crawler workflow 並未呼叫它；不要把其 `alerts_history` 當成 v10 Current/Events 的正式資料表。

## 5 新店家判定

### 5.1 正規化 Current/Events 的正式定義

- 若某個正式或 fallback 店家 ID 從未存在於 `stores`，首次寫入時設定 `first_seen=目前批次時間`。
- 非 baseline 批次會產生 `STORE_NEW` 事件。
- baseline 是整個可用歷史的起點，其中所有店家只建立初始狀態，不產生大量 `STORE_NEW`。
- 店家之後消失又回來，不算新店；會保留原 `first_seen`，並產生 `STORE_REAPPEARED`。

這個定義是「在系統保留並重建得到的歷史範圍內第一次見到」，不等同 Uber 真實開店日期。

### 5.2 靜態新店家 JSON 的定義

靜態產生器將最新 Parquet 與最近一個歷史 Parquet 比較；目前 `store_id` 不在前一份快照中，就列為新店家。這是「相較前一份快照新出現」，不是全歷史首見。

若沒有任何前置歷史 Parquet，產生器會把本批所有店家列為新店，作為基準線。

### 5.3 Packed 前端索引

合併 packed shards 時，系統會以所有 batch 中最新 `processed_at` 往前 7 天作 cutoff，只將 active、營業中、`first_seen >= cutoff` 且店家 `first_seen` 早於商品的資料加入 `f:new` 搜尋索引。店家搜尋使用 `first_seen` 與相同 7 天 cutoff。輸入關鍵字不得取消 new-only 條件。

## 6 新商品判定

### 6.1 正規化 Current/Events 的正式定義

- 若 `(store_uuid, product_uuid)` 從未存在於 `products`，首次寫入時設定 `first_seen=目前批次時間`。
- 非 baseline 批次產生 `NEW` 事件。
- 已存在但曾被標為 inactive 的商品再出現，產生 `REAPPEARED`，不是 `NEW`。
- 商品名稱相同但 UUID 不同會視為不同商品；缺 UUID 時則以同店商品名稱 fallback，因此改名可能被視為新商品。

### 6.2 靜態新商品 JSON 的定義

靜態產生器目前的條件是：

- 店家 ID 必須存在於最近一份歷史 Parquet，也就是老店。
- 商品 ID 不存在於最近一份歷史 Parquet。
- 現在 `price >= 1`。

因此它是「老店相較前一批新出現的商品」，不是全歷史首見。曾經出現、前一批消失、這一批恢復的商品，若只看前一批，也可能被列進靜態新品。

## 7 促銷判定

Current 狀態保存 Raw 的 `promo_type` 與 `quantity`。若兩者任一改變，產生 `PROMOTION_CHANGED` 事件。

靜態及 packed 促銷搜尋的共同條件為：

```text
(promo_type != '無' OR quantity > 1) AND price >= 1
```

促銷清單包含買一送一、買二送一及其他已解析出的方案。`quantity > 1` 可涵蓋促銷標籤缺失但份數已辨識的資料。

## 8 下架與重新出現

預設 `MISSING_STREAK_THRESHOLD=3`。

- 商品在一次快照未出現：`missing_streak + 1`，未滿 3 時仍維持 active。
- 商品連續第三次未出現：改為 inactive，產生 `REMOVED`。
- 若店家在該批次明確為關閉，跳過其商品缺失累計，避免把關店期間的空菜單誤判成商品下架。
- 店家連續三批未出現：改為 inactive，產生 `STORE_REMOVED`。
- inactive 商品重新出現：恢復 active，清零 streak，產生 `REAPPEARED`。
- inactive 店家重新出現：產生 `STORE_REAPPEARED`。
- 同一批之後持續缺失，不會每批重複產生 removed 事件；只有從 active 轉為 inactive 時產生一次。

## 9 其他變更事件

- `STORE_CHANGED`：店名、地址、城市、行政區、座標、評分、評論數、下單 URL 或營業狀態的 canonical state hash 改變。
- `CONTENT_CHANGED`：商品名稱、分類、描述或下單 URL 改變。
- `PROMOTION_CHANGED`：`promo_type` 或 `quantity` 改變。
- 一次商品狀態更新可能同時產生多種事件。
- `old_state` 與 `new_state` 以 JSON 文字保存，供稽核與歷史查詢。

