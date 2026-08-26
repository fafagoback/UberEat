# -*- coding: utf-8 -*-
"""
Uber Eats 全台店家資料湖彙整引擎 (Stage 3: Reducer / Global Deduplication)
【核心功能】：
1. 讀取所有 Worker 上傳的 stores_chunk_*.json 檔案。
2. 進行全台灣全局去重 (以 store_uuid 為主鍵)，整併涵蓋點位與出現縣市。
3. 計算全台與各縣市統計指標 (店家總數、評分排行、外送覆蓋率)。
4. 匯出標準資料集：
   - taiwan_all_stores.json (完整 JSON 陣列)
   - taiwan_all_stores.csv (Excel 相容 UTF-8 BOM CSV)
   - taiwan_stores_by_county.json (依縣市分組)
   - taiwan_summary.json (摘要統計)
5. 寫入本地 SQLite 資料庫 (taiwan_stores 資料表)。
6. 產出精美 Markdown 儀表板至 $GITHUB_STEP_SUMMARY。
"""

import os
import sys
import glob
import csv
import json
import time
import sqlite3
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
        print(f"\n[Final Step Summary]\n{markdown_text}\n")


def save_to_sqlite(db_path: str, stores: list, batch_id: str):
    """將全台店家寫入 SQLite 資料庫 (taiwan_stores 表)"""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS taiwan_stores (
        store_uuid VARCHAR(64) PRIMARY KEY,
        name VARCHAR(255) NOT NULL,
        slug VARCHAR(255),
        store_url TEXT,
        primary_county VARCHAR(50),
        all_counties TEXT,
        coverage_points_count INT DEFAULT 1,
        store_lat DECIMAL(10, 7),
        store_lon DECIMAL(10, 7),
        rating_score DECIMAL(3, 2),
        rating_count VARCHAR(50),
        rating_text VARCHAR(50),
        meta_tags TEXT,
        image_url TEXT,
        is_orderable BOOLEAN,
        availability_state VARCHAR(50),
        last_crawled_batch VARCHAR(14),
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tw_stores_county ON taiwan_stores (primary_county);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tw_stores_rating ON taiwan_stores (rating_score DESC);")

    insert_sql = """
    INSERT OR REPLACE INTO taiwan_stores (
        store_uuid, name, slug, store_url, primary_county, all_counties,
        coverage_points_count, store_lat, store_lon, rating_score,
        rating_count, rating_text, meta_tags, image_url, is_orderable,
        availability_state, last_crawled_batch, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """

    rows = []
    for s in stores:
        rows.append((
            s.get("store_uuid"),
            s.get("name", ""),
            s.get("slug", ""),
            s.get("store_url", ""),
            s.get("primary_county", "未知"),
            ", ".join(s.get("counties_available", [])),
            len(s.get("discovered_points", [])),
            s.get("store_lat"),
            s.get("store_lon"),
            s.get("rating_score"),
            s.get("rating_count", ""),
            s.get("rating_text", ""),
            ", ".join(s.get("meta_tags", [])),
            s.get("image_url", ""),
            1 if s.get("is_orderable", True) else 0,
            s.get("availability_state", ""),
            batch_id
        ))

    cursor.executemany(insert_sql, rows)
    conn.commit()
    conn.close()
    print(f"✅ SQLite 入庫完成！已寫入 {len(rows)} 筆店家記錄至 {db_path} (taiwan_stores)")


def sort_chunk_key(ws: dict):
    cid = str(ws.get("chunk_id", ""))
    digits = "".join(c for c in cid if c.isdigit())
    num = int(digits) if digits else 9999
    return (num, cid)


def main():
    parser = argparse.ArgumentParser(description="Uber Eats 全台店家資料湖彙整引擎 (Reducer)")
    parser.add_argument("--src-dir", required=True, help="存放所有 Worker 輸出的目錄路徑")
    parser.add_argument("--output-dir", default="taiwan_stores_dataset", help="最終資料集匯出目錄")
    parser.add_argument("--db-path", default="ubereats_monitor.db", help="SQLite 資料庫路徑")
    args = parser.parse_args()

    start_time = time.time()
    now_dt = datetime.now(TW_TZ)
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")

    print("=" * 80)
    print("🚀 【Stage 3: Reducer 全台店家去重彙整引擎】啟動")
    print(f"⏰ 執行時間: {now_str}")
    print(f"📦 來源目錄: {args.src_dir}")
    print(f"📁 匯出目錄: {args.output_dir}")
    print("=" * 80)

    os.makedirs(args.output_dir, exist_ok=True)

    # 1. 搜尋所有 chunk json 檔案
    search_pattern = os.path.join(args.src_dir, "**", "*.json")
    json_files = glob.glob(search_pattern, recursive=True)

    # 若特定名稱存在，優先過濾 stores_chunk_*.json
    chunk_files = [f for f in json_files if "stores_chunk_" in os.path.basename(f)]
    if not chunk_files:
        chunk_files = [f for f in json_files if "chunk_" in os.path.basename(f) and os.path.basename(f) not in ["taiwan_all_stores.json", "taiwan_summary.json", "taiwan_stores_by_county.json"]]

    print(f"🔍 找到 {len(chunk_files)} 個 Worker 結果分片檔案。")

    if not chunk_files:
        print("❌ 錯誤：未找到任何有效的 Worker JSON 結果檔案！", file=sys.stderr)
        sys.exit(1)

    global_stores = {}
    batch_id = now_dt.strftime("%Y%m%d%H%M%S")
    worker_stats = []
    total_raw_store_sightings = 0

    # 2. 逐一讀取並合併去重
    for cf in chunk_files:
        try:
            with open(cf, "r", encoding="utf-8") as f:
                data = json.load(f)

            c_id = data.get("chunk_id", os.path.basename(cf))
            b_id = data.get("batch_id")
            if b_id:
                batch_id = b_id

            stores_in_chunk = data.get("stores", [])
            total_raw_store_sightings += len(stores_in_chunk)

            worker_stats.append({
                "chunk_id": c_id,
                "file": os.path.basename(cf),
                "points_scanned": data.get("points_scanned", 0),
                "stores_found": len(stores_in_chunk),
                "elapsed": data.get("elapsed_seconds", 0.0)
            })

            for s in stores_in_chunk:
                key = s.get("store_uuid") or s.get("store_url")
                if not key:
                    continue

                if key not in global_stores:
                    global_stores[key] = dict(s)
                else:
                    # 補充更完整欄位
                    for field in ["name", "slug", "store_url", "store_lat", "store_lon", "rating_score", "rating_count", "rating_text", "image_url"]:
                        if s.get(field) and not global_stores[key].get(field):
                            global_stores[key][field] = s[field]

                    # 合併 discovered_points
                    existing_pts = global_stores[key].get("discovered_points", [])
                    for pt in s.get("discovered_points", []):
                        if not any(ep.get("point_id") == pt.get("point_id") for ep in existing_pts):
                            existing_pts.append(pt)
                    global_stores[key]["discovered_points"] = existing_pts

        except Exception as e:
            print(f"⚠️ 解析檔案 {cf} 失敗: {e}")

    # 3. 二次精煉：計算 primary_county 與 counties_available
    final_stores_list = []
    county_store_counts = {}
    rating_distribution = {"4.8 以上 (頂級口碑)": 0, "4.5 ~ 4.7 (優質首選)": 0, "4.0 ~ 4.4 (一般水準)": 0, "4.0 以下或無評分": 0}

    for s in global_stores.values():
        pts = s.get("discovered_points", [])
        counties_set = set()
        county_frequency = {}

        for pt in pts:
            c = pt.get("county", "未知")
            counties_set.add(c)
            county_frequency[c] = county_frequency.get(c, 0) + 1

        # 優先以被發現最多次的縣市作為 primary_county
        if county_frequency:
            primary_county = max(county_frequency.items(), key=lambda x: x[1])[0]
        else:
            primary_county = "未知"

        s["primary_county"] = primary_county
        s["counties_available"] = sorted(list(counties_set))
        s["coverage_points_count"] = len(pts)

        # 評分分佈統計
        r_score = s.get("rating_score")
        if r_score is not None and isinstance(r_score, (int, float)):
            if r_score >= 4.8:
                rating_distribution["4.8 以上 (頂級口碑)"] += 1
            elif r_score >= 4.5:
                rating_distribution["4.5 ~ 4.7 (優質首選)"] += 1
            elif r_score >= 4.0:
                rating_distribution["4.0 ~ 4.4 (一般水準)"] += 1
            else:
                rating_distribution["4.0 以下或無評分"] += 1
        else:
            rating_distribution["4.0 以下或無評分"] += 1

        county_store_counts[primary_county] = county_store_counts.get(primary_county, 0) + 1
        final_stores_list.append(s)

    # 依照評分與名稱排序
    final_stores_list.sort(key=lambda x: (x.get("rating_score") or 0.0, x.get("name", "")), reverse=True)
    total_unique_stores = len(final_stores_list)

    print("\n" + "=" * 80)
    print("📊 【全台彙整與去重成果】")
    print(f"🏪 全國去重後店家總數: {total_unique_stores:,} 間 (原始觀測累積: {total_raw_store_sightings:,} 間)")
    print(f"🏙️ 涵蓋縣市數量: {len(county_store_counts)} 個縣市")
    print("=" * 80)

    # 4. 匯出標準檔案
    # ① taiwan_all_stores.json
    out_all_json = os.path.join(args.output_dir, "taiwan_all_stores.json")
    with open(out_all_json, "w", encoding="utf-8") as f:
        json.dump(final_stores_list, f, ensure_ascii=False, indent=2)

    # ② taiwan_all_stores.csv (含 UTF-8 BOM 供 Excel 完美開啟)
    out_all_csv = os.path.join(args.output_dir, "taiwan_all_stores.csv")
    csv_headers = [
        "store_uuid", "name", "primary_county", "all_counties", "rating_score",
        "rating_count", "rating_text", "coverage_points_count", "store_lat",
        "store_lon", "meta_tags", "store_url", "is_orderable", "availability_state", "image_url"
    ]
    with open(out_all_csv, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_headers, extrasaction="ignore")
        writer.writeheader()
        for s in final_stores_list:
            row = dict(s)
            row["all_counties"] = ", ".join(s.get("counties_available", []))
            row["meta_tags"] = ", ".join(s.get("meta_tags", []))
            writer.writerow(row)

    # ③ taiwan_stores_by_county.json
    by_county = {}
    for s in final_stores_list:
        c = s["primary_county"]
        if c not in by_county:
            by_county[c] = []
        by_county[c].append(s)

    out_by_county_json = os.path.join(args.output_dir, "taiwan_stores_by_county.json")
    with open(out_by_county_json, "w", encoding="utf-8") as f:
        json.dump(by_county, f, ensure_ascii=False, indent=2)

    # ④ taiwan_summary.json
    summary_data = {
        "batch_id": batch_id,
        "crawled_at": now_str,
        "total_unique_stores": total_unique_stores,
        "total_raw_sightings": total_raw_store_sightings,
        "total_counties": len(county_store_counts),
        "county_distribution": county_store_counts,
        "rating_distribution": rating_distribution,
        "worker_performance": worker_stats
    }
    out_summary_json = os.path.join(args.output_dir, "taiwan_summary.json")
    with open(out_summary_json, "w", encoding="utf-8") as f:
        json.dump(summary_data, f, ensure_ascii=False, indent=2)

    print(f"💾 資料集匯出完成：\n   ├─ {out_all_json}\n   ├─ {out_all_csv}\n   ├─ {out_by_county_json}\n   └─ {out_summary_json}")

    # 5. 寫入 SQLite
    try:
        save_to_sqlite(args.db_path, final_stores_list, batch_id)
    except Exception as e:
        print(f"⚠️ 寫入 SQLite 異常: {e}")

    elapsed_total = time.time() - start_time

    # 6. 生成 GitHub Actions $GITHUB_STEP_SUMMARY 報表
    summary_md = f"""## 🇹🇼 【全台 Uber Eats 店家掃描成果大儀表板】
> ⏰ **採集批次**: `{batch_id}` ({now_str}) | ⏱️ **總整併耗時**: `{elapsed_total:.2f}` 秒

### 📊 核心成果摘要
| 項目 | 數值 | 說明 |
| :--- | :---: | :--- |
| 🏪 **全台不重複店家總數** | **`{total_unique_stores:,}` 間** | 經全台 1,559 點全局去重 |
| 👁️ **原始店家觀測總次數** | **`{total_raw_store_sightings:,}` 次** | 重複半徑交叉觀測總量 |
| 🏙️ **有效涵蓋縣市數** | **`{len(county_store_counts)}` 個** | 包含台灣各直轄市與縣市 |
| ⚡ **平行工作機台數** | **`{len(worker_stats)}` 台** | 15 台 Matrix 平行採集 |

---

### 🏙️ 各縣市店家分佈排行
| 排名 | 縣市名稱 | 店家總數 | 佔比 |
| :---: | :--- | :---: | :---: |
"""
    sorted_c = sorted(county_store_counts.items(), key=lambda x: x[1], reverse=True)
    for rank, (c_name, c_cnt) in enumerate(sorted_c, start=1):
        pct = (c_cnt / total_unique_stores) * 100.0 if total_unique_stores > 0 else 0.0
        summary_md += f"| {rank} | **{c_name}** | {c_cnt:,} | {pct:.1f}% |\n"

    summary_md += """
---

### ⭐ 全台店家評分分佈
| 評分區間 | 店家數量 | 佔比 |
| :--- | :---: | :---: |
"""
    for r_label, r_cnt in rating_distribution.items():
        pct = (r_cnt / total_unique_stores) * 100.0 if total_unique_stores > 0 else 0.0
        summary_md += f"| {r_label} | {r_cnt:,} | {pct:.1f}% |\n"

    summary_md += """
---

### 💻 15 台工作機採集效能
| Worker ID | 掃描點數 | 發現店家數 | 耗時 (秒) |
| :---: | :---: | :---: | :---: |
"""
    for ws in sorted(worker_stats, key=sort_chunk_key):
        summary_md += f"| Worker {ws['chunk_id']} | {ws.get('points_scanned', 0)} | {ws.get('stores_found', 0):,} | {ws.get('elapsed', 0.0):.1f}s |\n"

    append_github_step_summary(summary_md)


if __name__ == "__main__":
    main()
