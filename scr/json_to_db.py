# -*- coding: utf-8 -*-
"""
外送平台價格與商品監控系統 (Uber Eats Price & Store Monitor)
第二階段：JSON 資料湖轉資料庫 ETL 系統 (JSON to SQLite DB Importer)
依據《外送平台價格與商品監控系統 系統需求規格書 (SRS)》第 3、4、5、6 節規範開發

【核心功能】
1. 統一時間戳機制：全資料庫 5 張表嚴格對齊 crawled_time (VARCHAR(14), YYYYMMDDhhmmss)。
2. 高效能 SQLite 結構：啟用 WAL 模式與外鍵約束 (PRAGMA foreign_keys = ON)。
3. 5 大實體表建立與寫入：
   - crawl_batches: 採集批次總表
   - stores: 店家時序快照表
   - products: 商品與價格時序快照表 (含智慧去重與分類辨識)
   - store_business_hours: 店家營業時間表
   - store_cuisines: 店家料理標籤表
4. 高效能時序索引建立 (複合主鍵 + crawled_time DESC 降序歷史索引)。
5. 全自動資料驗證機制 (外鍵約束、NOT NULL 檢查、格式防呆、時序價格變動比對)。
"""

import os
import sys
import json
import glob
import time
import html
import re
import sqlite3
import hashlib
from datetime import datetime, timezone, timedelta

TW_TZ = timezone(timedelta(hours=8))
from typing import Dict, List, Tuple, Any, Optional

# 確保標準輸出支援 UTF-8
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass


def get_md5_hash(text: str) -> str:
    """產生字串的 MD5 雜湊值 (32 碼小寫十六進位字串)"""
    return hashlib.md5(text.encode('utf-8')).hexdigest()


def normalize_time_str(t_str: Optional[str]) -> str:
    """標準化時間字串為 HH:MM:SS 格式"""
    if not t_str:
        return "00:00:00"
    t_str = t_str.strip()
    parts = t_str.split(":")
    if len(parts) == 2:
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}:00"
    elif len(parts) == 3:
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}:{int(parts[2]):02d}"
    return t_str


CHINESE_DIGIT_MAP = {
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5,
    "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5
}


def extract_promo_info(sec_name: Optional[str], product_name: Optional[str], description: Optional[str]) -> Tuple[str, int]:
    """
    從分類名稱、商品名稱、商品描述中萃取促銷活動類型 (如 買1送1) 與 實質取得數量 (quantity)
    回傳: (promo_type: str, quantity: int)
    """
    sec = sec_name or ""
    pname = product_name or ""
    desc = description or ""
    
    # 優先順序: 品名 > 分類 > 描述
    texts = [pname, sec, desc]
    
    # 1. 匹配中文「買X送Y」或「買 X 送 Y」
    pattern_buy_get = r"買\s*([1-9一二兩三四五])\s*送\s*([1-9一二兩三四五])"
    for text in texts:
        m = re.search(pattern_buy_get, text)
        if m:
            b_str, f_str = m.group(1), m.group(2)
            b_val = CHINESE_DIGIT_MAP.get(b_str, 1)
            f_val = CHINESE_DIGIT_MAP.get(f_str, 1)
            promo_type = f"買{b_val}送{f_val}"
            quantity = b_val + f_val
            return promo_type, quantity

    # 2. 匹配「買A送B」或「買 A 送 B」
    for text in texts:
        if re.search(r"買\s*[a-zA-Z]\s*送\s*[a-zA-Z]", text):
            return "買A送B", 2
            
    # 3. 匹配英文 "Buy X, get Y (free)" / "Buy X Get Y" / "Buy X Free Y" / "Buy 1, get 1 free"
    pattern_en = r"Buy\s*([1-9])\s*,?\s*(?:get|free)\s*([1-9])?"
    for text in texts:
        m = re.search(pattern_en, text, re.IGNORECASE)
        if m:
            b_val = int(m.group(1))
            f_val = int(m.group(2)) if m.group(2) else 1
            promo_type = f"買{b_val}送{f_val}"
            quantity = b_val + f_val
            return promo_type, quantity
            
    # 4. 匹配 "BOGO"
    for text in texts:
        if re.search(r"\bBOGO\b", text, re.IGNORECASE):
            return "買1送1", 2

    # 5. 匹配特定折扣與限時特惠 (quantity 為 1，但標註 promo_type)
    for text in [sec, pname]:
        m_disc = re.search(r"([1-9](?:\.[1-9])?)\s*折", text)
        if m_disc:
            return f"{m_disc.group(1)}折特惠", 1
        if any(k in text for k in ["限時優惠", "限時特惠", "活動優惠", "特惠專區", "優惠專區", "優惠活動", "Special Offer", "Special Deals"]):
            return "限時特惠", 1
            
    return "無", 1


