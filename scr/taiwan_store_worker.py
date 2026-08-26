# -*- coding: utf-8 -*-
"""
Uber Eats 全台店家工作節點爬蟲 (Stage 2: Parallel Matrix Worker)
【核心功能】：
1. 讀取分配給本機的分片檔案 (tasks/chunk_X.json)。
2. 逐一針對各掃描點發送 Uber Eats 原生 Feed API (getFeedV1) 探索周邊店家。
3. 採用動態翻頁 (offset, hasMore) 徹底見底採集，並抽取店家名稱、UUID、經緯度座標、評分與標籤 (不爬菜單)。
4. 節點內自動去重，並產出 stores_chunk_X.json 與 stores_chunk_X.csv。
5. 即時回報進度與寫入 GitHub Actions $GITHUB_STEP_SUMMARY。
"""

import os
import sys
import re
import csv
import json
import time
import random
import urllib.parse
import argparse
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

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
        print(f"\n[Worker Step Summary]\n{markdown_text}\n")


def extract_stores_from_feed(obj, current_store_uuid=None):
    """從 Feed API 回傳之樹狀結構中遞迴抽取所有店家元件"""
    res = []
    if isinstance(obj, dict):
        s_uuid = obj.get('storeUuid') or current_store_uuid
        raw_u = obj.get('uuid')
        if raw_u and len(str(raw_u)) == 36 and not s_uuid:
            s_uuid = str(raw_u)

        action = obj.get('actionUrl', '')
        if action and isinstance(action, str) and '/store/' in action:
            title = ""
            if isinstance(obj.get('title'), dict):
                title = obj.get('title', {}).get('text', '')
            elif isinstance(obj.get('title'), str):
                title = obj.get('title')
            elif 'name' in obj and isinstance(obj.get('name'), str):
                title = obj.get('name')

            # 座標與標記 (mapMarker)
            marker = obj.get('mapMarker', {})
            store_lat = marker.get('latitude')
            store_lon = marker.get('longitude')

            # 評分與評論數
            rating_obj = obj.get('rating', {})
            rating_text = rating_obj.get('text') if isinstance(rating_obj, dict) else ''

            store_payload = obj.get('tracking', {}).get('storePayload', {})
            rating_info = store_payload.get('ratingInfo', {})
            rating_score = rating_info.get('storeRatingScore')
            rating_count = rating_info.get('ratingCount')

            # 標籤與中繼資訊
            meta_list = obj.get('meta', []) or []
            meta_texts = [m.get('text') for m in meta_list if isinstance(m, dict) and m.get('text')]

            # 封面大圖
            img_url = ''
            if obj.get('image', {}).get('items'):
                img_url = obj['image']['items'][0].get('url', '')

            # 萃取 slug 與乾淨 URL
            m = re.match(r'^(?:/tw)?/store/([^/?#]+)/([a-zA-Z0-9_-]+)', action)
            slug = m.group(1) if m else ''
            u_slug = m.group(2) if m else ''
            clean_url = f"https://www.ubereats.com/tw/store/{slug}/{u_slug}" if slug else f"https://www.ubereats.com{action}"

            res.append({
                "store_uuid": s_uuid or u_slug,
                "name": title.strip() if title else "",
                "slug": slug,
                "url_uuid": u_slug,
                "store_url": clean_url,
                "store_lat": store_lat,
                "store_lon": store_lon,
                "rating_text": rating_text,
                "rating_score": round(float(rating_score), 2) if rating_score is not None else None,
                "rating_count": rating_count,
                "meta_tags": meta_texts,
                "image_url": img_url,
                "is_orderable": store_payload.get('isOrderable', True),
                "availability_state": store_payload.get('storeAvailablityState', '')
            })

        for k, v in obj.items():
            res.extend(extract_stores_from_feed(v, s_uuid))
    elif isinstance(obj, list):
        for v in obj:
            res.extend(extract_stores_from_feed(v, current_store_uuid))
    return res


