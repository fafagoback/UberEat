# -*- coding: utf-8 -*-
"""
Uber Eats 全台店家菜單分散式採集工作節點 (Stage 4: Parallel Matrix Menu Worker)
【核心功能】：
1. 讀取分配給本節點的菜單分片任務 (menu_tasks/chunk_X.json)。
2. 調用 location_batch_scraper.py 原生 RPC API (getStoreV1) 高速擷取完整菜單資料。
3. 輸出符合 Schema.org Restaurant JSON-LD 規範之時序檔案：
   - 檔名規範：{crawled_time}_{store_id_8碼}_{safe_name}.json (與 location_batch_scraper.py 格式 100% 一致)
4. 節點內自動執行實體 JSON 完整性與欄位檢核。
5. 僅將成果交給 GitHub Actions Artifact；Hugging Face 備份由 Stage 5 統一處理。
6. 產出詳細檢核報表至 GitHub Actions $GITHUB_STEP_SUMMARY。
"""

import os
import sys
import glob
import json
import time
import argparse
from datetime import datetime, timezone, timedelta

TW_TZ = timezone(timedelta(hours=8))

# 確保標準輸出與標準錯誤支援 UTF-8
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

# 引入專案模組
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from location_batch_scraper import crawl_stores_with_watchdog, get_md5_hash
from snapshot_validation import validate_document, validate_snapshot


def append_github_step_summary(markdown_text: str):
    """將 Markdown 內容寫入 GitHub Actions $GITHUB_STEP_SUMMARY"""
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        try:
            with open(summary_file, "a", encoding="utf-8") as f:
                f.write(markdown_text + "\n")
        except Exception as e:
            print(f"⚠️ 寫入 GITHUB_STEP_SUMMARY 失敗: {e}")
    else:
        print(f"\n[Local Menu Worker Summary]\n{markdown_text}\n")


def fatal_error(chunk_id: int, step_name: str, reason: str, expected: str = "", actual: str = "", retries: int = 3):
    """輸出醒目錯誤橫幅、寫入 GITHUB_STEP_SUMMARY 並強制以 exit code 1 終止"""
    msg = f"""
================================================================================
❌ 【階段 4: Menu Worker {chunk_id} 菜單採集檢核失敗 (FATAL ERROR)】
步驟名稱: {step_name}
重試次數: 已重試 {retries} 輪均未達標
錯誤原因: {reason}
預期成果: {expected}
實際結果: {actual}
================================================================================
"""
    print(msg, file=sys.stderr, flush=True)
    
    summary_md = f"""
### ❌ 【Menu Worker {chunk_id} 採集失敗】
> [!CAUTION]
> **Menu Worker {chunk_id} 在「{step_name}」經 {retries} 輪補爬後仍未產出有效成果，節點已報錯終止 (Exit Code 1)！**
> - **錯誤原因**: `{reason}`
> - **預期成果**: `{expected}`
> - **實際結果**: `{actual}`
"""
    append_github_step_summary(summary_md)
    sys.exit(1)


def validate_schema_json(file_path: str) -> tuple:
    """驗證產出的 Schema.org Restaurant JSON-LD 完整性"""
    if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        return False, "檔案不存在或為 0 位元組"
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return False, "JSON 根節點不是物件"
        validate_document(data)
        return True, "OK"
    except Exception as e:
        return False, f"JSON 解析異常: {e}"


