# -*- coding: utf-8 -*-
"""
Uber Eats 分散式採集工作節點 (Stage 2: Parallel Matrix Worker)
【檢核與重試機制】：
1. 嚴格檢核分片任務輸入完整性
2. 看門狗並發採集 + 3 輪失敗自動補爬隊列
3. 嚴格驗證產出的 Schema.org JSON 檔案大小與欄位完整性
4. 若成功數為 0 或未產出有效檔案，立即報錯中斷 (sys.exit(1))
5. 輸出該 Worker 檢核報告至 GitHub Actions $GITHUB_STEP_SUMMARY
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

# 引入本地模組目錄
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "local_scr")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from location_batch_scraper import crawl_stores_with_watchdog


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
        print(f"\n[Local Step Summary]\n{markdown_text}\n")


def fatal_error(chunk_id: int, step_name: str, reason: str, expected: str = "", actual: str = "", retries: int = 3):
    """輸出醒目錯誤橫幅、寫入 GITHUB_STEP_SUMMARY 並強制以 exit code 1 終止"""
    msg = f"""
================================================================================
❌ 【階段 2: Worker {chunk_id} 採集檢核失敗 (FATAL ERROR)】
步驟名稱: {step_name}
重試次數: 已重試 {retries} 輪均未達標
錯誤原因: {reason}
預期成果: {expected}
實際結果: {actual}
================================================================================
"""
    print(msg, file=sys.stderr, flush=True)
    
    summary_md = f"""
