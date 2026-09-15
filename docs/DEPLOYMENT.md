# Deployment and secrets

## GitHub Actions

Crawler 頻率、worker 數量與 Raw 上傳均未變。Stage 6 改為建立並驗證 normalized SQLite，不再產生完整歷史 Parquet，也不再 commit `web/data`。

設定：

- `HF_TOKEN`, `HF_REPO_ID`：Raw upload 與 retention。
- `MISSING_STREAK_THRESHOLD` repository variable：預設 3。
- `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`：Turso。未設定時 crawler/Raw 不失敗，只做 SQLite validation。

## Safe migration

1. 在 GitHub Actions 執行 `Rebuild Turso From All HF Snapshots`，先保持 `publish=false`。
2. workflow 依時間重播 HF 全部可用快照；比對 batches、stores、products、events 與 fallback count，第一批 baseline 的 NEW/STORE_NEW 必須為 0。
3. 建立隔離的新 Turso database，設定 `TURSO_REBUILD_DATABASE_URL` / `TURSO_REBUILD_AUTH_TOKEN`，再以 `publish=true` 重跑。禁止由使用者本機寫入 production Turso。
4. 實測 COSTCO、麥當勞、雞排、牛肉、咖啡，以及新品/新店/歷史 API。
5. GitHub Pages 不得嵌入 Turso token；若明確拒絕任何 API proxy，維持靜態 fallback，任意全庫查詢只能在可信任後端或 Actions 中執行。
6. 觀察至少一個下一批 incremental update，再依 `docs/STORAGE_ARCHITECTURE.md` 順序清 legacy。

目前 CI 對 Turso 寫入保持 gate，避免在尚未提供 credentials、尚未匯入上一版 current 時把每批誤當 baseline。完成首次 baseline 驗證後才應啟用 remote incremental publish。
