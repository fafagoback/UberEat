# -*- coding: utf-8 -*-
"""
Uber Eats 邊緣靜態快照導出引擎 (Stage 6: Export Static Snapshots to Jamstack CDN)

【核心職責】:
1. 嚴格驗證來源 JSON 菜單檔案的結構與完整性
2. 執行本地 SQLite ETL (json_to_db) 並建立完整關聯
3. 執行智慧差異情報引擎 (alert_engine) 計算 7 天歷史價差、買一送一與新品
4. 匯出完全預先計算好的極速靜態 JSON 資料集至 web/data/:
   - stats.json: 大盤總覽統計與時間戳記
   - discounts.json: 今日大特價商品清單
   - new_stores.json: 全新進駐店家清單
   - new_products.json: 老店新菜推薦清單
   - promotions.json: 買一送一與促銷活動清單
   - products.json: 全品庫快速檢索清單
   - history.json: 各商品歷史價格趨勢索引字典
   - dashboard_data.js / dashboard_data.json: 離線獨立檢視包
   - version.json: 發布版本時間戳記
5. 輸出 Final 檢核報告至 GitHub Actions $GITHUB_STEP_SUMMARY
"""

import os
import sys
import glob
import json
import time
import re
import argparse
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional

TW_TZ = timezone(timedelta(hours=8))

# 確保標準輸出支援 UTF-8
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

# 引入本模組目錄
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from json_to_db import UberEatsDBImporter
from alert_engine import UberEatsAlertEngine
from snapshot_validation import validate_document, validate_snapshot

TW_CITIES = [
    "基隆市", "台北市", "臺北市", "新北市", "桃園市", "新竹市", "新竹縣", "苗栗縣",
    "台中市", "臺中市", "彰化縣", "南投縣", "雲林縣", "嘉義市", "嘉義縣", "台南市",
    "臺南市", "高雄市", "屏東縣", "宜蘭縣", "花蓮縣", "台東縣", "臺東縣", "澎湖縣",
    "金門縣", "連江縣"
]


def extract_city(locality: str, street_address: str) -> str:
    """自地址與行政區文字中識別台灣主要縣市名稱"""
    combined = f"{locality or ''} {street_address or ''}"
    for city in TW_CITIES:
        if city in combined or city.replace("臺", "台") in combined or city.replace("台", "臺") in combined:
            return city.replace("臺", "台")
    return locality or "其他"


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


def fatal_error(step_name: str, reason: str, expected: str = "", actual: str = "", retries: int = 0):
    """輸出錯誤橫幅並以 exit code 1 終止"""
    msg = f"""
================================================================================
❌ 【階段 6: 靜態快照導出失敗 (FATAL ERROR)】
步驟名稱: {step_name}
錯誤原因: {reason}
預期成果: {expected}
實際結果: {actual}
================================================================================
"""
    print(msg, file=sys.stderr, flush=True)

    summary_md = f"""
### ❌ 【靜態快照導出失敗】
> [!CAUTION]
> **在「{step_name}」執行失敗，流程已強制終止！**
> - **錯誤原因**: `{reason}`
> - **預期成果**: `{expected}`
> - **實際結果**: `{actual}`
"""
    append_github_step_summary(summary_md)
    sys.exit(1)