### ❌ 【Worker {chunk_id} 採集失敗】
> [!CAUTION]
> **Worker {chunk_id} 在「{step_name}」經 {retries} 輪補爬後仍未產出有效成果，節點已報錯終止 (Exit Code 1)！**
> - **錯誤原因**: `{reason}`
> - **預期成果**: `{expected}`
> - **實際結果**: `{actual}`
"""
    append_github_step_summary(summary_md)
    sys.exit(1)


def validate_json_file(file_path: str) -> tuple:
    """驗證單一產出的 Schema.org JSON 檔案完整性"""
    if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        return False, "檔案不存在或為 0 位元組"
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return False, "JSON 根節點不是物件"
        if not data.get("name"):
            return False, "缺少店家名稱 (name)"
        return True, "OK"
    except Exception as e:
        return False, f"JSON 解析異常: {e}"


def main():
    parser = argparse.ArgumentParser(description="Uber Eats 工作節點爬蟲 (嚴格檢核版)")
    parser.add_argument("--chunk-file", required=True, help="分片任務檔案路徑 (tasks/chunk_X.json)")
    parser.add_argument("--output-dir", required=True, help="JSON 結果輸出目錄")
    args = parser.parse_args()

    start_time = time.time()

    # ---------------------------------------------------------
    # 步驟 2.1：分片任務檔案讀取與輸入檢核
    # ---------------------------------------------------------
    if not os.path.exists(args.chunk_file):
        print(f"❌ 找不到分片檔案: {args.chunk_file}", file=sys.stderr)
        fatal_error(
            chunk_id=-1,
            step_name="步驟 2.1 分片任務檔案讀取",
            reason=f"分片任務檔案遺失: {args.chunk_file}",
            expected="檔案存在且可讀",
            actual="檔案不存在",
            retries=0
        )

    try:
        with open(args.chunk_file, "r", encoding="utf-8") as f:
            task_data = json.load(f)
    except Exception as e:
        fatal_error(
            chunk_id=-1,
            step_name="步驟 2.1 分片任務解析",
            reason=f"分片檔案 JSON 格式損毀: {e}",
            expected="合法 JSON 內容",
            actual="JSON 解析錯誤",
            retries=0
        )

    chunk_id = task_data.get("chunk_id", 0)
    stores = task_data.get("stores", [])
    total_assigned = len(stores)

    print("=" * 80)
    print(f"🚀 【階段 2: Worker {chunk_id} 啟動】(嚴格檢核與看門狗補爬模式)")
    print(f"📊 本節點分配店家數: {total_assigned} 間")
    print(f"📁 輸出目錄: {args.output_dir}")
    print("=" * 80)

    if total_assigned == 0:
        fatal_error(
            chunk_id=chunk_id,
            step_name="步驟 2.1 任務店家檢核",
            reason="分片內未分配任何店家",
            expected="店家數 > 0",
            actual="店家數 0 間",
            retries=0
        )

    crawled_time = task_data.get("crawled_time") or datetime.now(TW_TZ).strftime("%Y%m%d%H%M%S")

    os.makedirs(args.output_dir, exist_ok=True)
    file_time_prefix = f"{crawled_time}_"

    # ---------------------------------------------------------
    # 步驟 2.2：看門狗並發採集 (內建多輪補爬)
    # ---------------------------------------------------------
    print(f"\n⚡ 【步驟 2.2】啟動看門狗高速採集 (4 執行緒)...")
    results = crawl_stores_with_watchdog(
        stores_to_crawl=stores,
        output_dir=args.output_dir,
        time_prefix=file_time_prefix,
        workers=4
    )

    success_count = sum(1 for r in results if r["status"] == "SUCCESS")
    fail_count = total_assigned - success_count
    total_menu_items = sum(r.get("total_items", 0) for r in results if r["status"] == "SUCCESS")

    # ---------------------------------------------------------
    # 步驟 2.3：產出 JSON 檔案完整性與 Schema 嚴格檢核
    # ---------------------------------------------------------
    print(f"\n🔍 【步驟 2.3】執行產出 JSON 檔案實體檢核...")
    json_files = glob.glob(os.path.join(args.output_dir, "*.json"))
    valid_json_count = 0
    invalid_files = []

    for jf in json_files:
        is_ok, msg = validate_json_file(jf)
        if is_ok:
            valid_json_count += 1
        else:
            invalid_files.append(f"{os.path.basename(jf)}: {msg}")

    print(f"   ├─ 掃描 JSON 檔案數: {len(json_files)} 個")
    print(f"   ├─ 格式檢核通過數:   {valid_json_count} 個")
    if invalid_files:
        print(f"   └─ ⚠️ 格式異常檔案: {len(invalid_files)} 個 ({', '.join(invalid_files[:3])})")

    # 熔斷檢核判定: 成功店家數必須 > 0 且產出的有效 JSON 檔案數必須 > 0
    if success_count == 0 or valid_json_count == 0:
        fatal_error(
            chunk_id=chunk_id,
            step_name="步驟 2.3 採集產出檢核",
            reason=f"本節點經多輪嘗試後，採集成功數為 0 或有效 JSON 產出為 0",
            expected=f"成功店家 > 0 且有效 JSON > 0",
            actual=f"成功店家: {success_count} 間, 有效 JSON: {valid_json_count} 個",
            retries=3
        )

    elapsed = time.time() - start_time
    success_rate = (success_count / total_assigned) * 100.0

    print(f"✅ [步驟 2.3 通過] Worker {chunk_id} 檢核成功！有效 JSON: {valid_json_count} 個 (成功率: {success_rate:.1f}%)")

    # ---------------------------------------------------------
    # 步驟 2.4：輸出 Worker Step Summary
    # ---------------------------------------------------------
    summary_md = f"""
### 💻 Worker {chunk_id} 採集檢核報告
- **分配店家**: `{total_assigned}` 間 | **成功採集**: `{success_count}` 間 ({success_rate:.1f}%) | **失敗**: `{fail_count}` 間
- **擷取商品總數**: `{total_menu_items}` 項 | **產出有效 JSON**: `{valid_json_count}` 個 | **耗時**: `{elapsed:.1f} 秒`
- **檢核狀態**: ✅ 檢核通過
"""
    append_github_step_summary(summary_md)

    print("\n" + "=" * 80)
    print(f"🎉 【Worker {chunk_id} 任務完成！】全數通過檢核，產出已存入: {args.output_dir}")
    print("=" * 80)


if __name__ == "__main__":
    main()
