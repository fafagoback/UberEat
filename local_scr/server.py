# -*- coding: utf-8 -*-
"""
外送平台價格與商品監控系統 (Uber Eats Price & Store Monitor)
本地 Web 伺服器與 RESTful API 引擎 (Local Web & API Server)

【核心功能】
1. 純 Python 內建標準庫開發，零外部依賴 (無需 pip install額外套件)。
2. 提供高效能 SQLite 即時查詢 REST API:
   - /api/stats: 全局統計概覽
   - /api/alerts: 警報歷史紀錄 (支援類型篩選)
   - /api/discounts: 大特價商品清單 (支援動態降幅、現省金額、分類、關鍵字篩選與排序)
   - /api/new-stores: 全新進駐店家情報
   - /api/new-products: 老店新上架菜色情報
   - /api/promotions: 買一送一/超值促銷專區
   - /api/stores: 全店家檢索與料理標籤
   - /api/products: 萬筆商品全庫搜尋、價格區間篩選與分頁
   - /api/history: 單一商品跨批次歷史價格走勢 (用於 Chart.js 趨勢圖)
   - /api/refresh: 觸發重新執行 alert_engine 分析
3. 靜態檔案伺服器 (支援 UTF-8 HTML/CSS/JS/JSON/SVG)。
"""

import os
import sys
import json
import urllib.parse
import sqlite3
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from typing import Dict, List, Any, Optional

# 確保標準輸出支援 UTF-8
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

script_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(script_dir)
BASE_DIR = parent_dir if os.path.exists(os.path.join(parent_dir, "web")) or os.path.exists(os.path.join(parent_dir, "ubereats_monitor.db")) or os.path.exists(os.path.join(parent_dir, "index.html")) else script_dir
DB_PATH = os.path.join(BASE_DIR, "ubereats_monitor.db")
WEB_DIR = os.path.join(BASE_DIR, "web") if os.path.exists(os.path.join(BASE_DIR, "web")) else BASE_DIR


