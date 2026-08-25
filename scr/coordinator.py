# -*- coding: utf-8 -*-
"""
Uber Eats 分散式採集主調度器 (Stage 1: Coordinator)
【檢核與重試機制】：
1. 嚴格檢核地理定位 (3 次重試)
2. 嚴格檢核周邊店家探索 (3 次重試，未達標立即報錯中斷 sys.exit(1))
3. 嚴格檢核各分片任務檔案之完整性與總數校驗
4. 輸出檢核報告至 GitHub Actions $GITHUB_STEP_SUMMARY
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime

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

from location_batch_scraper import geocode_address, discover_nearby_stores

MAX_WORKERS_LIMIT = 15


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


def set_github_output(name: str, value: str):
    """將變數寫入 GitHub Actions $GITHUB_OUTPUT 供下游 Job 讀取"""
    github_output_path = os.environ.get("GITHUB_OUTPUT")
    if github_output_path:
        with open(github_output_path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    else:
        print(f"[Local Dry Run Output] {name}={value}")


def fatal_error(step_name: str, reason: str, expected: str = "", actual: str = "", retries: int = 3):
    """輸出醒目錯誤橫幅、寫入 GITHUB_STEP_SUMMARY 並強制以 exit code 1 終止"""
    msg = f"""
================================================================================
❌ 【階段 1: Coordinator 檢核失敗 (FATAL ERROR)】
步驟名稱: {step_name}
重試次數: 已重試 {retries} 次均未達標
錯誤原因: {reason}
預期成果: {expected}
實際結果: {actual}
================================================================================
"""
    print(msg, file=sys.stderr, flush=True)
    
    summary_md = f"""