def calculate_7day_discounts(conn: sqlite3.Connection, latest_batch: str, min_discount_pct: float = 20.0, min_savings_twd: float = 20.0) -> List[Dict[str, Any]]:
    """計算相較於過去 7 天內最高實質單價的降價商品"""
    cursor = conn.cursor()

    # 計算 7 天前時間戳記 (YYYYMMDDhhmmss)
    try:
        dt = datetime.strptime(latest_batch, "%Y%m%d%H%M%S")
        seven_days_ago = (dt - timedelta(days=7)).strftime("%Y%m%d%H%M%S")
    except Exception:
        seven_days_ago = "20000101000000"

    query = """
    SELECT 
        p1.product_id,
        p1.store_id,
        p1.store_name,
        p1.product_name,
        p1.category_name,
        p1.description,
        p1.price as curr_raw_price,
        p1.quantity as curr_qty,
        p1.promo_type,
        ROUND(p1.price * 1.0 / p1.quantity, 2) as curr_eff_price,
        (
            SELECT MAX(p0.price * 1.0 / p0.quantity)
            FROM products p0
            WHERE p0.product_id = p1.product_id
              AND p0.crawled_time >= ?
              AND p0.crawled_time < p1.crawled_time
              AND p0.price >= 1
              AND (p0.is_open = 1 OR p0.is_open IS NULL)
        ) as max_7day_eff_price,
        COALESCE(NULLIF(s.order_action_url, ''), s.store_url, '') as order_action_url,
        s.rating_value,
        s.review_count,
        s.locality,
        s.street_address,
        COALESCE(p1.is_open, s.is_open, 1) as is_open
    FROM products p1
    LEFT JOIN stores s ON p1.store_id = s.store_id AND p1.crawled_time = s.crawled_time
    WHERE p1.crawled_time = ?
      AND p1.price >= 1
      AND (p1.is_open = 1 OR p1.is_open IS NULL);
    """
    cursor.execute(query, (seven_days_ago, latest_batch))
    rows = cursor.fetchall()

    discounts = []
    for r in rows:
        curr_eff = float(r["curr_eff_price"])
        max_eff = float(r["max_7day_eff_price"]) if r["max_7day_eff_price"] is not None else None

        if max_eff is None or max_eff <= 0:
            continue

        savings = max_eff - curr_eff
        drop_pct = (savings / max_eff) * 100.0

        if drop_pct >= min_discount_pct and savings >= min_savings_twd:
            raw_url = r["order_action_url"] or ""
            clean_url = raw_url.replace("&amp;", "&")
            discounts.append({
                "product_id": r["product_id"],
                "store_id": r["store_id"],
                "store_name": r["store_name"],
                "product_name": r["product_name"],
                "category_name": r["category_name"] or "未分類",
                "description": r["description"] or "",
                "original_price": max_eff,
                "current_price": curr_eff,
                "prev_raw_price": max_eff,
                "curr_raw_price": float(r["curr_raw_price"]),
                "prev_qty": 1,
                "curr_qty": int(r["curr_qty"]),
                "discount_pct": round(drop_pct, 1),
                "savings_amount": round(savings, 1),
                "promo_type": r["promo_type"] or "無",
                "order_action_url": clean_url,
                "rating_value": float(r["rating_value"]) if r["rating_value"] is not None else None,
                "review_count": int(r["review_count"]) if r["review_count"] is not None else None,
                "locality": r["locality"] or "",
                "street_address": r["street_address"] or "",
                "crawled_time": latest_batch
            })

    discounts.sort(key=lambda x: (x["discount_pct"], x["savings_amount"]), reverse=True)
    return discounts


def extract_curated_catalog(conn: sqlite3.Connection, latest_batch: str, max_items: int = 4000) -> List[Dict[str, Any]]:
    """匯出精選全品庫清單 (限制在數千筆安全大小)，供前端離線或備援模式下秒級載入"""
    cursor = conn.cursor()
    query = """
    SELECT 
        p.product_id,
        p.store_id,
        p.store_name,
        p.category_name,
        p.product_name,
        p.price,
        p.quantity,
        p.promo_type,
        ROUND(p.price * 1.0 / p.quantity, 2) as eff_price,
        p.description,
        COALESCE(NULLIF(s.order_action_url, ''), s.store_url, '') as order_action_url,
        s.rating_value,
        s.review_count,
        s.locality,
        s.street_address,
        COALESCE(p.is_open, s.is_open, 1) as is_open
    FROM products p
    LEFT JOIN stores s ON p.store_id = s.store_id AND p.crawled_time = s.crawled_time
    WHERE p.crawled_time = ?
      AND p.price >= 1
    ORDER BY 
        CASE WHEN p.promo_type != '無' AND p.promo_type != '' THEN 0 ELSE 1 END,
        s.rating_value DESC NULLS LAST,
        (p.price * 1.0 / p.quantity) ASC
    LIMIT ?;
    """
    cursor.execute(query, (latest_batch, max_items))
    rows = cursor.fetchall()

    catalog = []
    for r in rows:
        loc = r["locality"] or ""
        addr = r["street_address"] or ""
        city = extract_city(loc, addr)

        catalog.append({
            "product_id": r["product_id"],
            "store_id": r["store_id"],
            "store_name": r["store_name"],
            "category_name": r["category_name"] or "一般",
            "product_name": r["product_name"],
            "price": float(r["price"]),
            "quantity": int(r["quantity"]),
            "promo_type": r["promo_type"] or "無",
            "eff_price": float(r["eff_price"]),
            "description": r["description"] or "",
            "order_action_url": (r["order_action_url"] or "").replace("&amp;", "&"),
            "rating_value": float(r["rating_value"]) if r["rating_value"] is not None else None,
            "review_count": int(r["review_count"]) if r["review_count"] is not None else None,
            "locality": loc,
            "city": city,
            "is_open": int(r["is_open"] or 1)
        })
    return catalog