class UberEatsDBImporter:
    """Uber Eats 資料庫匯入與驗證引擎"""

    def __init__(self, db_path: str, json_dir: str):
        self.db_path = os.path.abspath(db_path)
        self.json_dir = os.path.abspath(json_dir)
        self.conn: Optional[sqlite3.Connection] = None

    def connect(self) -> sqlite3.Connection:
        """建立資料庫連線並啟用外鍵與 WAL 模式"""
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self.conn.execute("PRAGMA journal_mode = WAL;")
        self.conn.execute("PRAGMA synchronous = NORMAL;")
        self.conn.row_factory = sqlite3.Row
        return self.conn

    def close(self):
        """關閉資料庫連線"""
        if self.conn:
            self.conn.close()
            self.conn = None

    def init_database(self):
        """
        初始化資料庫結構 (建立 5 張資料表與高效能時序查詢索引)
        符合《系統需求規格書 (SRS)》第 5 節 DDL 規範
        """
        conn = self.connect()
        cursor = conn.cursor()

        # 1. 採集批次總表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS crawl_batches (
            crawled_time VARCHAR(14) PRIMARY KEY, -- 格式: YYYYMMDDhhmmss
            benchmark_address VARCHAR(255) NOT NULL,
            benchmark_lat DECIMAL(10, 7) NOT NULL,
            benchmark_lon DECIMAL(10, 7) NOT NULL,
            total_discovered INT NOT NULL DEFAULT 0,
            success_count INT NOT NULL DEFAULT 0,
            fail_count INT NOT NULL DEFAULT 0
        );
        """)

        # 2. 店家資料時序快照表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS stores (
            store_id VARCHAR(32) NOT NULL,
            crawled_time VARCHAR(14) NOT NULL,
            store_name VARCHAR(255) NOT NULL,
            store_type VARCHAR(50) NOT NULL DEFAULT 'Restaurant',
            store_url VARCHAR(1000) NOT NULL,
            rating_value DECIMAL(3, 2),
            review_count INT,
            price_range VARCHAR(10),
            telephone VARCHAR(50),
            country_code VARCHAR(10) DEFAULT 'TW',
            region VARCHAR(50),
            locality VARCHAR(50),
            street_address VARCHAR(255),
            postal_code VARCHAR(20),
            latitude DECIMAL(10, 7),
            longitude DECIMAL(10, 7),
            order_action_url TEXT,
            total_menu_items INT NOT NULL DEFAULT 0,
            PRIMARY KEY (store_id, crawled_time),
            FOREIGN KEY (crawled_time) REFERENCES crawl_batches(crawled_time) ON DELETE CASCADE
        );
        """)

        # 3. 商品與價格時序快照表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            product_id VARCHAR(32) NOT NULL,
            crawled_time VARCHAR(14) NOT NULL,
            store_id VARCHAR(32) NOT NULL,
            store_name VARCHAR(255) NOT NULL,
            category_name VARCHAR(100),
            product_name VARCHAR(255) NOT NULL,
            price DECIMAL(10, 2) NOT NULL,
            currency VARCHAR(10) NOT NULL DEFAULT 'TWD',
            description TEXT,
            promo_type VARCHAR(50) NOT NULL DEFAULT '無',
            quantity INT NOT NULL DEFAULT 1,
            PRIMARY KEY (product_id, crawled_time),
            FOREIGN KEY (crawled_time) REFERENCES crawl_batches(crawled_time) ON DELETE CASCADE
        );
        """)

        # 防呆檢查：若現有資料庫缺少新欄位則自動擴充 (Schema Migration)
        cursor.execute("PRAGMA table_info(products);")
        p_cols = [c[1] for c in cursor.fetchall()]
        if "promo_type" not in p_cols and len(p_cols) > 0:
            cursor.execute("ALTER TABLE products ADD COLUMN promo_type VARCHAR(50) NOT NULL DEFAULT '無';")
        if "quantity" not in p_cols and len(p_cols) > 0:
            cursor.execute("ALTER TABLE products ADD COLUMN quantity INT NOT NULL DEFAULT 1;")

        # 4. 店家營業時間表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS store_business_hours (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id VARCHAR(32) NOT NULL,
            crawled_time VARCHAR(14) NOT NULL,
            day_of_week VARCHAR(20) NOT NULL,
            opens_at TIME NOT NULL,
            closes_at TIME NOT NULL,
            FOREIGN KEY (crawled_time) REFERENCES crawl_batches(crawled_time) ON DELETE CASCADE
        );
        """)

        # 5. 店家料理標籤表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS store_cuisines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id VARCHAR(32) NOT NULL,
            crawled_time VARCHAR(14) NOT NULL,
            cuisine_name VARCHAR(100) NOT NULL,
            FOREIGN KEY (crawled_time) REFERENCES crawl_batches(crawled_time) ON DELETE CASCADE
        );
        """)

        # 清理歷史重複資料 (若存在舊有重複紀錄則保留最小 id)
        cursor.execute("""
        DELETE FROM store_business_hours
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM store_business_hours
            GROUP BY store_id, crawled_time, day_of_week, opens_at, closes_at
        );
        """)
        cursor.execute("""
        DELETE FROM store_cuisines
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM store_cuisines
            GROUP BY store_id, crawled_time, cuisine_name
        );
        """)

        # 唯一性約束索引 (防止重複寫入相同快照的營業時間與標籤)
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_hours_unique ON store_business_hours (store_id, crawled_time, day_of_week, opens_at, closes_at);")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cuisines_unique ON store_cuisines (store_id, crawled_time, cuisine_name);")

        # 高效能時序查詢索引 (Time-Series Query Indexes)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_products_history ON products (product_id, crawled_time DESC);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_products_store_time ON products (store_id, crawled_time DESC);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_stores_history ON stores (store_id, crawled_time DESC);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_hours_store_time ON store_business_hours (store_id, crawled_time);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_cuisines_store_time ON store_cuisines (store_id, crawled_time);")

        # 修復歷史 stores 缺少 order_action_url 或包含 HTML 實體編碼問題
        cursor.execute("UPDATE stores SET order_action_url = store_url WHERE order_action_url IS NULL OR order_action_url = '';")
        cursor.execute("UPDATE stores SET order_action_url = REPLACE(order_action_url, '&amp;', '&') WHERE order_action_url LIKE '%&amp;%';")

        conn.commit()
        print("✅ [資料庫初始化] 5 張資料表與時序索引建立完成。")

    def import_all_data(self) -> Dict[str, int]:
        """
        掃描 JSON 目錄並執行全量 ETL 匯入
        回傳各表匯入總筆數
        """
        if not os.path.exists(self.json_dir):
            raise FileNotFoundError(f"JSON 目錄不存在: {self.json_dir}")

        all_files = os.listdir(self.json_dir)
        summary_files = sorted([f for f in all_files if f.endswith("_nearby_stores_summary.json")])
        store_files = sorted([
            f for f in all_files 
            if f.endswith(".json") 
            and not f.endswith("summary.json") 
            and not f.endswith("log.json")
            and len(f) >= 14 
            and f[:14].isdigit()
        ])

        print(f"\n📂 [掃描 JSON 資料湖]")
        print(f"   ├─ 發現批次摘要檔案 (Summary): {len(summary_files)} 個")
        print(f"   └─ 發現店家原始 JSON 檔案:     {len(store_files)} 個")

        conn = self.connect()
        cursor = conn.cursor()

        # -------------------------------------------------------------
        # 階段 1: 匯入批次總表 (crawl_batches)
        # -------------------------------------------------------------
        print(f"\n🚀 [階段 1/4] 匯入採集批次總表 (crawl_batches)...")
        batches_dict: Dict[str, Dict[str, Any]] = {}

        # 優先由 Summary JSON 讀取批次資訊
        for sf in summary_files:
            crawled_time = sf.split("_")[0]
            if len(crawled_time) != 14 or not crawled_time.isdigit():
                continue
            
            fpath = os.path.join(self.json_dir, sf)
            with open(fpath, "r", encoding="utf-8") as f:
                d = json.load(f)

            coords = d.get("coordinates", {})
            batches_dict[crawled_time] = {
                "crawled_time": crawled_time,
                "benchmark_address": d.get("benchmark_address", "台北市士林區中山北路七段81巷"),
                "benchmark_lat": float(coords.get("latitude", 25.1220568)),
                "benchmark_lon": float(coords.get("longitude", 121.5298302)),
                "total_discovered": int(d.get("total_discovered", 0)),
                "success_count": int(d.get("success_count", 0)),
                "fail_count": int(d.get("fail_count", 0))
            }

        # 確保所有店家 JSON 所屬之 crawled_time 均有對應批次記錄 (外鍵防呆)
        for st_file in store_files:
            c_time = st_file[:14]
            if c_time not in batches_dict:
                batches_dict[c_time] = {
                    "crawled_time": c_time,
                    "benchmark_address": "台北市士林區中山北路七段81巷",
                    "benchmark_lat": 25.1220568,
                    "benchmark_lon": 121.5298302,
                    "total_discovered": 0,
                    "success_count": 0,
                    "fail_count": 0
                }

        batch_rows = [
            (
                b["crawled_time"],
                b["benchmark_address"],
                b["benchmark_lat"],
                b["benchmark_lon"],
                b["total_discovered"],
                b["success_count"],
                b["fail_count"]
            )
            for b in sorted(batches_dict.values(), key=lambda x: x["crawled_time"])
        ]

        cursor.executemany("""
        INSERT OR REPLACE INTO crawl_batches (
            crawled_time, benchmark_address, benchmark_lat, benchmark_lon,
            total_discovered, success_count, fail_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?);
        """, batch_rows)
        conn.commit()

        print(f"   ↳ 成功寫入 {len(batch_rows)} 筆批次資訊。")

        # -------------------------------------------------------------
        # 階段 2: 解析與匯入店家、料理標籤、營業時間、菜單商品
        # -------------------------------------------------------------
        print(f"\n🚀 [階段 2/4] 解析並寫入店家快照、標籤、營業時間與商品明細...")
        
        stores_rows: List[Tuple] = []
        cuisines_rows: List[Tuple] = []
        hours_rows: List[Tuple] = []
        products_rows: List[Tuple] = []

        start_time = time.time()
        for idx, sf in enumerate(store_files, 1):
            crawled_time = sf[:14]
            fpath = os.path.join(self.json_dir, sf)
            
            with open(fpath, "r", encoding="utf-8") as f:
                try:
                    data = json.load(f)
                except Exception as e:
                    print(f"   ⚠️ 跳過損壞 JSON: {sf} ({e})")
                    continue

            store_url = data.get("@id") or ""
            if not store_url:
                continue

            store_id = get_md5_hash(store_url)
            store_name = html.unescape(str(data.get("name", "未命名店家"))).strip()
            store_type = data.get("@type", "Restaurant")
            price_range = data.get("priceRange")
            telephone = data.get("telephone")

            # 地址欄位解析
            address_obj = data.get("address")
            country_code = "TW"
            region = None
            locality = None
            street_address = None
            postal_code = None

            if isinstance(address_obj, dict):
                country_code = address_obj.get("addressCountry") or "TW"
                region = address_obj.get("addressRegion") or None
                locality = address_obj.get("addressLocality") or None
                street_address = address_obj.get("streetAddress") or None
                postal_code = address_obj.get("postalCode") or None

            # 座標欄位解析
            geo_obj = data.get("geo")
            latitude = None
            longitude = None
            if isinstance(geo_obj, dict):
                try:
                    latitude = float(geo_obj.get("latitude")) if geo_obj.get("latitude") is not None else None
                    longitude = float(geo_obj.get("longitude")) if geo_obj.get("longitude") is not None else None
                except (ValueError, TypeError):
                    pass

            # 評分與評論數解析
            rating_obj = data.get("aggregateRating")
            rating_value = None
            review_count = None
            if isinstance(rating_obj, dict):
                try:
                    if rating_obj.get("ratingValue") is not None:
                        rating_value = float(rating_obj.get("ratingValue"))
                    if rating_obj.get("reviewCount") is not None:
                        review_count = int(rating_obj.get("reviewCount"))
                except (ValueError, TypeError):
                    pass

            # 點餐網址
            order_action_url = None
            pot_action = data.get("potentialAction")
            if isinstance(pot_action, dict):
                target = pot_action.get("target")
                if isinstance(target, dict):
                    order_action_url = target.get("urlTemplate")

            if not order_action_url:
                order_action_url = store_url
            else:
                order_action_url = html.unescape(order_action_url).replace("&amp;", "&")

            # 料理標籤解析 (store_cuisines)
            cuisines_list = data.get("servesCuisine", [])
            seen_cuisines = set()
            if isinstance(cuisines_list, list):
                for c in cuisines_list:
                    if isinstance(c, str):
                        c_clean = c.strip()
                        if c_clean and c_clean not in seen_cuisines:
                            seen_cuisines.add(c_clean)
                            cuisines_rows.append((store_id, crawled_time, c_clean))

            # 營業時間解析 (store_business_hours)
            hours_list = data.get("openingHoursSpecification", [])
            seen_hours = set()
            if isinstance(hours_list, list):
                for h in hours_list:
                    if isinstance(h, dict):
                        opens = normalize_time_str(h.get("opens"))
                        closes = normalize_time_str(h.get("closes"))
                        dow = h.get("dayOfWeek", [])
                        if isinstance(dow, list):
                            for d in dow:
                                if isinstance(d, str) and d.strip():
                                    h_key = (store_id, crawled_time, d.strip(), opens, closes)
                                    if h_key not in seen_hours:
                                        seen_hours.add(h_key)
                                        hours_rows.append(h_key)
                        elif isinstance(dow, str) and dow.strip():
                            h_key = (store_id, crawled_time, dow.strip(), opens, closes)
                            if h_key not in seen_hours:
                                seen_hours.add(h_key)
                                hours_rows.append(h_key)

            # 菜單商品與價格解析 (products)
            # 支援同店家內跨分類去重：優先保留具體菜系分類與非空描述
            store_products: Dict[str, Dict[str, Any]] = {}
            has_menu = data.get("hasMenu")

            if isinstance(has_menu, dict):
                sections = has_menu.get("hasMenuSection", [])
                if isinstance(sections, list):
                    for sec in sections:
                        if not isinstance(sec, dict):
                            continue
                        sec_name = html.unescape(str(sec.get("name", ""))).strip()
                        items = sec.get("hasMenuItem", [])
                        if isinstance(items, list):
                            for item in items:
                                if not isinstance(item, dict):
                                    continue
                                raw_pname = item.get("name", "")
                                pname = html.unescape(str(raw_pname)).strip()
                                if not pname:
                                    continue

                                pid = get_md5_hash(f"{store_id}_{pname}")
                                
                                offers = item.get("offers", {})
                                p_raw = offers.get("price") if isinstance(offers, dict) else None
                                curr = offers.get("priceCurrency", "TWD") if isinstance(offers, dict) else "TWD"
                                if not curr:
                                    curr = "TWD"
                                
                                try:
                                    price_val = round(float(p_raw), 2) if p_raw is not None else 0.00
                                except (ValueError, TypeError):
                                    price_val = 0.00

                                # 排除價格 <= 0 的非商品廣告/公告項目 (例如店家提醒、買一送一備註等)
                                if price_val <= 0:
                                    continue

                                desc = html.unescape(str(item.get("description", ""))).strip() if item.get("description") else None
                                promo_type, qty = extract_promo_info(sec_name, pname, desc)

                                generic_sections = {
                                    "精選商品", "熱門商品", "人氣精選", "熱門", "Best sellers",
                                    "買 1 送 1", "買1送1", "買一送一", "促銷商品", "超值優惠",
                                    "Buy 1, get 1 free", "Buy 1 Get 1", "Buy 1 Free 1",
                                    "Offers", "Special Offer", "Special Deals", "限時優惠", "限時特惠", "活動優惠"
                                }
                                if pid in store_products:
                                    old_p = store_products[pid]
                                    # 若新舊任一處具有促銷活動標記，保留促銷活動與數量
                                    if promo_type != "無" and (old_p["promo_type"] == "無" or qty > old_p["quantity"]):
                                        old_p["promo_type"] = promo_type
                                        old_p["quantity"] = qty
                                        old_p["price"] = price_val

                                    # 若舊分類為通用推薦區塊，而新分類為具體類別，則更新為具體類別與描述
                                    if old_p["category_name"] in generic_sections and sec_name not in generic_sections and sec_name:
                                        old_p["category_name"] = sec_name
                                        if desc:
                                            old_p["description"] = desc
                                    # 若新分類亦為通用或新分類具體，但舊描述為空而新描述有值，補齊描述
                                    elif not old_p["description"] and desc:
                                        old_p["description"] = desc
                                else:
                                    store_products[pid] = {
                                        "product_id": pid,
                                        "crawled_time": crawled_time,
                                        "store_id": store_id,
                                        "store_name": store_name,
                                        "category_name": sec_name if sec_name else None,
                                        "product_name": pname,
                                        "price": price_val,
                                        "currency": curr,
                                        "description": desc,
                                        "promo_type": promo_type,
                                        "quantity": qty
                                    }

            # 轉換為商品寫入列
            for p in store_products.values():
                products_rows.append((
                    p["product_id"],
                    p["crawled_time"],
                    p["store_id"],
                    p["store_name"],
                    p["category_name"],
                    p["product_name"],
                    p["price"],
                    p["currency"],
                    p["description"],
                    p["promo_type"],
                    p["quantity"]
                ))

            # 店家快照寫入列 (商品總數以實際抓取之商品數為準)
            stores_rows.append((
                store_id,
                crawled_time,
                store_name,
                store_type,
                store_url,
                rating_value,
                review_count,
                price_range,
                telephone,
                country_code,
                region,
                locality,
                street_address,
                postal_code,
                latitude,
                longitude,
                order_action_url,
                len(store_products)
            ))

        # -------------------------------------------------------------
        # 階段 3: 執行批量寫入資料庫
        # -------------------------------------------------------------
        print(f"\n💾 [階段 3/4] 執行資料庫事務批量寫入 (Batch Insert)...")

        # 寫入 stores
        cursor.executemany("""
        INSERT OR REPLACE INTO stores (
            store_id, crawled_time, store_name, store_type, store_url,
            rating_value, review_count, price_range, telephone, country_code,
            region, locality, street_address, postal_code, latitude, longitude,
            order_action_url, total_menu_items
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, stores_rows)

        # 寫入 store_cuisines
        cursor.executemany("""
        INSERT OR IGNORE INTO store_cuisines (store_id, crawled_time, cuisine_name)
        VALUES (?, ?, ?);
        """, cuisines_rows)

        # 寫入 store_business_hours
        cursor.executemany("""
        INSERT OR IGNORE INTO store_business_hours (store_id, crawled_time, day_of_week, opens_at, closes_at)
        VALUES (?, ?, ?, ?, ?);
        """, hours_rows)

        # 寫入 products
        cursor.executemany("""
        INSERT OR REPLACE INTO products (
            product_id, crawled_time, store_id, store_name, category_name,
            product_name, price, currency, description, promo_type, quantity
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, products_rows)

        conn.commit()
        elapsed = time.time() - start_time

        stats = {
            "crawl_batches": len(batch_rows),
            "stores": len(stores_rows),
            "products": len(products_rows),
            "store_business_hours": len(hours_rows),
            "store_cuisines": len(cuisines_rows)
        }

        print(f"🎉 批量寫入完成！總耗時: {elapsed:.2f} 秒")
        print("📊 【資料表寫入統計】")
        print(f"   ├─ ① crawl_batches:        {stats['crawl_batches']:>6} 筆")
        print(f"   ├─ ② stores:               {stats['stores']:>6} 筆")
        print(f"   ├─ ③ products:             {stats['products']:>6} 筆")
        print(f"   ├─ ④ store_business_hours: {stats['store_business_hours']:>6} 筆")
        print(f"   └─ ⑤ store_cuisines:       {stats['store_cuisines']:>6} 筆")

        return stats

    def validate_database(self) -> Dict[str, Any]:
        """
        執行嚴格的資料庫格式、外鍵約束、NOT NULL 約束與時序比對驗證
        """
        conn = self.connect()
        cursor = conn.cursor()
        print("\n" + "=" * 90)
        print("🔍 【啟動全自動資料完整性與格式嚴格驗證 (Data Validation)】")
        print("=" * 90)

        validation_results = {
            "fk_check": True,
            "not_null_checks": {},
            "format_checks": {},
            "time_series_queries": {}
        }

        # 1. 外鍵約束檢查
        cursor.execute("PRAGMA foreign_key_check;")
        fk_errors = cursor.fetchall()
        if fk_errors:
            validation_results["fk_check"] = False
            print(f"❌ 外鍵約束檢查失敗！發現 {len(fk_errors)} 個孤兒記錄: {fk_errors}")
        else:
            print("✅ [外鍵約束檢驗] 100% 通過 (PRAGMA foreign_key_check 無任何異常)")

        # 2. 檢驗 NOT NULL 與關鍵欄位完整性
        tables_to_check = [
            ("crawl_batches", ["crawled_time", "benchmark_address", "benchmark_lat", "benchmark_lon"]),
            ("stores", ["store_id", "crawled_time", "store_name", "store_type", "store_url", "total_menu_items"]),
            ("products", ["product_id", "crawled_time", "store_id", "store_name", "product_name", "price", "currency", "promo_type", "quantity"]),
            ("store_business_hours", ["store_id", "crawled_time", "day_of_week", "opens_at", "closes_at"]),
            ("store_cuisines", ["store_id", "crawled_time", "cuisine_name"])
        ]

        for tbl, cols in tables_to_check:
            tbl_errors = []
            for col in cols:
                cursor.execute(f"SELECT COUNT(*) AS c FROM {tbl} WHERE {col} IS NULL;")
                null_count = cursor.fetchone()["c"]
                if null_count > 0:
                    tbl_errors.append(f"{col} 有 {null_count} 筆 NULL")
            if tbl_errors:
                validation_results["not_null_checks"][tbl] = tbl_errors
                print(f"❌ [{tbl}] 必填約束失敗: {', '.join(tbl_errors)}")
            else:
                validation_results["not_null_checks"][tbl] = "OK"
                print(f"✅ [{tbl:<22}] NOT NULL 約束檢查 100% 通過")

        # 3. 欄位格式與數值合理性檢查
        # (a) crawled_time 格式嚴格為 14 碼數字
        cursor.execute("""
        SELECT 
            (SELECT COUNT(*) FROM crawl_batches WHERE length(crawled_time) != 14) +
            (SELECT COUNT(*) FROM stores WHERE length(crawled_time) != 14) +
            (SELECT COUNT(*) FROM products WHERE length(crawled_time) != 14) +
            (SELECT COUNT(*) FROM store_business_hours WHERE length(crawled_time) != 14) +
            (SELECT COUNT(*) FROM store_cuisines WHERE length(crawled_time) != 14) AS invalid_times;
        """)
        inv_times = cursor.fetchone()["invalid_times"]
        validation_results["format_checks"]["crawled_time_14_digits"] = (inv_times == 0)
        print(f"✅ [時間戳格式] 全表 crawled_time 均為標準 14 碼 YYYYMMDDhhmmss: {'通過' if inv_times == 0 else '失敗'}")

        # (b) 評分範圍 (1.0 ~ 5.0 或 NULL)
        cursor.execute("SELECT COUNT(*) AS c FROM stores WHERE rating_value IS NOT NULL AND (rating_value < 1.0 OR rating_value > 5.0);")
        inv_ratings = cursor.fetchone()["c"]
        validation_results["format_checks"]["ratings_valid"] = (inv_ratings == 0)
        print(f"✅ [評分範圍] rating_value 介於 1.0 ~ 5.0: {'通過' if inv_ratings == 0 else '失敗'}")

        # (c) 價格不能為負數
        cursor.execute("SELECT COUNT(*) AS c FROM products WHERE price < 0;")
        inv_prices = cursor.fetchone()["c"]
        validation_results["format_checks"]["prices_non_negative"] = (inv_prices == 0)
        print(f"✅ [商品價格] price >= 0: {'通過' if inv_prices == 0 else '失敗'}")

        # (d) 數量必須 >= 1
        cursor.execute("SELECT COUNT(*) AS c FROM products WHERE quantity < 1;")
        inv_qtys = cursor.fetchone()["c"]
        validation_results["format_checks"]["quantity_positive"] = (inv_qtys == 0)
        print(f"✅ [商品數量] quantity >= 1: {'通過' if inv_qtys == 0 else '失敗'}")

        # 4. 時序比對 SQL 查詢驗證 (依 SRS 第 6 節)
        print("\n" + "-" * 90)
        print("📈 【時序商業查詢驗證 (SRS Section 6 Business Queries)】")
        print("-" * 90)

        # 6.1 商品價格波動歷史查詢
        print("▶ 查詢 1: 「海南雞腿飯」價格波動歷史 (SRS 6.1)")
        cursor.execute("""
        SELECT 
            store_name AS 店家名稱,
            product_name AS 商品名稱,
            price AS 抓取價格,
            currency AS 貨幣,
            crawled_time AS 抓取時間戳記
        FROM products
        WHERE product_name LIKE '%海南雞腿飯%'
        ORDER BY store_name, product_name, crawled_time DESC;
        """)
        h_rows = cursor.fetchall()
        for r in h_rows[:6]:
            print(f"   [{r['抓取時間戳記']}] {r['店家名稱']:<20} | {r['商品名稱']:<35} | {r['貨幣']} {r['抓取價格']}")
        if len(h_rows) > 6:
            print(f"   ... 共取得 {len(h_rows)} 筆歷史價格記錄。")

        # 6.2 跨日調價商品比對 (20260824 vs 20260825)
        print("\n▶ 查詢 2: 2026-08-24 與 2026-08-25 價格調漲/調降明細 (SRS 6.2)")
        cursor.execute("""
        SELECT 
            p25.store_name AS 店家名稱,
            p25.product_name AS 商品名稱,
            p24.price AS [2026-08-24 價格],
            p25.price AS [2026-08-25 價格],
            (p25.price - p24.price) AS 價差,
            ROUND(((p25.price - p24.price) * 100.0 / p24.price), 2) AS 漲跌幅百分比,
            p24.crawled_time AS 前次抓取時間,
            p25.crawled_time AS 本次抓取時間
        FROM products p25
        JOIN products p24 
          ON p25.product_id = p24.product_id
        WHERE p24.crawled_time = '20260824161056'
          AND p25.crawled_time = '20260825161056'
          AND p25.price != p24.price
        ORDER BY 價差 DESC;
        """)
        diff_rows = cursor.fetchall()
        print(f"   ↳ 偵測到 {len(diff_rows)} 項商品發生跨日價格變動：")
        print(f"   {'店家名稱':<18} | {'商品名稱':<30} | {'08/24 價格':>9} | {'08/25 價格':>9} | {'價差':>7} | {'漲跌幅':>8}")
        print("   " + "-" * 92)
        for r in diff_rows:
            p24 = float(r["2026-08-24 價格"])
            p25 = float(r["2026-08-25 價格"])
            diff = float(r["價差"])
            pct = float(r["漲跌幅百分比"])
            print(f"   {r['店家名稱'][:16]:<18} | {r['商品名稱'][:28]:<30} | {p24:>9.2f} | {p25:>9.2f} | {diff:>+7.2f} | {pct:>+7.2f}%")

        # 6.3 店家評分歷史查詢 (SRS 6.3)
        print("\n▶ 查詢 3: 店家評分與評論時序快照 (SRS 6.3)")
        cursor.execute("""
        SELECT 
            store_name AS 店家名稱,
            crawled_time AS 抓取時間戳記,
            rating_value AS 當時星等評分,
            review_count AS 當時評論數量
        FROM stores
        WHERE store_name LIKE '%雙月食品社%' OR store_name LIKE '%鼎泰豐%'
        ORDER BY store_name, crawled_time ASC;
        """)
        s_rows = cursor.fetchall()
        for r in s_rows:
            print(f"   [{r['抓取時間戳記']}] {r['店家名稱']:<25} | ⭐ {r['當時星等評分']} ({r['當時評論數量']} 則評論)")

        # 6.4 促銷買幾送幾商品與實質單件價格查詢 (SRS 6.4)
        print("\n▶ 查詢 4: 買一送一 / 買幾送幾促銷商品與實質單價 (SRS 6.4)")
        cursor.execute("""
        SELECT 
            store_name AS 店家名稱,
            category_name AS 分類名稱,
            product_name AS 商品名稱,
            price AS 平台標價,
            promo_type AS 促銷活動,
            quantity AS 取得數量,
            ROUND(price / quantity, 2) AS 實質單件價格,
            crawled_time AS 抓取時間
        FROM products
        WHERE promo_type != '無' AND crawled_time = (SELECT MAX(crawled_time) FROM crawl_batches)
        ORDER BY price DESC
        LIMIT 10;
        """)
        promo_rows = cursor.fetchall()
        print(f"   {'促銷類型':<8} | {'數量':>4} | {'店家名稱':<18} | {'商品名稱':<26} | {'平台標價':>8} | {'實質單價':>8}")
        print("   " + "-" * 92)
        for r in promo_rows:
            p_val = float(r["平台標價"])
            u_val = float(r["實質單件價格"])
            print(f"   {r['促銷活動']:<8} | {r['取得數量']:>4} | {r['店家名稱'][:16]:<18} | {r['商品名稱'][:24]:<26} | ${p_val:>7.2f} | ${u_val:>7.2f}")

        # 統計全庫促銷活動分佈
        cursor.execute("""
        SELECT promo_type, COUNT(*) AS count, AVG(quantity) AS avg_qty
        FROM products
        WHERE crawled_time = (SELECT MAX(crawled_time) FROM crawl_batches)
        GROUP BY promo_type
        ORDER BY count DESC;
        """)
        summary_rows = cursor.fetchall()
        print("\n   📊 最新批次促銷類型分佈統計:")
        for sr in summary_rows:
            print(f"      - {sr['promo_type']:<10}: {sr['count']:>5} 項商品 (平均取得數量: {sr['avg_qty']:.1f})")

        # 嚴格校驗判定：若外鍵錯誤或 NOT NULL 約束失敗，直接拋錯中斷
        errors = []
        if not validation_results["fk_check"]:
            errors.append(f"外鍵約束檢查失敗，存在孤兒記錄: {fk_errors}")
        for tbl, status in validation_results["not_null_checks"].items():
            if status != "OK":
                errors.append(f"資料表 [{tbl}] NOT NULL 約束違規: {status}")
        if not validation_results["format_checks"]["crawled_time_14_digits"]:
            errors.append("時間戳記格式非 14 碼標準數字")
        if not validation_results["format_checks"]["prices_non_negative"]:
            errors.append("檢測到負數商品價格")
        if not validation_results["format_checks"]["quantity_positive"]:
            errors.append("檢測到 quantity < 1 之異常商品數量")

        if errors:
            err_msg = "【資料庫完整性檢核失敗】" + "; ".join(errors)
            print(f"\n❌ {err_msg}\n", file=sys.stderr)
            raise RuntimeError(err_msg)

        print("=" * 90)
        return validation_results


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(script_dir)
    base_dir = parent_dir if os.path.exists(os.path.join(parent_dir, "JSON")) or os.path.exists(os.path.join(parent_dir, "ubereats_monitor.db")) else script_dir
    json_dir = os.path.join(base_dir, "JSON")
    db_path = os.path.join(base_dir, "ubereats_monitor.db")

    # 支援自訂命令列參數
    if len(sys.argv) > 1:
        for arg in sys.argv[1:]:
            if arg.endswith(".db"):
                db_path = os.path.abspath(arg)
            elif os.path.isdir(arg):
                json_dir = os.path.abspath(arg)

    print("=" * 90)
    print("🚀 【外送平台價格與商品監控系統 (Uber Eats Price & Store Monitor)】")
    print("📌 第二階段 ETL 轉檔模組: JSON 資料湖 ➔ 時序關聯資料庫 (SQLite)")
    print(f"⏰ 執行時間: {datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"📁 來源目錄: {json_dir}")
    print(f"🗄️ 目標資料庫: {db_path}")
    print("=" * 90)

    importer = UberEatsDBImporter(db_path=db_path, json_dir=json_dir)
    try:
        # 1. 建立資料庫與時序索引
        importer.init_database()

        # 2. 執行全量 ETL 匯入
        importer.import_all_data()

        # 3. 執行嚴格資料品質與時序比對驗證
        importer.validate_database()

        print(f"\n🎉 【ETL 轉檔與資料驗證全部成功完成！】")
        print(f"💾 資料庫檔案位置: {db_path} ({os.path.getsize(db_path) / (1024*1024):.2f} MB)")
        print("=" * 90)
    finally:
        importer.close()


if __name__ == "__main__":
    main()