## ❌ 【階段 1: Coordinator 調度失敗】
> [!CAUTION]
> **在步驟「{step_name}」經 {retries} 次重試仍未達成預期成果，流程已強制終止 (Exit Code 1)！**
> - **錯誤原因**: `{reason}`
> - **預期成果**: `{expected}`
> - **實際結果**: `{actual}`
"""
    append_github_step_summary(summary_md)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Uber Eats 分散式爬蟲調度器 (嚴格檢核版)")
    parser.add_argument("--address", default="台北市士林區中山北路七段", help="基準地址")
    parser.add_argument("--max-workers", type=int, default=MAX_WORKERS_LIMIT, help="最大工作機數量 (預設 15)")
    args = parser.parse_args()

    start_time = time.time()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print("=" * 80)
    print("🚀 【階段 1: Coordinator 調度節點】啟動 (嚴格檢核與 3 次重試模式)")
    print(f"⏰ 執行時間: {now_str}")
    print(f"📍 目標地址: {args.address}")
    print(f"⚙️ 最大工作機台數上限: {args.max_workers}")
    print("=" * 80)

    # ---------------------------------------------------------
    # 步驟 1.1：地理座標解析 (Geocoding) - 3 次重試檢核
    # ---------------------------------------------------------
    print("\n🔍 【步驟 1.1】執行地理座標解析...")
    lat, lon, geo_src = 0.0, 0.0, ""
    geo_retries = 3
    geo_passed = False
    
    for attempt in range(1, geo_retries + 1):
        try:
            lat, lon, geo_src = geocode_address(args.address)
            # 檢核: 座標是否在有效數值範圍 (台灣本島或合理範圍)
            if lat != 0 and lon != 0 and (20.0 <= lat <= 27.0) and (118.0 <= lon <= 124.0):
                print(f"✅ [步驟 1.1 通過] (嘗試 {attempt}/{geo_retries}) 座標: ({lat:.7f}, {lon:.7f}) 來源: {geo_src}")
                geo_passed = True
                break
            else:
                print(f"⚠️ [步驟 1.1 檢核未過] (嘗試 {attempt}/{geo_retries}) 座標異常: ({lat}, {lon})")
        except Exception as e:
            print(f"⚠️ [步驟 1.1 異常] (嘗試 {attempt}/{geo_retries}): {e}")
        
        if attempt < geo_retries:
            time.sleep(2.0 * attempt)

    if not geo_passed:
        fatal_error(
            step_name="步驟 1.1 地理座標解析",
            reason="無法取得目標地址之有效 GPS 經緯度座標",
            expected="緯度 20~27, 經度 118~124",
            actual=f"lat={lat}, lon={lon}",
            retries=geo_retries
        )

    # ---------------------------------------------------------
    # 步驟 1.2：周邊外送店家探索 (Nearby Discovery) - 3 次重試檢核
    # ---------------------------------------------------------
    print(f"\n🔍 【步驟 1.2】執行周邊外送店家動態探索...")
    stores = []
    discovery_retries = 3
    discovery_passed = False
    last_discovery_err = ""

    for attempt in range(1, discovery_retries + 1):
        print(f"   ▶ 探索周邊店家 (嘗試 {attempt}/{discovery_retries})...")
        try:
            stores = discover_nearby_stores(lat, lon, args.address)
            total_stores = len(stores)
            
            # 檢核標準: 探索到的店家數必須 > 0
            if total_stores > 0:
                print(f"✅ [步驟 1.2 通過] (嘗試 {attempt}/{discovery_retries}) 成功探索到 {total_stores} 間外送店家！")
                discovery_passed = True
                break
            else:
                last_discovery_err = f"回傳店家數為 0 間 (可能遭反爬阻擋或伺服端無資料)"
                print(f"⚠️ [步驟 1.2 檢核未過] (嘗試 {attempt}/{discovery_retries}): {last_discovery_err}")
        except Exception as e:
            last_discovery_err = str(e)
            print(f"⚠️ [步驟 1.2 異常] (嘗試 {attempt}/{discovery_retries}): {e}")

        if attempt < discovery_retries:
            backoff_sec = 3.0 * attempt
            print(f"   ⏳ 等待 {backoff_sec:.1f} 秒後重新探索...")
            time.sleep(backoff_sec)

    if not discovery_passed or len(stores) == 0:
        fatal_error(
            step_name="步驟 1.2 周邊店家探索",
            reason=f"重試 {discovery_retries} 次後探索店家數仍為 0 間：{last_discovery_err}",
            expected="店家總數 > 0",
            actual=f"店家總數 = {len(stores)}",
            retries=discovery_retries
        )

    total_stores = len(stores)

    # ---------------------------------------------------------
    # 步驟 1.3：分散式任務分片與檔案校驗 (Task Chunks Validation)
    # ---------------------------------------------------------
    print(f"\n🧩 【步驟 1.3】計算分片並驗證分片任務檔案...")
    num_workers = min(args.max_workers, total_stores)
    print(f"   ⚡ 動態分片: 總店家數 {total_stores} ➔ 啟動 {num_workers} 台工作機平行採集")

    os.makedirs("tasks", exist_ok=True)

    # 均勻輪流分配店家到各 chunk (Round-robin)
    chunks = [[] for _ in range(num_workers)]
    for idx, store in enumerate(stores):
        chunk_idx = idx % num_workers
        chunks[chunk_idx].append(store)

    matrix_include = []
    chunk_verification_errors = []
    assigned_stores_total = 0

    for i in range(num_workers):
        chunk_file = f"tasks/chunk_{i}.json"
        task_payload = {
            "chunk_id": i,
            "total_chunks": num_workers,
            "stores_count": len(chunks[i]),
            "stores": chunks[i]
        }
        
        # 寫入分片檔案
        with open(chunk_file, "w", encoding="utf-8") as f:
            json.dump(task_payload, f, ensure_ascii=False, indent=2)

        # 檢核分片檔案完整性
        if not os.path.exists(chunk_file):
            chunk_verification_errors.append(f"Worker {i} 分片檔案未成功生成: {chunk_file}")
            continue
        
        if os.path.getsize(chunk_file) == 0:
            chunk_verification_errors.append(f"Worker {i} 分片檔案大小為 0 位元組: {chunk_file}")
            continue

        try:
            with open(chunk_file, "r", encoding="utf-8") as f:
                verified_payload = json.load(f)
            v_count = len(verified_payload.get("stores", []))
            if v_count != len(chunks[i]) or v_count == 0:
                chunk_verification_errors.append(f"Worker {i} 店家數量校驗不符: 預期 {len(chunks[i])}, 實際 {v_count}")
                continue
            assigned_stores_total += v_count
        except Exception as e:
            chunk_verification_errors.append(f"Worker {i} JSON 解析失敗: {e}")
            continue

        matrix_include.append({
            "chunk_id": i,
            "stores_count": len(chunks[i])
        })
        print(f"   ├─ 💻 Worker {i:>2}: 分配 {len(chunks[i]):>3} 間店家 ➔ {chunk_file} (校驗通過 ✅)")

    # 總量一致性校驗
    if chunk_verification_errors:
        fatal_error(
            step_name="步驟 1.3 分片任務檔案校驗",
            reason=f"分片檔案校驗發現 {len(chunk_verification_errors)} 處錯誤: {'; '.join(chunk_verification_errors[:3])}",
            expected=f"{num_workers} 個合法分片檔案",
            actual=f"{len(matrix_include)} 個通過校驗",
            retries=1
        )

    if assigned_stores_total != total_stores:
        fatal_error(
            step_name="步驟 1.3 店家總量一致性校驗",
            reason="分片分配後店家總數與探索總數不一致",
            expected=f"總店家數 = {total_stores}",
            actual=f"分片總和 = {assigned_stores_total}",
            retries=1
        )

    print(f"✅ [步驟 1.3 通過] 所有 {num_workers} 個分片檔案校驗 100% 合法，總店家數 {assigned_stores_total} 精確對齊！")

    # ---------------------------------------------------------
    # 步驟 1.4：輸出 GitHub Actions Matrix 與 Step Summary
    # ---------------------------------------------------------
    matrix_json = json.dumps({"include": matrix_include})
    set_github_output("matrix", matrix_json)
    set_github_output("has_tasks", "true")

    elapsed = time.time() - start_time

    # 產生 GHA Step Summary
    summary_md = f"""