def scan_single_point(point: dict, max_pages: int = 10) -> tuple:
    """針對單一經緯度點位進行動態翻頁掃描"""
    p_id = point["id"]
    lat = point["latitude"]
    lon = point["longitude"]
    county = point.get("county", "台灣")
    addr_ref = f"{county}座標點#{p_id}"

    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "x-csrf-token": "x",
        "Content-Type": "application/json"
    }

    loc_cookie = {
        "address": {
            "address1": addr_ref,
            "address2": "",
            "aptOrSuite": "",
            "city": county,
            "country": "TW",
            "postalCode": "100",
            "region": ""
        },
        "latitude": lat,
        "longitude": lon,
        "reference": addr_ref,
        "referenceType": "google_places",
        "type": "google_places"
    }
    session.cookies.set("uev2.loc", urllib.parse.quote(json.dumps(loc_cookie)), domain=".ubereats.com")

    stores_found = {}
    offset = 0
    page = 1
    has_more = True

    while has_more and (max_pages == 0 or page <= max_pages):
        payload = {
            "userQuery": "",
            "feedProviderType": "LOCATION",
            "feedVersion": 2,
            "targetLocation": {
                "latitude": lat,
                "longitude": lon,
                "reference": addr_ref,
                "referenceType": "google_places",
                "address": {
                    "address1": addr_ref,
                    "city": county,
                    "country": "TW",
                    "postalCode": "100"
                }
            },
            "pageInfo": {"offset": offset}
        }

        page_success = False
        for attempt in range(1, 4):
            try:
                time.sleep(random.uniform(0.1, 0.25))
                resp = session.post("https://www.ubereats.com/api/getFeedV1", json=payload, headers=headers, timeout=12)
                
                if resp.status_code == 429:
                    sleep_s = 3.0 * attempt + random.uniform(1.0, 2.0)
                    time.sleep(sleep_s)
                    continue
                elif resp.status_code != 200:
                    time.sleep(1.0 * attempt)
                    continue

                data = resp.json().get("data", {})
                feed_items = data.get("feedItems", [])
                meta = data.get("meta", {})
                has_more = meta.get("hasMore", False)
                next_offset = meta.get("offset", offset)

                extracted = extract_stores_from_feed(feed_items)
                for s in extracted:
                    key = s["store_uuid"] or s["store_url"]
                    if not key:
                        continue
                    if key not in stores_found:
                        s_copy = dict(s)
                        s_copy["discovered_points"] = [{
                            "point_id": p_id,
                            "lat": lat,
                            "lon": lon,
                            "county": county
                        }]
                        stores_found[key] = s_copy
                    else:
                        # 補充可能缺失的欄位
                        for f in ["name", "store_lat", "store_lon", "rating_score", "rating_count", "image_url"]:
                            if s.get(f) and not stores_found[key].get(f):
                                stores_found[key][f] = s[f]

                if not has_more or next_offset == offset or len(feed_items) == 0:
                    has_more = False

                offset = next_offset
                page += 1
                page_success = True
                break
            except Exception as e:
                time.sleep(1.0 * attempt)

        if not page_success:
            break

    return p_id, list(stores_found.values()), page - 1