def get_db_connection() -> sqlite3.Connection:
    """取得 SQLite 連線並啟用 WAL 與 Row Factory"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.row_factory = sqlite3.Row
    return conn


class UberRadarAPIHandler(SimpleHTTPRequestHandler):
    """自訂 HTTP 請求處理器，整合 REST API 與靜態檔案服務"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def end_headers(self):
        """加入 CORS 與 UTF-8 標頭"""
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def do_OPTIONS(self):
        """處理 CORS preflight 請求"""
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        """分流 API 請求與靜態資源請求"""
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query_params = urllib.parse.parse_qs(parsed_url.query)

        if path.startswith("/api/"):
            self.handle_api(path, query_params)
        else:
            # 靜態檔案預設導向 index.html
            if path == "/" or path == "":
                self.path = "/index.html"
            super().do_GET()

    def do_POST(self):
        """處理 POST 請求 (例如手動觸發重新分析)"""
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        if path == "/api/refresh":
            try:
                from alert_engine import UberEatsAlertEngine
                engine = UberEatsAlertEngine(db_path=DB_PATH, output_dir=WEB_DIR)
                result = engine.detect_all()
                engine.close()
                self.send_json_response({"status": "success", "message": "差異情報分析更新完成", "data": result["stats"]})
            except Exception as e:
                self.send_json_response({"status": "error", "message": str(e)}, status_code=500)
        else:
            self.send_json_response({"error": "Endpoint not found"}, status_code=404)

    def send_json_response(self, data: Any, status_code: int = 200):
        """回傳 JSON 格式回應"""
        response_bytes = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

    def handle_api(self, path: str, params: Dict[str, List[str]]):
        """API 路由分派核心"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            # 取得所有可用批次
            cursor.execute("SELECT DISTINCT crawled_time FROM products ORDER BY crawled_time DESC;")
            batch_rows = cursor.fetchall()
            batches = [r["crawled_time"] for r in batch_rows]
            latest_batch = batches[0] if batches else ""
            prev_batch = batches[1] if len(batches) > 1 else ""

            # -----------------------------------------------------------------
            # 1. GET /api/stats (全系統統計概覽)
            # -----------------------------------------------------------------
            if path == "/api/stats":
                # 總店家與總商品數
                cursor.execute("SELECT COUNT(DISTINCT store_id) as cnt FROM stores WHERE crawled_time = ?;", (latest_batch,))
                total_stores = cursor.fetchone()["cnt"] if latest_batch else 0

                cursor.execute("SELECT COUNT(*) as cnt FROM products WHERE crawled_time = ? AND price > 0;", (latest_batch,))
                total_products = cursor.fetchone()["cnt"] if latest_batch else 0

                # 警報計數
                cursor.execute("""
                SELECT alert_type, COUNT(*) as cnt 
                FROM alerts_history 
                WHERE crawled_time = ? 
                GROUP BY alert_type;
                """, (latest_batch,))
                alert_counts = {r["alert_type"]: r["cnt"] for r in cursor.fetchall()}

                # 促銷商品計數 (買1送1等，排除 price <= 0)
                cursor.execute("SELECT COUNT(*) as cnt FROM products WHERE crawled_time = ? AND quantity > 1 AND price > 0;", (latest_batch,))
                promo_count = cursor.fetchone()["cnt"] if latest_batch else 0

                # 格式化日期
                date_fmt = ""
                if len(latest_batch) == 14:
                    date_fmt = f"{latest_batch[:4]}-{latest_batch[4:6]}-{latest_batch[6:8]} {latest_batch[8:10]}:{latest_batch[10:12]}"

                self.send_json_response({
                    "status": "success",
                    "latest_batch": latest_batch,
                    "latest_batch_formatted": date_fmt,
                    "prev_batch": prev_batch,
                    "batches": batches,
                    "total_stores": total_stores,
                    "total_products": total_products,
                    "big_discounts_count": alert_counts.get("BIG_DISCOUNT", 0),
                    "new_stores_count": alert_counts.get("NEW_STORE", 0),
                    "new_products_count": alert_counts.get("NEW_PRODUCT", 0),
                    "promotions_count": promo_count
                })

            # -----------------------------------------------------------------
            # 2. GET /api/discounts (大特價即時篩選查詢)
            # -----------------------------------------------------------------
            elif path == "/api/discounts":
                min_discount = float(params.get("min_discount", ["30.0"])[0])
                min_savings = float(params.get("min_savings", ["20.0"])[0])
                keyword = params.get("q", [""])[0].strip().lower()
                category = params.get("category", [""])[0].strip()
                sort_by = params.get("sort", ["discount_desc"])[0]

                if not prev_batch:
                    self.send_json_response({"status": "success", "total": 0, "items": []})
                    conn.close()
                    return

                query = """
                SELECT 
                    p1.product_id,
                    p1.store_id,
                    p1.store_name,
                    p1.product_name,
                    p1.category_name,
                    p1.description,
                    ROUND(p0.price * 1.0 / p0.quantity, 2) as prev_eff_price,
                    ROUND(p1.price * 1.0 / p1.quantity, 2) as curr_eff_price,
                    p0.price as prev_raw_price,
                    p1.price as curr_raw_price,
                    p0.quantity as prev_qty,
                    p1.quantity as curr_qty,
                    p1.promo_type,
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
                """
                cursor.execute(query, (prev_batch, latest_batch))
                rows = cursor.fetchall()

                items = []
                for r in rows:
                    p_eff = float(r["prev_eff_price"])
                    c_eff = float(r["curr_eff_price"])
                    if p_eff <= 0:
                        continue
                    savings = p_eff - c_eff
                    drop_pct = (savings / p_eff) * 100.0

                    if drop_pct >= min_discount and savings >= min_savings:
                        p_name = r["product_name"] or ""
                        s_name = r["store_name"] or ""
                        cat_name = r["category_name"] or "未分類"

                        # 關鍵字篩選
                        if keyword and (keyword not in p_name.lower() and keyword not in s_name.lower()):
                            continue

                        # 分類篩選
                        if category and category != "全部" and category not in cat_name:
                            continue

                        raw_url = r["order_action_url"] or ""
                        items.append({
                            "product_id": r["product_id"],
                            "store_id": r["store_id"],
                            "store_name": s_name,
                            "product_name": p_name,
                            "category_name": cat_name,
                            "description": r["description"] or "",
                            "original_price": p_eff,
                            "current_price": c_eff,
                            "prev_raw_price": float(r["prev_raw_price"]),
                            "curr_raw_price": float(r["curr_raw_price"]),
                            "prev_qty": int(r["prev_qty"]),
                            "curr_qty": int(r["curr_qty"]),
                            "discount_pct": round(drop_pct, 1),
                            "savings_amount": round(savings, 1),
                            "promo_type": r["promo_type"],
                            "order_action_url": raw_url.replace("&amp;", "&"),
                            "rating_value": float(r["rating_value"]) if r["rating_value"] is not None else None,
                            "review_count": int(r["review_count"]) if r["review_count"] is not None else None,
                            "locality": r["locality"] or "",
                            "street_address": r["street_address"] or "",
                            "crawled_time": latest_batch
                        })

                # 排序邏輯
                if sort_by == "discount_desc":
                    items.sort(key=lambda x: (x["discount_pct"], x["savings_amount"]), reverse=True)
                elif sort_by == "savings_desc":
                    items.sort(key=lambda x: (x["savings_amount"], x["discount_pct"]), reverse=True)
                elif sort_by == "price_asc":
                    items.sort(key=lambda x: x["current_price"])
                elif sort_by == "price_desc":
                    items.sort(key=lambda x: x["current_price"], reverse=True)

                self.send_json_response({"status": "success", "total": len(items), "items": items})

            # -----------------------------------------------------------------
            # 3. GET /api/new-stores (全新進駐店家)
            # -----------------------------------------------------------------
            elif path == "/api/new-stores":
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
                stores = []
                for r in rows:
                    d = dict(r)
                    d["store_url"] = (d.get("store_url") or "").replace("&amp;", "&")
                    d["order_action_url"] = (d.get("order_action_url") or d.get("store_url") or "").replace("&amp;", "&")
                    stores.append(d)
                self.send_json_response({"status": "success", "total": len(stores), "items": stores})

            # -----------------------------------------------------------------
            # 4. GET /api/new-products (老店新菜色)
            # -----------------------------------------------------------------
            elif path == "/api/new-products":
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
                prods = []
                for r in rows:
                    d = dict(r)
                    d["order_action_url"] = (d.get("order_action_url") or "").replace("&amp;", "&")
                    prods.append(d)
                self.send_json_response({"status": "success", "total": len(prods), "items": prods})

            # -----------------------------------------------------------------
            # 5. GET /api/promotions (買一送一促銷專區)
            # -----------------------------------------------------------------
            elif path == "/api/promotions":
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
                    d = dict(r)
                    d["order_action_url"] = (d.get("order_action_url") or "").replace("&amp;", "&")
                    promos.append(d)
                self.send_json_response({"status": "success", "total": len(promos), "items": promos})

            # -----------------------------------------------------------------
            # 6. GET /api/products (全庫商品即時檢索與分頁)
            # -----------------------------------------------------------------
            elif path == "/api/products":
                keyword = params.get("q", [""])[0].strip()
                category = params.get("category", [""])[0].strip()
                store_id = params.get("store_id", [""])[0].strip()
                min_price = float(params.get("min_price", ["0"])[0])
                max_price = float(params.get("max_price", ["99999"])[0])
                page = int(params.get("page", ["1"])[0])
                limit = int(params.get("limit", ["24"])[0])
                sort_by = params.get("sort", ["rating_desc"])[0]

                sql_where = ["p.crawled_time = ?", "p.price > 0"]
                sql_params = [latest_batch]

                if keyword:
                    sql_where.append("(p.product_name LIKE ? OR p.store_name LIKE ? OR p.description LIKE ?)")
                    kw_like = f"%{keyword}%"
                    sql_params.extend([kw_like, kw_like, kw_like])

                if category and category != "全部":
                    sql_where.append("p.category_name LIKE ?")
                    sql_params.append(f"%{category}%")

                if store_id:
                    sql_where.append("p.store_id = ?")
                    sql_params.append(store_id)

                if min_price > 0:
                    sql_where.append("p.price >= ?")
                    sql_params.append(min_price)

                if max_price < 99999:
                    sql_where.append("p.price <= ?")
                    sql_params.append(max_price)

                where_clause = " WHERE " + " AND ".join(sql_where)

                # 計算總數
                count_query = f"SELECT COUNT(*) as total FROM products p {where_clause};"
                cursor.execute(count_query, sql_params)
                total = cursor.fetchone()["total"]

                # 排序依據
                sort_clause = "ORDER BY s.rating_value DESC, p.price ASC"
                if sort_by == "price_asc":
                    sort_clause = "ORDER BY p.price ASC"
                elif sort_by == "price_desc":
                    sort_clause = "ORDER BY p.price DESC"
                elif sort_by == "name_asc":
                    sort_clause = "ORDER BY p.product_name ASC"

                offset = (page - 1) * limit
                data_query = f"""
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
                    s.locality
                FROM products p
                LEFT JOIN stores s ON p.store_id = s.store_id AND p.crawled_time = s.crawled_time
                {where_clause}
                {sort_clause}
                LIMIT ? OFFSET ?;
                """
                cursor.execute(data_query, sql_params + [limit, offset])
                rows = cursor.fetchall()
                prods = []
                for r in rows:
                    d = dict(r)
                    d["order_action_url"] = (d.get("order_action_url") or "").replace("&amp;", "&")
                    prods.append(d)

                self.send_json_response({
                    "status": "success",
                    "total": total,
                    "page": page,
                    "limit": limit,
                    "total_pages": (total + limit - 1) // limit,
                    "items": prods
                })

            # -----------------------------------------------------------------
            # 7. GET /api/history (單一商品歷史價格趨勢，用於 Chart.js)
            # -----------------------------------------------------------------
            elif path == "/api/history":
                prod_id = params.get("product_id", [""])[0].strip()
                if not prod_id:
                    self.send_json_response({"status": "error", "message": "product_id is required"}, status_code=400)
                    conn.close()
                    return

                query = """
                SELECT 
                    p.product_id,
                    p.crawled_time,
                    p.store_name,
                    p.product_name,
                    p.price,
                    p.quantity,
                    p.promo_type,
                    ROUND(p.price * 1.0 / p.quantity, 2) as eff_price
                FROM products p
                WHERE p.product_id = ?
                ORDER BY p.crawled_time ASC;
                """
                cursor.execute(query, (prod_id,))
                rows = cursor.fetchall()
                history = [dict(r) for r in rows]

                self.send_json_response({
                    "status": "success",
                    "product_id": prod_id,
                    "history": history
                })

            # -----------------------------------------------------------------
            # 8. GET /api/stores (店家檢索)
            # -----------------------------------------------------------------
            elif path == "/api/stores":
                keyword = params.get("q", [""])[0].strip()
                sql_where = ["s.crawled_time = ?"]
                sql_params = [latest_batch]

                if keyword:
                    sql_where.append("s.store_name LIKE ?")
                    sql_params.append(f"%{keyword}%")

                where_clause = " WHERE " + " AND ".join(sql_where)
                query = f"""
                SELECT 
                    s.store_id,
                    s.store_name,
                    s.store_type,
                    s.store_url,
                    s.rating_value,
                    s.review_count,
                    s.price_range,
                    s.telephone,
                    s.locality,
                    s.street_address,
                    COALESCE(NULLIF(s.order_action_url, ''), s.store_url, '') as order_action_url,
                    s.total_menu_items,
                    (
                        SELECT GROUP_CONCAT(cuisine_name, '、')
                        FROM store_cuisines sc
                        WHERE sc.store_id = s.store_id AND sc.crawled_time = s.crawled_time
                    ) as cuisines
                FROM stores s
                {where_clause}
                ORDER BY s.rating_value DESC, s.review_count DESC;
                """
                cursor.execute(query, sql_params)
                rows = cursor.fetchall()
                stores = []
                for r in rows:
                    d = dict(r)
                    d["store_url"] = (d.get("store_url") or "").replace("&amp;", "&")
                    d["order_action_url"] = (d.get("order_action_url") or d.get("store_url") or "").replace("&amp;", "&")
                    stores.append(d)
                self.send_json_response({"status": "success", "total": len(stores), "items": stores})

            else:
                self.send_json_response({"error": f"API route '{path}' not found"}, status_code=404)

            conn.close()

        except Exception as e:
            self.send_json_response({"status": "error", "message": str(e)}, status_code=500)


def run_server(port: int = 8000, auto_open_browser: bool = False):
    """啟動本地 Web & API 伺服器"""
    server_address = ("", port)
    
    # 嘗試綁定埠號，若被佔用則順延
    for p in range(port, port + 20):
        try:
            httpd = HTTPServer(("", p), UberRadarAPIHandler)
            actual_port = p
            break
        except OSError:
            continue
    else:
        print("❌ 無法綁定任何可用連接埠。")
        return

    url = f"http://localhost:{actual_port}"
    print("\n" + "=" * 65)
    print("🍔 Uber Eats 特價與新品情報監控系統 (UberEats Radar)")
    print(f"🚀 本地伺服器已啟動: {url}")
    print(f"📂 資料庫位置:       {DB_PATH}")
    print(f"🌐 網頁目錄位置:     {WEB_DIR}")
    print("💡 按 Ctrl+C 可停止伺服器")
    print("=" * 65 + "\n")

    if auto_open_browser:
        webbrowser.open(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 伺服器已安全停止。")
        httpd.server_close()


if __name__ == "__main__":
    auto_open = "--open" in sys.argv or "-o" in sys.argv
    port_arg = 8000
    for arg in sys.argv:
        if arg.startswith("--port="):
            port_arg = int(arg.split("=")[1])
    run_server(port=port_arg, auto_open_browser=auto_open)