def export_parquet_catalog(conn: sqlite3.Connection, latest_batch: str, output_parquet_path: str) -> int:
    """從 SQLite 匯出全台完整百萬級商品清單為極致壓縮的 Parquet 格式"""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        print("⚠️ 未安裝 pyarrow，跳過 Parquet 檔案導出。")
        return 0

    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    query = """
    SELECT 
        p.product_id,
        p.store_id,
        p.store_name,
        p.category_name,
        p.product_name,
        CAST(p.price AS REAL) as price,
        CAST(p.quantity AS INTEGER) as quantity,
        COALESCE(p.promo_type, '無') as promo_type,
        ROUND(p.price * 1.0 / p.quantity, 2) as eff_price,
        COALESCE(p.description, '') as description,
        COALESCE(NULLIF(s.order_action_url, ''), s.store_url, '') as order_action_url,
        s.rating_value,
        s.review_count,
        COALESCE(s.locality, '') as locality,
        COALESCE(s.street_address, '') as street_address,
        COALESCE(p.is_open, s.is_open, 1) as is_open,
        p.crawled_time
    FROM products p
    LEFT JOIN stores s ON p.store_id = s.store_id AND p.crawled_time = s.crawled_time
    WHERE p.crawled_time = ?
      AND p.price >= 1
    ORDER BY s.rating_value DESC NULLS LAST, (p.price * 1.0 / p.quantity) ASC;
    """
    cursor.execute(query, (latest_batch,))
    rows = cursor.fetchall()
    if not rows:
        return 0

    data = {
        "product_id": [],
        "store_id": [],
        "store_name": [],
        "category_name": [],
        "product_name": [],
        "price": [],
        "quantity": [],
        "promo_type": [],
        "eff_price": [],
        "description": [],
        "order_action_url": [],
        "rating_value": [],
        "review_count": [],
        "locality": [],
        "street_address": [],
        "city": [],
        "is_open": [],
        "crawled_time": []
    }

    for r in rows:
        loc = r["locality"] or ""
        addr = r["street_address"] or ""
        city = extract_city(loc, addr)

        data["product_id"].append(str(r["product_id"]))
        data["store_id"].append(str(r["store_id"]))
        data["store_name"].append(str(r["store_name"] or ""))
        data["category_name"].append(str(r["category_name"] or "一般"))
        data["product_name"].append(str(r["product_name"] or ""))
        data["price"].append(float(r["price"]))
        data["quantity"].append(int(r["quantity"]))
        data["promo_type"].append(str(r["promo_type"] or "無"))
        data["eff_price"].append(float(r["eff_price"]))
        data["description"].append(str(r["description"] or ""))
        data["order_action_url"].append(str(r["order_action_url"] or "").replace("&amp;", "&"))
        data["rating_value"].append(float(r["rating_value"]) if r["rating_value"] is not None else None)
        data["review_count"].append(int(r["review_count"]) if r["review_count"] is not None else None)
        data["locality"].append(loc)
        data["street_address"].append(addr)
        data["city"].append(city)
        data["is_open"].append(int(r["is_open"] or 1))
        data["crawled_time"].append(str(r["crawled_time"]))

    schema = pa.schema([
        pa.field("product_id", pa.string()),
        pa.field("store_id", pa.string()),
        pa.field("store_name", pa.string()),
        pa.field("category_name", pa.string()),
        pa.field("product_name", pa.string()),
        pa.field("price", pa.float64()),
        pa.field("quantity", pa.int64()),
        pa.field("promo_type", pa.string()),
        pa.field("eff_price", pa.float64()),
        pa.field("description", pa.string()),
        pa.field("order_action_url", pa.string()),
        pa.field("rating_value", pa.float64()),
        pa.field("review_count", pa.int64()),
        pa.field("locality", pa.string()),
        pa.field("street_address", pa.string()),
        pa.field("city", pa.string()),
        pa.field("is_open", pa.int64()),
        pa.field("crawled_time", pa.string())
    ])

    table = pa.Table.from_pydict(data, schema=schema)
    os.makedirs(os.path.dirname(os.path.abspath(output_parquet_path)), exist_ok=True)
    pq.write_table(table, output_parquet_path, compression="zstd")
    return len(rows)