def main():
    parser = argparse.ArgumentParser(description="Uber Eats 全台店家工作節點爬蟲 (Worker)")
    parser.add_argument("--chunk-file", required=True, help="分片任務檔案路徑 (tasks/chunk_X.json)")
    parser.add_argument("--output-dir", required=True, help="結果輸出目錄 (output_worker_X)")
    parser.add_argument("--max-pages-per-point", type=int, default=10, help="每個座標點最大翻頁數 (預設 10，0為無限制)")
    parser.add_argument("--concurrency", type=int, default=2, help="本節點內部平行連線數 (預設 2)")
    args = parser.parse_args()

    start_time = time.time()

    if not os.path.exists(args.chunk_file):
        print(f"❌ 找不到分片檔案: {args.chunk_file}", file=sys.stderr)
        sys.exit(1)

    with open(args.chunk_file, "r", encoding="utf-8") as f:
        chunk_data = json.load(f)

    chunk_id = chunk_data.get("chunk_id", 0)
    batch_id = chunk_data.get("batch_id", "")
    points = chunk_data.get("points", [])
    total_points = len(points)

    print("=" * 80)
    print(f"🚀 【Stage 2: Worker {chunk_id} 啟動】全台掃描工作節點")
    print(f"📊 本節點分配點位數: {total_points} 個點")
    print(f"⚙️ 內部並發線程數: {args.concurrency} | 最大翻頁數: {args.max_pages_per_point}")
    print(f"📁 輸出目錄: {args.output_dir}")
    print("=" * 80)

    os.makedirs(args.output_dir, exist_ok=True)
    all_stores = {}
    completed_points = 0
    total_pages_scanned = 0

    # 執行掃描 (支援內部小規模執行緒池)
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        future_to_point = {
            executor.submit(scan_single_point, pt, args.max_pages_per_point): pt
            for pt in points
        }

        for future in as_completed(future_to_point):
            pt = future_to_point[future]
            try:
                p_id, point_stores, pages = future.result()
                completed_points += 1
                total_pages_scanned += pages

                # 節點內去重彙整
                for s in point_stores:
                    key = s["store_uuid"] or s["store_url"]
                    if key not in all_stores:
                        all_stores[key] = s
                    else:
                        # 合併 discovered_points
                        existing_pts = all_stores[key].get("discovered_points", [])
                        new_pts = s.get("discovered_points", [])
                        for np in new_pts:
                            if not any(ep["point_id"] == np["point_id"] for ep in existing_pts):
                                existing_pts.append(np)
                        all_stores[key]["discovered_points"] = existing_pts

                if completed_points % 10 == 0 or completed_points == total_points:
                    pct = (completed_points / total_points) * 100.0
                    print(f"   [進度 {completed_points:>3}/{total_points}] ({pct:5.1f}%) | 累積發現店家數: {len(all_stores):>4} 間 | 點#{pt['id']} ({pt.get('county')}) 取得 {len(point_stores)} 間 ({pages}頁)", flush=True)

            except Exception as e:
                print(f"   ⚠️ 處理點位 {pt.get('id')} 時發生異常: {e}", flush=True)

    elapsed = time.time() - start_time
    total_unique_stores = len(all_stores)

    print("\n" + "=" * 80)
    print(f"🎉 【Worker {chunk_id} 採集完成】")
    print(f"⏱️ 總耗時: {elapsed:.1f} 秒 (平均每點 {elapsed/max(1, total_points):.2f} 秒)")
    print(f"📍 掃描點位數: {completed_points}/{total_points} (翻頁總數: {total_pages_scanned})")
    print(f"🏪 累積不重複店家: {total_unique_stores} 間")
    print("=" * 80)

    # 匯出 JSON
    out_json_path = os.path.join(args.output_dir, f"stores_chunk_{chunk_id}.json")
    with open(out_json_path, "w", encoding="utf-8") as f:
        json.dump({
            "chunk_id": chunk_id,
            "batch_id": batch_id,
            "points_scanned": completed_points,
            "total_stores": total_unique_stores,
            "elapsed_seconds": round(elapsed, 2),
            "stores": list(all_stores.values())
        }, f, ensure_ascii=False, indent=2)

    # 匯出 CSV
    out_csv_path = os.path.join(args.output_dir, f"stores_chunk_{chunk_id}.csv")
    csv_fields = [
        "store_uuid", "name", "slug", "store_url", "store_lat", "store_lon",
        "rating_score", "rating_count", "rating_text", "meta_tags",
        "is_orderable", "availability_state", "discovered_counties_count", "image_url"
    ]
    with open(out_csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
        writer.writeheader()
        for s in all_stores.values():
            row = dict(s)
            row["meta_tags"] = ", ".join(s.get("meta_tags", []))
            row["discovered_counties_count"] = len(s.get("discovered_points", []))
            writer.writerow(row)

    print(f"💾 已成功產出:\n   ├─ {out_json_path}\n   └─ {out_csv_path}")

    # GITHUB_STEP_SUMMARY
    summary_md = f"""### ⚡ 【Worker {chunk_id} 成果報告】
- **掃描點位數**: `{completed_points}/{total_points}` 點 (翻頁次數: `{total_pages_scanned}`)
- **發現不重複店家**: **{total_unique_stores:,}** 間
- **執行耗時**: `{elapsed:.1f}` 秒 (平均 `{elapsed/max(1, total_points):.2f}` 秒/點)
"""
    append_github_step_summary(summary_md)


if __name__ == "__main__":
    main()
