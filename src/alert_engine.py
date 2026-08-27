# -*- coding: utf-8 -*-
"""
外送平台價格與商品監控系統 (Uber Eats Price & Store Monitor)
智慧差異情報分析引擎 (Alert & Difference Detection Engine)

【核心功能】
1. 建立並維護 alerts_history 表，具備防重複推播與去重機制。
2. 偵測單日大特價 (Big Discount): 降幅 >= 30% 且現省金額 >= 20 元 (支援實質單價與買1送1換算)。
3. 偵測全新進駐店家 (New Stores): 歷史首度出現之店家。
4. 偵測老店新上架商品 (New Products): 既有老店新推出的全新菜色。
5. 匯出預先計算好的靜態資料 (dashboard_data.js & dashboard_data.json)，支援離線直接開啟 HTML。
"""

import os
import sys
import re
import json
import sqlite3
from datetime import datetime, timezone, timedelta

TW_TZ = timezone(timedelta(hours=8))
from typing import Dict, List, Tuple, Any, Optional

# 確保標準輸出支援 UTF-8
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_DB_PATH = (
    os.path.join(PROJECT_ROOT, "data", "db", "ubereats_monitor.db")
    if os.path.exists(os.path.join(PROJECT_ROOT, "data", "db", "ubereats_monitor.db"))
    else os.path.join(PROJECT_ROOT, "ubereats_monitor.db")
)
DEFAULT_WEB_DIR = os.path.join(PROJECT_ROOT, "web") if os.path.exists(os.path.join(PROJECT_ROOT, "web")) else PROJECT_ROOT


