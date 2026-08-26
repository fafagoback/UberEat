# -*- coding: utf-8 -*-
"""
Uber Eats 全台店家大規模掃描調度器 (Stage 1: Coordinator)
【核心功能】：
1. 讀取全台 3km 陸地掃描基準點 CSV (例如 taiwan_scan_points_3km_land_only.csv，共 1,559 點)。
2. 採用輪流分片法 (Round-Robin) 平均分配給 15 台 (或自訂台數) 工作機。
3. 輸出 tasks/chunk_0.json ~ tasks/chunk_14.json 任務檔。
4. 輸出 GitHub Actions 專用 Matrix JSON 與統計資訊至 $GITHUB_OUTPUT 與 $GITHUB_STEP_SUMMARY。
"""

import os
import sys
import csv
import json
import time
import argparse
from datetime import datetime, timezone, timedelta

TW_TZ = timezone(timedelta(hours=8))

# 確保標準輸出支援 UTF-8
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass


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
        print(f"\n[Step Summary]\n{markdown_text}\n")


def set_github_output(name: str, value: str):
    """將變數寫入 GitHub Actions $GITHUB_OUTPUT 供下游 Job 讀取"""
    github_output_path = os.environ.get("GITHUB_OUTPUT")
    if github_output_path:
        with open(github_output_path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    else:
        print(f"[Local Output] {name}={value}")


def main():
    parser = argparse.ArgumentParser(description="Uber Eats 全台店家採集調度器 (Coordinator)")
    parser.add_argument("--scan-file", default="taiwan_scan_points_3km_land_only.csv", help="掃描基準點 CSV 檔案路徑")
    parser.add_argument("--max-workers", type=int, default=15, help="工作機台數 (預設 15)")
    parser.add_argument("--output-dir", default="tasks", help="任務分片輸出目錄")
    args = parser.parse_args()

    start_time = time.time()
    now_dt = datetime.now(TW_TZ)
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    batch_id = now_dt.strftime("%Y%m%d%H%M%S")

    print("=" * 80)
    print("🚀 【Stage 1: 全台店家掃描調度器 (Coordinator)】啟動")
    print(f"⏰ 執行時間: {now_str} (Batch: {batch_id})")
    print(f"📍 基準點檔案: {args.scan_file}")
    print(f"⚙️ 目標工作機台數: {args.max_workers}")
    print("=" * 80)

    # 1. 讀取並檢驗 CSV 檔案
    if not os.path.exists(args.scan_file):
        alt_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), args.scan_file)
        if os.path.exists(alt_path):
            args.scan_file = alt_path
        else:
            print(f"❌ 找不到掃描點檔案: {args.scan_file}", file=sys.stderr)
            sys.exit(1)

    points = []
    county_counts = {}

    with open(args.scan_file, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            p_id = int(row.get("id", len(points) + 1))
            lat = float(row.get("latitude", 0.0))
            lon = float(row.get("longitude", 0.0))
            county = row.get("county", "未知")
            radius = float(row.get("radius_km", 3.0))

            if lat != 0.0 and lon != 0.0:
                point_data = {
                    "id": p_id,
                    "latitude": lat,
                    "longitude": lon,
                    "county": county,
                    "radius_km": radius,
                    "twd97_x": row.get("twd97_x"),
                    "twd97_y": row.get("twd97_y")
                }
                points.append(point_data)
                county_counts[county] = county_counts.get(county, 0) + 1

    total_points = len(points)
    print(f"✅ 成功載入 {total_points} 個掃描基準點，涵蓋 {len(county_counts)} 個縣市區域。")

    if total_points == 0:
        print("❌ 錯誤：未讀取到任何有效掃描點！", file=sys.stderr)
        sys.exit(1)

    # 2. 計算工作機分片 (Round-Robin 分配)
    num_workers = min(args.max_workers, total_points)
    os.makedirs(args.output_dir, exist_ok=True)

    chunks = [[] for _ in range(num_workers)]
    for idx, pt in enumerate(points):
        chunk_idx = idx % num_workers
        chunks[chunk_idx].append(pt)

    matrix_include = []
    print("\n📦 【分片統計】各工作機分配點位數：")
    for i in range(num_workers):
        chunk_points = chunks[i]
        chunk_file = os.path.join(args.output_dir, f"chunk_{i}.json")
        chunk_data = {
            "chunk_id": i,
            "total_chunks": num_workers,
            "batch_id": batch_id,
            "created_at": now_str,
            "points_count": len(chunk_points),
            "points": chunk_points
        }

        with open(chunk_file, "w", encoding="utf-8") as cf:
            json.dump(chunk_data, cf, ensure_ascii=False, indent=2)

        matrix_include.append({"chunk_id": i})
        print(f"   ├─ Worker {i:>2}: 分配 {len(chunk_points):>4} 個點位 ➔ {chunk_file}")

    # Hard checkpoint: all source points must appear exactly once in non-empty
    # chunk files. A broken dispatcher must never launch a green partial run.
    checkpoint_files = [os.path.join(args.output_dir, f"chunk_{i}.json") for i in range(num_workers)]
    checkpoint_points = [pt for chunk in chunks for pt in chunk]
    checkpoint_ids = [str(pt.get("id")) for pt in checkpoint_points]
    checkpoint_errors = []
    if len(checkpoint_points) != total_points:
        checkpoint_errors.append(f"分片點位合計 {len(checkpoint_points)} != 來源 {total_points}")
    if len(checkpoint_ids) != len(set(checkpoint_ids)):
        checkpoint_errors.append("分片中存在重複 point_id")
    empty_files = [p for p in checkpoint_files if not os.path.isfile(p) or os.path.getsize(p) <= 0]
    if empty_files:
        checkpoint_errors.append(f"缺少或空白分片: {empty_files}")

    # 3. 輸出 GitHub Actions 變數
    matrix_json = json.dumps({"include": matrix_include})
    set_github_output("matrix", matrix_json)
    set_github_output("has_tasks", "true" if num_workers > 0 else "false")
    set_github_output("total_points", str(total_points))
    set_github_output("num_workers", str(num_workers))
    set_github_output("batch_id", batch_id)

    elapsed = time.time() - start_time
    print(f"\n✨ 調度完成！共生成 {num_workers} 個分片任務檔，耗時 {elapsed:.2f} 秒。")

    # 4. 生成 GITHUB_STEP_SUMMARY
    summary_md = f"""## 🚀 【Stage 1: Coordinator 調度完成】全台 15 台工作機準備就緒
- **執行批次**: `{batch_id}` ({now_str})
- **基準點來源**: `{os.path.basename(args.scan_file)}`
- **全台採樣點總數**: **{total_points:,}** 個陸地網格點
- **啟動工作機台數**: **{num_workers}** 台 (平行並發)
- **平均每台負擔**: 約 **{total_points // num_workers} ~ {total_points // num_workers + 1}** 個座標點

### {'❌' if checkpoint_errors else '✅'} Stage 1 產出 Checkpoint
| 檢核項目 | 預期 | 實際 | 狀態 |
| :--- | :--- | :--- | :---: |
| 分片檔案 | `{num_workers}` 個非空 JSON | `{sum(1 for p in checkpoint_files if os.path.isfile(p) and os.path.getsize(p) > 0)}` 個 | {'✅' if not empty_files else '❌'} |
| 點位總和 | `{total_points}` 點 | `{len(checkpoint_points)}` 點 | {'✅' if len(checkpoint_points) == total_points else '❌'} |
| point_id 唯一性 | 0 筆重複 | `{len(checkpoint_ids) - len(set(checkpoint_ids))}` 筆重複 | {'✅' if len(checkpoint_ids) == len(set(checkpoint_ids)) else '❌'} |

### 🏙️ 採樣點縣市分佈 Top 10
| 縣市 | 採樣點數 | 佔比 |
| :--- | :---: | :---: |
"""
    sorted_counties = sorted(county_counts.items(), key=lambda x: x[1], reverse=True)
    for c_name, c_cnt in sorted_counties[:10]:
        pct = (c_cnt / total_points) * 100.0
        summary_md += f"| {c_name} | {c_cnt:,} | {pct:.1f}% |\n"

    append_github_step_summary(summary_md)

    if checkpoint_errors:
        print("❌ Stage 1 checkpoint 失敗：" + "；".join(checkpoint_errors), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
