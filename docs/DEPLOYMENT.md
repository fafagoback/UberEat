# Deployment and secrets

## GitHub Actions

Crawler 頻率、worker 數量與 Raw 上傳均未變。Stage 6 改為建立並驗證 normalized SQLite，不再產生完整歷史 Parquet，也不再 commit `web/data`。

設定：

- `HF_TOKEN`, `HF_REPO_ID`：Raw upload 與 retention。
- `MISSING_STREAK_THRESHOLD` repository variable：預設 3。
- `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`：Turso。未設定時 crawler/Raw 不失敗，只做 SQLite validation。
- Worker secrets：`TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`；variable `ALLOWED_ORIGIN`。

## Safe migration

1. 用最新完整 Raw 執行 `python src/serving_state.py --source <dir-or-tar> --database serving.db --batch-id <timestamp> --baseline`。
2. 比對 Raw、stores、products 數量與 fallback count；baseline events 必須為 0。
3. 將 SQLite baseline 匯入新 Turso database，部署 `worker/`。
4. 實測 COSTCO、麥當勞、雞排、牛肉、咖啡，以及新品/新店/歷史 API。
5. 把 `web/config.js` 的 `WORKER_API_BASE_URL` 改成實際 Worker URL並部署 Pages。
6. 觀察至少一個下一批 incremental update，再依 `docs/STORAGE_ARCHITECTURE.md` 順序清 legacy。

目前 CI 對 Turso 寫入保持 gate，避免在尚未提供 credentials、尚未匯入上一版 current 時把每批誤當 baseline。完成首次 baseline 驗證後才應啟用 remote incremental publish。