def main():
    parser = argparse.ArgumentParser(description="Uber Eats 全台店家菜單分散式工作節點 (Stage 4 Menu Worker)")
    parser.add_argument("--chunk-file", required=True, help="分片任務檔案路徑 (menu_tasks/chunk_X.json)")
    parser.add_argument("--output-dir", required=True, help="菜單 JSON 結果輸出目錄")
    parser.add_argument("--concurrency", type=int, default=5, help="內部並發執行緒數 (預設 5)")
    args = parser.parse_args()

    start_time = time.time()
    now_dt = datetime.now(TW_TZ)
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")

    # 1. 讀取並檢驗菜單分片檔案
    if not os.path.exists(args.chunk_file):
        print(f"❌ 找不到菜單分片任務檔案: {args.chunk_file}", file=sys.stderr)
        fatal_error(
            chunk_id=-1,
            step_name="步驟 4.1 菜單分片檔案讀取",
            reason=f"檔案不存在: {args.chunk_file}",
            expected="檔案存在且可讀",
            actual="檔案不存在",
            retries=0
        )

    with open(args.chunk_file, "r", encoding="utf-8") as f:
        task_data = json.load(f)

    chunk_id = task_data.get("chunk_id", 0)
    batch_id = task_data.get("batch_id") or task_data.get("crawled_time") or now_dt.strftime("%Y%m%d%H%M%S")
    stores = task_data.get("stores", [])
    total_assigned = len(stores)

    print("=" * 80)
    print(f"🚀 【Stage 4: Menu Worker {chunk_id} 啟動】全台菜單深度採集")
    print(f"⏰ 執行時間: {now_str} (Batch: {batch_id})")
    print(f"🏪 本節點分配店家總數: {total_assigned} 間")
    print(f"⚙️ 內部並發執行緒: {args.concurrency} | 輸出目錄: {args.output_dir}")
    print("📦 本階段只產出菜單 JSON；由 GitHub Artifact 傳遞給 Stage 5 統一備份")
    print("=" * 80)

    if total_assigned == 0:
        fatal_error(
            chunk_id=chunk_id,
            step_name="步驟 4.1 任務店家分配檢核",
            reason="本節點分片未分配任何店家",
            expected="店家總數 > 0",
            actual="0 間店家",
            retries=0
        )

    os.makedirs(args.output_dir, exist_ok=True)
    file_time_prefix = f"{batch_id}_"

    # 2. 啟動看門狗多線程高速抓取 (使用原生 RPC getStoreV1，雙向轉化為 Schema.org JSON)
    print(f"\n⚡ 【步驟 4.2】啟動看門狗原生 RPC 菜單高速採集 ({args.concurrency} 執行緒)...")
    results = crawl_stores_with_watchdog(
        stores_to_crawl=stores,
        output_dir=args.output_dir,
        time_prefix=file_time_prefix,
        workers=args.concurrency
    )

    success_count = sum(1 for r in results if r["status"] == "SUCCESS")
    fail_count = total_assigned - success_count
    total_menu_items = sum(r.get("total_items", 0) for r in results if r["status"] == "SUCCESS")

    # 3. 實體產出檢核
    print(f"\n🔍 【步驟 4.3】執行產出 Schema.org JSON 檔案實體檢核...")
    json_files = glob.glob(os.path.join(args.output_dir, "*.json"))
    validate_snapshot(args.output_dir, stores, batch_id)
    valid_json_count = 0
    invalid_files = []

    for jf in json_files:
        is_ok, msg = validate_schema_json(jf)
        if is_ok:
            valid_json_count += 1
        else:
            invalid_files.append(f"{os.path.basename(jf)}: {msg}")

    print(f"   ├─ 實體 JSON 檔案數: {len(json_files)} 個")
    print(f"   ├─ 格式檢核通過數:   {valid_json_count} 個")
    if invalid_files:
        print(f"   └─ ⚠️ 格式異常檔案: {len(invalid_files)} 個 ({', '.join(invalid_files[:3])})")

    if success_count == 0 or valid_json_count == 0:
        fatal_error(
            chunk_id=chunk_id,
            step_name="步驟 4.3 菜單產出檢核",
            reason="本節點採集成功數為 0 或未產出任何有效 JSON 檔案",
            expected="成功店家 > 0 且有效 JSON > 0",
            actual=f"成功店家: {success_count}, 有效 JSON: {valid_json_count}",
            retries=3
        )

    crawl_elapsed = time.time() - start_time
    success_rate = (success_count / total_assigned) * 100.0

    print(f"✅ [步驟 4.3 通過] Menu Worker {chunk_id} 採集完成！成功率: {success_rate:.1f}% ({success_count}/{total_assigned})，擷取商品: {total_menu_items:,} 道")

    total_elapsed = time.time() - start_time

    # 5. 輸出 GITHUB_STEP_SUMMARY
    summary_md = f"""### 🍽️ 【Menu Worker {chunk_id} 菜單採集報告】
- **分配店家**: `{total_assigned:,}` 間 | **成功採集**: **`{success_count:,}`** 間 ({success_rate:.1f}%) | **失敗**: `{fail_count}` 間
- **擷取商品總數**: **`{total_menu_items:,}`** 道菜品 | **產出有效 JSON**: `{valid_json_count:,}` 個
- **採集耗時**: `{crawl_elapsed:.1f}` 秒 (平均 `{crawl_elapsed/max(1, total_assigned):.2f}` 秒/店) | **總耗時**: `{total_elapsed:.1f}` 秒
- **下游傳遞方式**: `GitHub Actions Artifact（HF 由 Stage 5 統一單次 Commit）`

| Checkpoint | 預期 | 實際 | 狀態 |
| :--- | :--- | :--- | :---: |
| 店家處理完成 | `{total_assigned}` 間 | 成功 `{success_count}` / 失敗 `{fail_count}` | {'✅' if success_count == total_assigned else '❌'} |
| Schema JSON | `{total_assigned}` 個有效檔案 | `{valid_json_count}` 個有效 / `{len(invalid_files)}` 個無效 | {'✅' if valid_json_count == total_assigned and not invalid_files else '❌'} |
| Stage 4 職責邊界 | 不直接呼叫 HF | 僅產出本 Worker JSON | ✅ |
"""
    append_github_step_summary(summary_md)

    if success_count != total_assigned or valid_json_count != total_assigned or invalid_files:
        fatal_error(
            chunk_id=chunk_id,
            step_name="步驟 4.3 菜單全量產出 Checkpoint",
            reason=f"成功 {success_count}/{total_assigned}，有效 JSON {valid_json_count}/{total_assigned}，無效檔 {len(invalid_files)}",
            expected="每個指派店家皆產出一個有效 Schema.org JSON",
            actual=f"缺少 {total_assigned - valid_json_count} 個有效結果",
            retries=2
        )

    print("\n" + "=" * 80)
    print(f"🎉 【Menu Worker {chunk_id} 任務全數完成！】成果已存入: {args.output_dir}")
    print("=" * 80)


if __name__ == "__main__":
    main()