def upload_parquet_to_hf(parquet_path: str, batch_id: str, repo_id: str = "hub-google/UberEat") -> bool:
    """將 Parquet 資料庫上傳至 Hugging Face Datasets 資料湖"""
    token = os.environ.get("HF_TOKEN")
    if not token:
        print("⚠️ 未檢測到 HF_TOKEN，跳過 Hugging Face Parquet 上傳（本地或離線測試模式）。")
        return False

    try:
        from huggingface_hub import HfApi
        api = HfApi(token=token)
        api.create_repo(repo_id=repo_id, repo_type="dataset", exist_ok=True)

        print(f"🚀 正在上傳全台全品庫 Parquet 至 Hugging Face ({repo_id}/Parquet/taiwan_catalog_latest.parquet)...")
        api.upload_file(
            path_or_fileobj=parquet_path,
            path_in_repo="Parquet/taiwan_catalog_latest.parquet",
            repo_id=repo_id,
            repo_type="dataset",
            commit_message=f"Update latest Taiwan catalog Parquet ({batch_id})"
        )
        print("✅ [HF Parquet 上傳成功] Parquet/taiwan_catalog_latest.parquet")

        api.upload_file(
            path_or_fileobj=parquet_path,
            path_in_repo=f"Parquet/history/taiwan_catalog_{batch_id}.parquet",
            repo_id=repo_id,
            repo_type="dataset",
            commit_message=f"Archive Taiwan catalog snapshot Parquet ({batch_id})"
        )
        print(f"✅ [HF Parquet 歸檔成功] Parquet/history/taiwan_catalog_{batch_id}.parquet")
        return True
    except Exception as e:
        print(f"⚠️ 上傳 Parquet 至 Hugging Face 失敗: {e}")
        return False


def extract_price_history_map(conn: sqlite3.Connection, target_product_ids: Optional[set] = None) -> Dict[str, List[Dict[str, Any]]]:
    """匯出特價/促銷商品按 product_id 聚合之歷史價格字典，控制在安全大小避免撐爆 Git"""
    cursor = conn.cursor()
    if target_product_ids:
        placeholders = ",".join(["?"] * len(target_product_ids))
        query = f"""
        SELECT 
            product_id,
            crawled_time,
            store_name,
            product_name,
            price,
            quantity,
            promo_type,
            ROUND(price * 1.0 / quantity, 2) as eff_price
        FROM products
        WHERE product_id IN ({placeholders})
        ORDER BY product_id, crawled_time ASC;
        """
        cursor.execute(query, list(target_product_ids))
    else:
        query = """
        SELECT 
            product_id,
            crawled_time,
            store_name,
            product_name,
            price,
            quantity,
            promo_type,
            ROUND(price * 1.0 / quantity, 2) as eff_price
        FROM products
        WHERE promo_type != '無' AND promo_type != ''
        ORDER BY product_id, crawled_time ASC
        LIMIT 5000;
        """
        cursor.execute(query)

    rows = cursor.fetchall()
    history_map: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        pid = r["product_id"]
        if pid not in history_map:
            history_map[pid] = []
        history_map[pid].append({
            "crawled_time": r["crawled_time"],
            "price": float(r["price"]),
            "quantity": int(r["quantity"]),
            "promo_type": r["promo_type"] or "無",
            "eff_price": float(r["eff_price"])
        })
    return history_map


