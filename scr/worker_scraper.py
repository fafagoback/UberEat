# -*- coding: utf-8 -*-
"""
Uber Eats 分散式採集工作節點 (Stage 2: Parallel Matrix Worker)
讀取單一分片 JSON (tasks/chunk_X.json)，利用獨立公網 IP 進行平行採集。
"""

import os
import sys
import json
import argparse
from datetime import datetime

# 引入本地模組目錄
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "local_scr")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from location_batch_scraper import crawl_stores_with_watchdog


def main():
    parser = argparse.ArgumentParser(description="Uber Eats 工作節點爬蟲")
    parser.add_argument("--chunk-file", required=True, help="分片任務檔案路徑 (tasks/chunk_X.json)")
    parser.add_argument("--output-dir", required=True, help="JSON 結果輸出目錄")
    args = parser.parse_args()

    if not os.path.exists(args.chunk_file):
        print(f"❌ 找不到分片檔案: {args.chunk_file}")
        sys.exit(1)

    with open(args.chunk_file, "r", encoding="utf-8") as f:
        task_data = json.load(f)

    chunk_id = task_data.get("chunk_id", 0)
    stores = task_data.get("stores", [])
    total_assigned = len(stores)

    print("=" * 80)
    print(f"🚀 【階段 2: Worker {chunk_id} 啟動】")
    print(f"📊 本節點分配店家數: {total_assigned} 間")
    print(f"📁 輸出目錄: {args.output_dir}")
    print("=" * 80)

    if total_assigned == 0:
        print("⚠️ 無分配店家，提早退出。")
        sys.exit(0)

    os.makedirs(args.output_dir, exist_ok=True)

    file_time_prefix = datetime.now().strftime("%Y%m%d%H%M%S_") + f"w{chunk_id}_"

    # 執行看門狗並發採集 (每台工作機 4 線程)
    results = crawl_stores_with_watchdog(
        stores_to_crawl=stores,
        output_dir=args.output_dir,
        time_prefix=file_time_prefix,
        workers=4
    )

    success_count = sum(1 for r in results if r["status"] == "SUCCESS")
    fail_count = total_assigned - success_count
    
    print("\n" + "=" * 80)
    print(f"🎉 【Worker {chunk_id} 任務完成！】")
    print(f"📈 成功: {success_count} / 失敗: {fail_count} (成功率: {success_count/total_assigned*100:.1f}%)")
    print("=" * 80)


if __name__ == "__main__":
    main()