## 🚀 【階段 1: Coordinator 調度節點】檢核成功報告

> **基準地址**: `{args.address}` | **定位座標**: `({lat:.7f}, {lon:.7f})` | **耗時**: `{elapsed:.2f} 秒` | **狀態**: ✅ 檢核通過

### 📋 Coordinator 檢核清單
| 檢核步驟 | 檢核項目 | 預期標準 | 實際結果 | 檢核狀態 |
| :--- | :--- | :--- | :--- | :---: |
| **步驟 1.1** | 地理座標解析 (Geocoding) | 取得合法 GPS 經緯度 | `({lat:.7f}, {lon:.7f})` ({geo_src}) | ✅ 通過 |
| **步驟 1.2** | 周邊外送店家探索 | 探索店家數 `> 0` | 探索到 **{total_stores}** 間店家 | ✅ 通過 |
| **步驟 1.3** | 分片任務檔案完整性 | {num_workers} 個分片且總數對齊 | {num_workers} 個 chunk 檔案校驗 100% 通過 | ✅ 通過 |

### 💻 動態並行分片矩陣 (Workers Matrix)
| Worker 編號 | 分配店家數 | 分片檔案 | 校驗狀態 |
| :---: | :---: | :--- | :---: |
"""
    for item in matrix_include:
        cid = item["chunk_id"]
        cnt = item["stores_count"]
        summary_md += f"| **Worker {cid}** | {cnt} 間 | `tasks/chunk_{cid}.json` | ✅ 通過 |\n"

    append_github_step_summary(summary_md)

    print("\n" + "=" * 80)
    print("🎉 【階段 1: Coordinator 調度完成】全流程檢核通過，準備進入階段 2 並行採集！")
    print("=" * 80)


if __name__ == "__main__":
    main()