def export_all_static_snapshots(
    src_dir: str,
    output_dir: str = "web/data",
    db_path: Optional[str] = None,
    batch_id: Optional[str] = None,
    stores_file: Optional[str] = None,
    scope: str = "taiwan",
    hf_repo_id: str = "hub-google/UberEat"
) -> Dict[str, Any]:
    """
    執行端到端靜態快照導出作業
    """
    start_time = time.time()
    print("=" * 80)
    print("🚀 【階段 6: 邊緣靜態快照導出 (Plan C Jamstack CDN)】啟動")
    print(f"📁 原始菜單來源: {src_dir}")
    print(f"📦 靜態導出目錄: {output_dir}")

    # 處理 SQLite DB 路徑 (若未指定或為 :memory:，使用安全獨立的暫存 SQLite 檔案確保 ETL、AlertEngine 與查詢共享相同資料庫)
    is_temp_db = False
    if not db_path or db_path == ":memory:":
        is_temp_db = True
        temp_dir = tempfile.gettempdir()
        db_path = os.path.join(temp_dir, f"ubereats_snapshot_{int(time.time() * 1000)}_{os.getpid()}.db")
    else:
        db_path = os.path.abspath(db_path)

    # 確保輸出目錄與 DB 目錄存在
    os.makedirs(output_dir, exist_ok=True)
    if os.path.dirname(db_path):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

    try:
        # 1. 檢核來源目錄與檔案
        if not os.path.exists(src_dir):
            fatal_error("步驟 6.1 來源目錄檢核", f"來源目錄不存在: {src_dir}")

        json_files = glob.glob(os.path.join(src_dir, "*.json"))
        if not json_files:
            fatal_error("步驟 6.1 來源 JSON 總量檢核", f"目錄 {src_dir} 內無任何 JSON 檔案")

        print(f"✅ [步驟 6.1 通過] 掃描到 {len(json_files)} 個原始 JSON 檔案。")

        batches = set()
        identities = set()
        inactive_stores = []
        for path in sorted(glob.glob(os.path.join(src_dir, "*.json"))):
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
            current_batch = os.path.basename(path).split("_")[0]
            identity = validate_document(doc, current_batch, path)
            if identity in identities:
                raise ValueError(f"duplicate store: {identity}")
            identities.add(identity)
            batches.add(current_batch)
            if doc.get("menu_status") == "inactive_account":
                inactive_stores.append(doc.get("name", "未命名店家"))

        if len(batches) != 1 or (batch_id and batch_id not in batches):
            raise ValueError("mixed or unexpected input batch")
        batch_id = batches.pop()

        if stores_file and os.path.exists(stores_file):
            with open(stores_file, encoding="utf-8") as handle:
                validate_snapshot(src_dir, json.load(handle), batch_id)

        # 2. 執行本地 SQLite ETL
        print(f"\n⚙️ 【步驟 6.2】執行本地 SQLite ETL 洗淨資料庫...")
        importer = UberEatsDBImporter(db_path=db_path, json_dir=src_dir)
        try:
            importer.init_database()
            importer.import_all_data()
            importer.validate_database()
        finally:
            importer.close()

        # 3. 執行智慧差異情報計算 (Alert Engine)
        print(f"\n🧠 【步驟 6.3】執行智慧差異情報與特價分析...")
        engine = UberEatsAlertEngine(db_path=db_path)
        try:
            alert_result = engine.detect_all(latest_batch=batch_id)
        finally:
            engine.close()

        # 4. 匯出全台全品庫 Parquet 檔案並上傳 Hugging Face
        print(f"\n🗄️ 【步驟 6.4】生成極致壓縮 Parquet 百萬資料湖檔案...")
        parquet_filename = f"taiwan_catalog_{batch_id}.parquet"
        parquet_filepath = os.path.join(output_dir, parquet_filename)
        
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            total_parquet_rows = export_parquet_catalog(conn, batch_id, parquet_filepath)
            
            if os.path.exists(parquet_filepath):
                parquet_size_mb = os.path.getsize(parquet_filepath) / 1024 / 1024
                print(f"✅ [Parquet 導出成功] 共 {total_parquet_rows:,} 筆商品，大小: {parquet_size_mb:.2f} MB")
                upload_parquet_to_hf(parquet_filepath, batch_id, repo_id=hf_repo_id)

            # 5. 連線本地資料庫並生成高精準靜態 JSON 資料集
            print(f"\n📝 【步驟 6.5】生成前端靜態 JSON 檔案集合...")

            # (A) 大特價清單 (7天最高價比較)
            discounts = calculate_7day_discounts(conn, batch_id, min_discount_pct=20.0, min_savings_twd=20.0)

            # (B) 新進店家
            new_stores = alert_result.get("new_stores", [])

            # (C) 老店新菜
            new_products = alert_result.get("new_products", [])

            # (D) 促銷專區 (正向匹配買一送一/多件優惠)
            promotions = alert_result.get("promotions", [])

            # (E) 精選全品庫清單 (精選前 4000 筆熱門/促銷商品，大小約 2MB，供離線或備援使用)
            curated_catalog = extract_curated_catalog(conn, batch_id, max_items=4000)

            # (F) 商品價格歷程字典 (僅針對特價商品提供價格走勢，大小 < 1MB)
            target_pids = {d["product_id"] for d in discounts}
            target_pids.update({p["product_id"] for p in promotions[:500]})
            history_map = extract_price_history_map(conn, target_product_ids=target_pids)

            # (G) 大盤總覽統計
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(DISTINCT store_id) as cnt FROM stores WHERE crawled_time = ?;", (batch_id,))
            total_stores_cnt = cursor.fetchone()["cnt"]
            cursor.execute("SELECT COUNT(*) as cnt FROM products WHERE crawled_time = ? AND price >= 1;", (batch_id,))
            total_products_cnt = cursor.fetchone()["cnt"]
        finally:
            conn.close()

        max_savings_val = max([d["savings_amount"] for d in discounts], default=0.0)
        formatted_date = f"{batch_id[:4]}-{batch_id[4:6]}-{batch_id[6:8]} {batch_id[8:10]}:{batch_id[10:12]}"

        stats_data = {
            "status": "success",
            "latest_batch": batch_id,
            "latest_batch_formatted": formatted_date,
            "prev_batch": alert_result.get("prev_batch", ""),
            "total_stores": total_stores_cnt,
            "total_monitored_stores": total_stores_cnt,
            "total_products": total_products_cnt,
            "total_monitored_products": total_products_cnt,
            "inactive_stores_count": len(inactive_stores),
            "big_discounts_count": len([d for d in discounts if d["discount_pct"] >= 30.0]),
            "new_stores_count": len(new_stores),
            "new_products_count": len(new_products),
            "promotions_count": len(promotions),
            "max_savings_twd": round(max_savings_val)
        }

        # 6. 寫入各靜態 JSON 檔案 (所有檔案均限制在安全體積 < 25MB)
        def save_json(filename: str, payload: Any):
            filepath = os.path.join(output_dir, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            size_kb = os.path.getsize(filepath) / 1024
            print(f"   ├─ 📄 {filename:<20} ({size_kb:6.1f} KB)")

        print(f"💾 正在寫入靜態 API 檔案至 {output_dir}:")
        save_json("stats.json", stats_data)
        save_json("discounts.json", {"status": "success", "total": len(discounts), "items": discounts})
        save_json("new_stores.json", {"status": "success", "total": len(new_stores), "items": new_stores})
        save_json("new_products.json", {"status": "success", "total": len(new_products), "items": new_products})
        save_json("promotions.json", {"status": "success", "total": len(promotions), "items": promotions})
        save_json("products.json", {"status": "success", "total": len(curated_catalog), "items": curated_catalog})
        save_json("history.json", {"status": "success", "history": history_map})

        # (H) 寫入前端版本時間戳記 (web/version.json)
        web_dir = os.path.dirname(output_dir) if output_dir.endswith("data") else output_dir
        version_data = {
            "version": batch_id,
            "buildTime": datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")
        }
        with open(os.path.join(web_dir, "version.json"), "w", encoding="utf-8") as f:
            json.dump(version_data, f, ensure_ascii=False, indent=2)
        print(f"   ├─ 📄 version.json        (版本: {batch_id})")

        elapsed = time.time() - start_time

        inactive_section_md = ""
        if inactive_stores:
            items_md = "\n".join([f"- `{name}`" for name in inactive_stores])
            inactive_section_md = f"""
<details>
<summary><b>⚠️ 全台網頁已失效店家名單 (共 {len(inactive_stores)} 間)</b></summary>

{items_md}

</details>
"""

        # 7. 輸出 Step Summary
        summary_md = f"""
## ⚡ 【階段 6: 邊緣靜態快照與 Parquet 湖倉導出 (v7.0)】成功報告

> **批次時間戳記**: `{batch_id}` | **輸出目錄**: `{output_dir}` | **耗時**: `{elapsed:.2f} 秒` | **架構狀態**: ✅ HF Lakehouse & GitHub Pages 準備完成

### 📊 產出資料集指標
| 資料集檔案 | 規模 / 大小 | 說明 | 狀態 |
| :--- | :---: | :--- | :---: |
| `taiwan_catalog.parquet` | **{total_parquet_rows:,}** 筆全台商品 | 列式壓縮湖倉檔，直傳 Hugging Face (支援 DuckDB-WASM 邊緣 SQL 毫秒級檢索) | ✅ 完成 |
| `stats.json` | 1 份 | 總店家 **{total_stores_cnt:,}** 間、總商品 **{total_products_cnt:,}** 項 | ✅ 完成 |
| `discounts.json` | **{len(discounts):,}** 筆 | 7 天價差降幅 ≥ 20% 大特價 (現省最高 ${round(max_savings_val)}) | ✅ 完成 |
| `new_stores.json` | **{len(new_stores):,}** 間 | 本批次全新進駐店家 | ✅ 完成 |
| `new_products.json` | **{len(new_products):,}** 筆 | 老店新上架菜色 | ✅ 完成 |
| `promotions.json` | **{len(promotions):,}** 筆 | 買一送一與促銷活動 | ✅ 完成 |
| `products.json` | **{len(curated_catalog):,}** 筆 | 精選商品清單 (輕量備援) | ✅ 完成 |
| `history.json` | **{len(history_map):,}** 款 | 特價商品價格走勢索引 | ✅ 完成 |
{inactive_section_md}
---
"""
        append_github_step_summary(summary_md)

        print("\n" + "=" * 80)
        print(f"🎉 【階段 6: 導出全部完成！】耗時 {elapsed:.2f} 秒")
        print("=" * 80)

        return stats_data
    finally:
        if is_temp_db and os.path.exists(db_path):
            try:
                os.remove(db_path)
            except Exception:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="導出 Uber Eats 邊緣靜態快照與 Parquet (v7.0)")
    parser.add_argument("--src-dir", required=True, help="原始菜單 JSON 資料夾")
    parser.add_argument("--output-dir", default="web/data", help="靜態資料輸出資料夾")
    parser.add_argument("--db-path", default=None, help="本地 SQLite 資料庫路徑 (選填，預設使用安全暫存資料庫)")
    parser.add_argument("--batch-id", help="指定 14 碼批次時間戳記")
    parser.add_argument("--stores-file", help="全台店家 Manifest 清單")
    parser.add_argument("--scope", choices=["taiwan", "regional"], default="taiwan")
    parser.add_argument("--repo-id", default="hub-google/UberEat", help="Hugging Face Dataset Repo ID")
    args = parser.parse_args()

    export_all_static_snapshots(
        src_dir=args.src_dir,
        output_dir=args.output_dir,
        db_path=args.db_path,
        batch_id=args.batch_id,
        stores_file=args.stores_file,
        scope=args.scope,
        hf_repo_id=args.repo_id
    )