class UberEatsAlertEngine:
    """Uber Eats 差異偵測與特價/新品情報引擎"""

    def __init__(self, db_path: Optional[str] = None, output_dir: Optional[str] = None):
        self.db_path = db_path if db_path == ":memory:" else (os.path.abspath(db_path) if db_path else DEFAULT_DB_PATH)
        self.output_dir = os.path.abspath(output_dir) if output_dir else DEFAULT_WEB_DIR
        self.conn: Optional[sqlite3.Connection] = None

    def connect(self) -> sqlite3.Connection:
        """建立資料庫連線"""
        if self.conn is not None:
            return self.conn
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self.conn.execute("PRAGMA journal_mode = WAL;")
        self.conn.row_factory = sqlite3.Row
        return self.conn

    def close(self):
        """關閉資料庫連線"""
        if self.conn:
            self.conn.close()
            self.conn = None

    def init_alert_tables(self):
        """初始化 alerts_history 資料表結構與索引"""
        conn = self.connect()
        cursor = conn.cursor()

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS alerts_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_type VARCHAR(20) NOT NULL,    -- 'BIG_DISCOUNT', 'NEW_STORE', 'NEW_PRODUCT', 'PROMO_BOGO'
            target_id VARCHAR(64) NOT NULL,     -- store_id 或 product_id
            store_id VARCHAR(32) NOT NULL,
            store_name VARCHAR(255) NOT NULL,
            product_name VARCHAR(255),
            category_name VARCHAR(100),
            original_price DECIMAL(10, 2),
            current_price DECIMAL(10, 2),
            discount_pct DECIMAL(5, 2),
            savings_amount DECIMAL(10, 2),
            promo_type VARCHAR(50) DEFAULT '無',
            order_action_url TEXT,
            crawled_time VARCHAR(14) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(alert_type, target_id, crawled_time)
        );
        """)

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_alerts_time ON alerts_history (crawled_time DESC, alert_type);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_alerts_discount ON alerts_history (discount_pct DESC);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_alerts_store ON alerts_history (store_id, crawled_time);")

        conn.commit()

    def get_available_batches(self) -> List[str]:
        """取得資料庫中所有的採集批次時間戳記 (由新到舊排序)"""
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='products';")
        if not cursor.fetchone():
            return []
        cursor.execute("SELECT DISTINCT crawled_time FROM products ORDER BY crawled_time DESC;")
        rows = cursor.fetchall()
        return [r["crawled_time"] for r in rows]

    def detect_all(
        self,
        latest_batch: Optional[str] = None,
        prev_batch: Optional[str] = None,
        min_discount_pct: float = 30.0,
        min_savings_twd: float = 20.0
    ) -> Dict[str, Any]:
        """
        執行全方位情報差異分析
        1. 大特價 (降價 >= min_discount_pct% 且現省 >= min_savings_twd)
        2. 全新進駐店家
        3. 老店新上架菜色
        4. 買一送一促銷專區
        """
        self.init_alert_tables()
        conn = self.connect()

        batches = self.get_available_batches()
        if not batches:
            print("⚠️ 資料庫中尚無任何商品快照資料。")
            return {
                "latest_batch": "",
                "prev_batch": "",
                "big_discounts": [],
                "new_stores": [],
                "new_products": [],
                "promotions": [],
                "stats": {}
            }

        if latest_batch is None:
            latest_batch = batches[0]
        if prev_batch is None:
            prev_batch = batches[1] if len(batches) > 1 else None

        print(f"\n🔍 [執行情報分析引擎]")
        print(f"   ├─ 當前分析批次: {latest_batch}")
        print(f"   ├─ 前次比對批次: {prev_batch if prev_batch else '無 (首個批次)'}")
        print(f"   ├─ 大特價門檻:   降幅 >= {min_discount_pct}% 且現省 >= ${min_savings_twd}")

        # 1. 偵測大特價 (Big Discounts)
        big_discounts = []
        if prev_batch:
            big_discounts = self._detect_big_discounts(
                conn, latest_batch, prev_batch, min_discount_pct, min_savings_twd
            )
        print(f"   ├─ 發現降價 >= {min_discount_pct}% 商品: {len(big_discounts)} 筆")

        # 2. 偵測全新進駐店家 (New Stores)
        new_stores = self._detect_new_stores(conn, latest_batch)
        print(f"   ├─ 發現全新進駐店家: {len(new_stores)} 間")

        # 3. 偵測老店新上架菜色 (New Products)
        new_products = self._detect_new_products(conn, latest_batch)
        print(f"   ├─ 發現老店新上架菜色: {len(new_products)} 筆")

        # 4. 偵測買一送一/促銷專區 (Promotions)
        promotions = self._detect_promotions(conn, latest_batch)
        print(f"   └─ 發現促銷優惠商品: {len(promotions)} 筆")

        # 5. 寫入 alerts_history 表
        self._save_alerts_to_db(conn, latest_batch, big_discounts, new_stores, new_products, promotions)

        # 6. 計算整體統計指標
        stats = self._calculate_overall_stats(
            conn, latest_batch, prev_batch, big_discounts, new_stores, new_products, promotions
        )

        result = {
            "latest_batch": latest_batch,
            "latest_batch_formatted": stats.get("latest_batch_formatted", latest_batch),
            "prev_batch": prev_batch,
            "batches_list": batches,
            "min_discount_pct": min_discount_pct,
            "min_savings_twd": min_savings_twd,
            "big_discounts": big_discounts,
            "new_stores": new_stores,
            "new_products": new_products,
            "promotions": promotions,
            "stats": stats,
            "generated_at": datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")
        }

        return result

    def _detect_big_discounts(
        self,
        conn: sqlite3.Connection,
        latest_batch: str,
        prev_batch: str,
        min_discount_pct: float,
        min_savings_twd: float
    ) -> List[Dict[str, Any]]:
        """偵測兩批次間降價幅度達標的大特價商品"""
        cursor = conn.cursor()
        query = """
        SELECT 
            p1.product_id,
            p1.store_id,
            p1.store_name,
            p1.product_name,
            p1.category_name,
            p1.description,
            p0.price as prev_raw_price,
            p0.quantity as prev_qty,
            p0.promo_type as prev_promo,
            ROUND(p0.price * 1.0 / p0.quantity, 2) as prev_eff_price,
            p1.price as curr_raw_price,
            p1.quantity as curr_qty,
            p1.promo_type as curr_promo,
            ROUND(p1.price * 1.0 / p1.quantity, 2) as curr_eff_price,
            COALESCE(NULLIF(s.order_action_url, ''), s.store_url, '') as order_action_url,
            s.rating_value,
            s.review_count,
            s.locality,
            s.street_address
        FROM products p1
        JOIN products p0 ON p1.product_id = p0.product_id AND p1.store_id = p0.store_id
        LEFT JOIN stores s ON p1.store_id = s.store_id AND p1.crawled_time = s.crawled_time
        WHERE p0.crawled_time = ?
          AND p1.crawled_time = ?
          AND p0.price > 0
          AND p1.price > 0
          AND (p0.is_open = 1 OR p0.is_open IS NULL)
          AND (p1.is_open = 1 OR p1.is_open IS NULL)
        """
        cursor.execute(query, (prev_batch, latest_batch))
        rows = cursor.fetchall()

        discounts = []
        for r in rows:
            prev_eff = float(r["prev_eff_price"])
            curr_eff = float(r["curr_eff_price"])

            if prev_eff <= 0:
                continue

            savings = prev_eff - curr_eff
            drop_pct = (savings / prev_eff) * 100.0

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
                    "original_price": prev_eff,
                    "current_price": curr_eff,
                    "prev_raw_price": float(r["prev_raw_price"]),
                    "curr_raw_price": float(r["curr_raw_price"]),
                    "prev_qty": int(r["prev_qty"]),
                    "curr_qty": int(r["curr_qty"]),
                    "promo_type": r["curr_promo"],
                    "discount_pct": round(drop_pct, 1),
                    "savings_amount": round(savings, 1),
                    "order_action_url": clean_url,
                    "rating_value": float(r["rating_value"]) if r["rating_value"] is not None else None,
                    "review_count": int(r["review_count"]) if r["review_count"] is not None else None,
                    "locality": r["locality"] or "",
                    "street_address": r["street_address"] or "",
                    "crawled_time": latest_batch
                })

        # 依降幅排序 (降幅最高在前，同降幅則現省最多在前)
        discounts.sort(key=lambda x: (x["discount_pct"], x["savings_amount"]), reverse=True)
        return discounts

    def _detect_new_stores(self, conn: sqlite3.Connection, latest_batch: str) -> List[Dict[str, Any]]:
        """偵測最新批次首度進駐的全新店家"""
        cursor = conn.cursor()
        query = """
        SELECT 
            s1.store_id,
            s1.store_name,
            s1.store_type,
            s1.store_url,
            s1.rating_value,
            s1.review_count,
            s1.price_range,
            s1.telephone,
            s1.region,
            s1.locality,
            s1.street_address,
            COALESCE(NULLIF(s1.order_action_url, ''), s1.store_url, '') as order_action_url,
            s1.total_menu_items,
            s1.crawled_time,
            (
                SELECT GROUP_CONCAT(cuisine_name, '、')
                FROM store_cuisines sc
                WHERE sc.store_id = s1.store_id AND sc.crawled_time = s1.crawled_time
            ) as cuisines
        FROM stores s1
        WHERE s1.crawled_time = ?
          AND s1.store_id NOT IN (
              SELECT DISTINCT s0.store_id
              FROM stores s0
              WHERE s0.crawled_time < ?
          )
        ORDER BY s1.rating_value DESC, s1.total_menu_items DESC;
        """
        cursor.execute(query, (latest_batch, latest_batch))
        rows = cursor.fetchall()

        new_stores = []
        for r in rows:
            raw_url = r["order_action_url"] or r["store_url"] or ""
            new_stores.append({
                "store_id": r["store_id"],
                "store_name": r["store_name"],
                "store_type": r["store_type"],
                "store_url": (r["store_url"] or "").replace("&amp;", "&"),
                "rating_value": float(r["rating_value"]) if r["rating_value"] is not None else None,
                "review_count": int(r["review_count"]) if r["review_count"] is not None else None,
                "price_range": r["price_range"] or "$",
                "telephone": r["telephone"] or "",
                "locality": r["locality"] or "",
                "street_address": r["street_address"] or "",
                "order_action_url": raw_url.replace("&amp;", "&"),
                "total_menu_items": int(r["total_menu_items"] or 0),
                "cuisines": r["cuisines"] or "熱門餐飲",
                "crawled_time": latest_batch
            })
        return new_stores

    def _detect_new_products(self, conn: sqlite3.Connection, latest_batch: str) -> List[Dict[str, Any]]:
        """偵測既有老店推出的全新菜色"""
        cursor = conn.cursor()
        query = """
        SELECT 
            p1.product_id,
            p1.store_id,
            p1.store_name,
            p1.category_name,
            p1.product_name,
            p1.price,
            p1.currency,
            p1.description,
            p1.promo_type,
            p1.quantity,
            ROUND(p1.price * 1.0 / p1.quantity, 2) as eff_price,
            COALESCE(NULLIF(s.order_action_url, ''), s.store_url, '') as order_action_url,
            s.rating_value
        FROM products p1
        JOIN stores s ON p1.store_id = s.store_id AND p1.crawled_time = s.crawled_time
        WHERE p1.crawled_time = ?
          AND p1.price > 0
          AND p1.product_id NOT IN (
              SELECT DISTINCT p0.product_id
              FROM products p0
              WHERE p0.crawled_time < ?
          )
          AND p1.store_id IN (
              SELECT DISTINCT s0.store_id
              FROM stores s0
              WHERE s0.crawled_time < ?
          )
        ORDER BY p1.store_name, p1.price DESC;
        """
        cursor.execute(query, (latest_batch, latest_batch, latest_batch))
        rows = cursor.fetchall()

        new_prods = []
        for r in rows:
            raw_url = r["order_action_url"] or ""
            new_prods.append({
                "product_id": r["product_id"],
                "store_id": r["store_id"],
                "store_name": r["store_name"],
                "category_name": r["category_name"] or "未分類",
                "product_name": r["product_name"],
                "price": float(r["price"]),
                "quantity": int(r["quantity"]),
                "promo_type": r["promo_type"],
                "eff_price": float(r["eff_price"]),
                "currency": r["currency"] or "TWD",
                "description": r["description"] or "",
                "order_action_url": raw_url.replace("&amp;", "&"),
                "rating_value": float(r["rating_value"]) if r["rating_value"] is not None else None,
                "crawled_time": latest_batch
            })
        return new_prods

    def _detect_promotions(self, conn: sqlite3.Connection, latest_batch: str) -> List[Dict[str, Any]]:
        """偵測最新批次中包含買1送1、買2送1等促銷優惠 (排除價格 <= 0 的廣告文案)"""
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
            s.locality
        FROM products p
        LEFT JOIN stores s ON p.store_id = s.store_id AND p.crawled_time = s.crawled_time
        WHERE p.crawled_time = ?
          AND p.quantity > 1
          AND p.price > 0
        ORDER BY p.promo_type DESC, (p.price * 1.0 / p.quantity) ASC;
        """
        cursor.execute(query, (latest_batch,))
        rows = cursor.fetchall()

        promos = []
        for r in rows:
            price = float(r["price"])
            qty = int(r["quantity"])
            promo_t = r["promo_type"] or ""
            
            # 精確計算買X送Y與促銷實質單價
            eff = float(r["eff_price"])
            m_buy = re.search(r"買\s*([0-9一二兩三四五])\s*送\s*([0-9一二兩三四五])", promo_t)
            if m_buy:
                digit_map = {'1': 1, '2': 2, '3': 3, '4': 4, '5': 5, '一': 1, '二': 2, '兩': 2, '三': 3, '四': 4, '五': 5}
                b = digit_map.get(m_buy.group(1), 1)
                f = digit_map.get(m_buy.group(2), 1)
                if b + f > 0:
                    eff = round((price * b) / (b + f), 2)
            elif re.search(r"第\s*[2二兩]\s*[件杯項份]\s*半價", promo_t):
                eff = round((price * 1.5) / 2.0, 2)

            raw_url = r["order_action_url"] or ""
            promos.append({
                "product_id": r["product_id"],
                "store_id": r["store_id"],
                "store_name": r["store_name"],
                "category_name": r["category_name"] or "促銷特區",
                "product_name": r["product_name"],
                "price": price,
                "quantity": qty,
                "promo_type": r["promo_type"],
                "eff_price": eff,
                "description": r["description"] or "",
                "order_action_url": raw_url.replace("&amp;", "&"),
                "rating_value": float(r["rating_value"]) if r["rating_value"] is not None else None,
                "locality": r["locality"] or "",
                "crawled_time": latest_batch
            })
        return promos

    def _save_alerts_to_db(
        self,
        conn: sqlite3.Connection,
        latest_batch: str,
        big_discounts: List[Dict[str, Any]],
        new_stores: List[Dict[str, Any]],
        new_products: List[Dict[str, Any]],
        promotions: List[Dict[str, Any]]
    ):
        """將警報寫入 alerts_history 表 (支援 INSERT OR REPLACE 去重)"""
        cursor = conn.cursor()
        
        # 1. 寫入大特價
        for d in big_discounts:
            cursor.execute("""
            INSERT OR REPLACE INTO alerts_history (
                alert_type, target_id, store_id, store_name, product_name, category_name,
                original_price, current_price, discount_pct, savings_amount, promo_type,
                order_action_url, crawled_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                "BIG_DISCOUNT",
                d["product_id"],
                d["store_id"],
                d["store_name"],
                d["product_name"],
                d["category_name"],
                d["original_price"],
                d["current_price"],
                d["discount_pct"],
                d["savings_amount"],
                d["promo_type"],
                d["order_action_url"],
                latest_batch
            ))

        # 2. 寫入新進店家
        for s in new_stores:
            cursor.execute("""
            INSERT OR REPLACE INTO alerts_history (
                alert_type, target_id, store_id, store_name, product_name, category_name,
                original_price, current_price, discount_pct, savings_amount, promo_type,
                order_action_url, crawled_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                "NEW_STORE",
                s["store_id"],
                s["store_id"],
                s["store_name"],
                f"【全新進駐】共 {s['total_menu_items']} 道菜品",
                s["cuisines"],
                None,
                None,
                None,
                None,
                "新店家",
                s["order_action_url"],
                latest_batch
            ))

        # 3. 寫入老店新品
        for p in new_products:
            cursor.execute("""
            INSERT OR REPLACE INTO alerts_history (
                alert_type, target_id, store_id, store_name, product_name, category_name,
                original_price, current_price, discount_pct, savings_amount, promo_type,
                order_action_url, crawled_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                "NEW_PRODUCT",
                p["product_id"],
                p["store_id"],
                p["store_name"],
                p["product_name"],
                p["category_name"],
                None,
                p["price"],
                None,
                None,
                p["promo_type"],
                p["order_action_url"],
                latest_batch
            ))

        conn.commit()

    def _calculate_overall_stats(
        self,
        conn: sqlite3.Connection,
        latest_batch: str,
        prev_batch: Optional[str],
        big_discounts: List[Dict[str, Any]],
        new_stores: List[Dict[str, Any]],
        new_products: List[Dict[str, Any]],
        promotions: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """計算儀表板頂部統計指標"""
        cursor = conn.cursor()

        # 總店家數
        cursor.execute("SELECT COUNT(DISTINCT store_id) as cnt FROM stores WHERE crawled_time = ?;", (latest_batch,))
        total_stores = cursor.fetchone()["cnt"]

        # 總商品數 (排除 price <= 0 之非商品公告)
        cursor.execute("SELECT COUNT(*) as cnt FROM products WHERE crawled_time = ? AND price > 0;", (latest_batch,))
        total_products = cursor.fetchone()["cnt"]

        # 最高降幅商品
        max_discount_item = big_discounts[0] if big_discounts else None

        # 最高省下金額
        max_savings = max([d["savings_amount"] for d in big_discounts], default=0.0)

        # 格式化日期時間
        formatted_date = ""
        if len(latest_batch) == 14:
            formatted_date = f"{latest_batch[:4]}-{latest_batch[4:6]}-{latest_batch[6:8]} {latest_batch[8:10]}:{latest_batch[10:12]}"

        return {
            "latest_batch": latest_batch,
            "latest_batch_formatted": formatted_date,
            "prev_batch": prev_batch or "無",
            "total_monitored_stores": total_stores,
            "total_monitored_products": total_products,
            "big_discounts_count": len(big_discounts),
            "new_stores_count": len(new_stores),
            "new_products_count": len(new_products),
            "promotions_count": len(promotions),
            "max_discount_pct": max_discount_item["discount_pct"] if max_discount_item else 0.0,
            "max_savings_twd": max_savings
        }

    def export_static_data(self, data: Dict[str, Any]):
        """
        匯出靜態資料集檔案:
        - dashboard_data.json
        - dashboard_data.js (宣告 window.UBER_RADAR_DATA = {...})
        """
        os.makedirs(self.output_dir, exist_ok=True)
        json_path = os.path.join(self.output_dir, "dashboard_data.json")
        js_path = os.path.join(self.output_dir, "dashboard_data.js")

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        with open(js_path, "w", encoding="utf-8") as f:
            f.write("// UberEats Radar 靜態快照資料集 (自動由 alert_engine.py 產生)\n")
            f.write("window.UBER_RADAR_DATA = ")
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write(";\n")

        print(f"💾 [匯出快照完成]")
        print(f"   ├─ JSON: {json_path}")
        print(f"   └─ JS:   {js_path}")


def main():
    db_path = DEFAULT_DB_PATH
    output_dir = DEFAULT_WEB_DIR

    if len(sys.argv) > 1:
        for arg in sys.argv[1:]:
            if arg.endswith(".db"):
                db_path = os.path.abspath(arg)
            elif os.path.isdir(arg):
                output_dir = os.path.abspath(arg)

    engine = UberEatsAlertEngine(db_path=db_path, output_dir=output_dir)
    engine.detect_all()
    engine.close()


if __name__ == "__main__":
    main()
