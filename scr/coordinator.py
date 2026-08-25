# -*- coding: utf-8 -*-
"""
Uber Eats 分散式採集主調度器 (Stage 1: Coordinator)
負責探索周邊店家，依據 min(15, total_stores) 策略動態切分分片，並輸出 GHA Matrix。
"""

import os
import sys
import json
import argparse

# 引入本地模組目錄
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "local_scr")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from location_batch_scraper import geocode_address, discover_nearby_stores

MAX_WORKERS_LIMIT = 15


def set_github_output(name: str, value: str):
    """將變數寫入 GitHub Actions $GITHUB_OUTPUT 供下游 Job 讀取"""
    github_output_path = os.environ.get("GITHUB_OUTPUT")
    if github_output_path:
        with open(github_output_path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    else:
        print(f"[Local Dry Run Output] {name}={value}")


def main():
    parser = argparse.ArgumentParser(description="Uber Eats 分散式爬蟲調度器")
    parser.add_argument("--address", default="台北市士林區中山北路七段", help="基準地址")
    parser.add_argument("--max-workers", type=int, default=MAX_WORKERS_LIMIT, help="最大工作機數量 (預設 15)")
    args = parser.parse_args()

    print("=" * 80)
    print("🚀 【階段 1: Coordinator 調度節點】啟動")
    print(f"📍 目標地址: {args.address}")
    print(f"⚙️ 最大工作機台數上限: {args.max_workers}")
    print("=" * 80)

    # 1. 座標定位與店家探索
    lat, lon, geo_src = geocode_address(args.address)
    print(f"🧭 定位座標: 緯度 {lat:.7f}, 經度 {lon:.7f} ({geo_src})")
    
    stores = discover_nearby_stores(lat, lon, args.address)
    total_stores = len(stores)
    print(f"✅ 探索成功！周邊共發現 {total_stores} 間店家。")

    if total_stores == 0:
        print("⚠️ 未發現任何店家，流程結束。")
        set_github_output("matrix", json.dumps({"include": []}))
        set_github_output("has_tasks", "false")
        return

    # 2. 計算動態工作機台數: min(15, total_stores)
    num_workers = min(args.max_workers, total_stores)
    print(f"\n⚡ 【動態分片策略】總店家數 {total_stores} ➔ 啟動 {num_workers} 台工作機平行採集")

    # 3. 建立任務分片目錄
    os.makedirs("tasks", exist_ok=True)

    # 均勻輪流分配店家到各 chunk (Round-robin)
    chunks = [[] for _ in range(num_workers)]
    for idx, store in enumerate(stores):
        chunk_idx = idx % num_workers
        chunks[chunk_idx].append(store)

    matrix_include = []
    for i in range(num_workers):
        chunk_file = f"tasks/chunk_{i}.json"
        task_payload = {
            "chunk_id": i,
            "total_chunks": num_workers,
            "stores_count": len(chunks[i]),
            "stores": chunks[i]
        }
        with open(chunk_file, "w", encoding="utf-8") as f:
            json.dump(task_payload, f, ensure_ascii=False, indent=2)

        matrix_include.append({
            "chunk_id": i,
            "stores_count": len(chunks[i])
        })
        print(f"   ├─ 💻 Worker {i:>2}: 分配 {len(chunks[i]):>3} 間店家 ➔ {chunk_file}")

    # 4. 輸出 Matrix 給 GitHub Actions
    matrix_json = json.dumps({"include": matrix_include})
    set_github_output("matrix", matrix_json)
    set_github_output("has_tasks", "true")
    print("\n✅ Coordinator 分片完成，已輸出動態 Matrix 供階段 2 平行運行。")


if __name__ == "__main__":
    main()
