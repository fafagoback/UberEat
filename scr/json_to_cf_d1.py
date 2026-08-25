# -*- coding: utf-8 -*-
"""
Uber Eats Cloudflare D1 匯入模組 (Stage 3: Push to Cloudflare D1 Serverless SQL)
將清洗後的資料與差異情報寫入 SQLite，並自動生成 D1 批次語句同步至 Cloudflare D1。
"""

import os
import sys
import glob
import json
import argparse
import subprocess
import sqlite3

# 引入本地模組目錄
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "local_scr")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from json_to_db import UberEatsDBImporter
from alert_engine import UberEatsAlertEngine


def generate_d1_sync_sql(db_path: str, output_sql_path: str, latest_batch: str):
    """將最新批次的 6 張資料表導出為 D1 專用的標準 SQL 批次語句"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    sql_statements = []

    # 1. 確保 DDL 結構存在
    ddl = """
    CREATE TABLE IF NOT EXISTS crawl_batches (
        crawled_time VARCHAR(14) PRIMARY KEY,
        benchmark_address VARCHAR(255) NOT NULL,
        benchmark_lat DECIMAL(10, 7) NOT NULL,
        benchmark_lon DECIMAL(10, 7) NOT NULL,
        total_discovered INT NOT NULL DEFAULT 0,
        success_count INT NOT NULL DEFAULT 0,
        fail_count INT NOT NULL DEFAULT 0
    );

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
        PRIMARY KEY (store_id, crawled_time)
    );

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
        PRIMARY KEY (product_id, crawled_time)
    );

    CREATE TABLE IF NOT EXISTS store_business_hours (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        store_id VARCHAR(32) NOT NULL,
        crawled_time VARCHAR(14) NOT NULL,
        day_of_week VARCHAR(20) NOT NULL,
        opens_at TIME NOT NULL,
        closes_at TIME NOT NULL
    );

    CREATE TABLE IF NOT EXISTS store_cuisines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        store_id VARCHAR(32) NOT NULL,
        crawled_time VARCHAR(14) NOT NULL,
        cuisine_name VARCHAR(100) NOT NULL
    );

    CREATE TABLE IF NOT EXISTS alerts_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alert_type VARCHAR(20) NOT NULL,
        target_id VARCHAR(64) NOT NULL,
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

    CREATE INDEX IF NOT EXISTS idx_products_history ON products (product_id, crawled_time DESC);
    CREATE INDEX IF NOT EXISTS idx_products_store_time ON products (store_id, crawled_time DESC);
    CREATE INDEX IF NOT EXISTS idx_stores_history ON stores (store_id, crawled_time DESC);
    CREATE INDEX IF NOT EXISTS idx_alerts_time ON alerts_history (crawled_time DESC, alert_type);
    """
    sql_statements.append(ddl)

    def escape_sql(val):
        if val is None:
            return "NULL"
        if isinstance(val, (int, float)):
            return str(val)
        return "'" + str(val).replace("'", "''") + "'"

    # 2. 導出 crawl_batches
    cursor.execute("SELECT * FROM crawl_batches WHERE crawled_time = ?", (latest_batch,))
    for row in cursor.fetchall():
        sql_statements.append(
            f"INSERT OR REPLACE INTO crawl_batches (crawled_time, benchmark_address, benchmark_lat, benchmark_lon, total_discovered, success_count, fail_count) VALUES ({escape_sql(row['crawled_time'])}, {escape_sql(row['benchmark_address'])}, {escape_sql(row['benchmark_lat'])}, {escape_sql(row['benchmark_lon'])}, {row['total_discovered']}, {row['success_count']}, {row['fail_count']});"
        )

    # 3. 導出 stores
    cursor.execute("SELECT * FROM stores WHERE crawled_time = ?", (latest_batch,))
    for row in cursor.fetchall():
        sql_statements.append(
            f"INSERT OR REPLACE INTO stores (store_id, crawled_time, store_name, store_type, store_url, rating_value, review_count, price_range, telephone, country_code, region, locality, street_address, postal_code, latitude, longitude, order_action_url, total_menu_items) VALUES ({escape_sql(row['store_id'])}, {escape_sql(row['crawled_time'])}, {escape_sql(row['store_name'])}, {escape_sql(row['store_type'])}, {escape_sql(row['store_url'])}, {escape_sql(row['rating_value'])}, {escape_sql(row['review_count'])}, {escape_sql(row['price_range'])}, {escape_sql(row['telephone'])}, {escape_sql(row['country_code'])}, {escape_sql(row['region'])}, {escape_sql(row['locality'])}, {escape_sql(row['street_address'])}, {escape_sql(row['postal_code'])}, {escape_sql(row['latitude'])}, {escape_sql(row['longitude'])}, {escape_sql(row['order_action_url'])}, {row['total_menu_items']});"
        )

    # 4. 導出 products
    cursor.execute("SELECT * FROM products WHERE crawled_time = ?", (latest_batch,))
    for row in cursor.fetchall():
        sql_statements.append(
            f"INSERT OR REPLACE INTO products (product_id, crawled_time, store_id, store_name, category_name, product_name, price, currency, description, promo_type, quantity) VALUES ({escape_sql(row['product_id'])}, {escape_sql(row['crawled_time'])}, {escape_sql(row['store_id'])}, {escape_sql(row['store_name'])}, {escape_sql(row['category_name'])}, {escape_sql(row['product_name'])}, {row['price']}, {escape_sql(row['currency'])}, {escape_sql(row['description'])}, {escape_sql(row['promo_type'])}, {row['quantity']});"
        )

    # 5. 導出 store_cuisines
    cursor.execute("SELECT * FROM store_cuisines WHERE crawled_time = ?", (latest_batch,))
    for row in cursor.fetchall():
        sql_statements.append(
            f"INSERT OR IGNORE INTO store_cuisines (store_id, crawled_time, cuisine_name) VALUES ({escape_sql(row['store_id'])}, {escape_sql(row['crawled_time'])}, {escape_sql(row['cuisine_name'])});"
        )

    # 6. 導出 store_business_hours
    cursor.execute("SELECT * FROM store_business_hours WHERE crawled_time = ?", (latest_batch,))
    for row in cursor.fetchall():
        sql_statements.append(
            f"INSERT OR IGNORE INTO store_business_hours (store_id, crawled_time, day_of_week, opens_at, closes_at) VALUES ({escape_sql(row['store_id'])}, {escape_sql(row['crawled_time'])}, {escape_sql(row['day_of_week'])}, {escape_sql(row['opens_at'])}, {escape_sql(row['closes_at'])});"
        )

    # 7. 導出 alerts_history
    cursor.execute("SELECT * FROM alerts_history WHERE crawled_time = ?", (latest_batch,))
    for row in cursor.fetchall():
        sql_statements.append(
            f"INSERT OR REPLACE INTO alerts_history (alert_type, target_id, store_id, store_name, product_name, category_name, original_price, current_price, discount_pct, savings_amount, promo_type, order_action_url, crawled_time) VALUES ({escape_sql(row['alert_type'])}, {escape_sql(row['target_id'])}, {escape_sql(row['store_id'])}, {escape_sql(row['store_name'])}, {escape_sql(row['product_name'])}, {escape_sql(row['category_name'])}, {escape_sql(row['original_price'])}, {escape_sql(row['current_price'])}, {escape_sql(row['discount_pct'])}, {escape_sql(row['savings_amount'])}, {escape_sql(row['promo_type'])}, {escape_sql(row['order_action_url'])}, {escape_sql(row['crawled_time'])});"
        )

    conn.close()

    with open(output_sql_path, "w", encoding="utf-8") as f:
        f.write("\n".join(sql_statements))

    print(f"📝 已生成 D1 同步 SQL 檔案: {output_sql_path} (共 {len(sql_statements)} 條語句)")
    return len(sql_statements)


def sync_to_cloudflare_d1(src_dir: str, db_name: str):
    cf_token = os.environ.get("CLOUDFLARE_API_TOKEN") or os.environ.get("CF_API_TOKEN")
    cf_account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID") or os.environ.get("CF_ACCOUNT_ID")

    db_path = "ubereats_monitor.db"
    
    # 1. 執行本地 SQLite ETL
    importer = UberEatsDBImporter(db_path=db_path, json_dir=src_dir)
    try:
        importer.init_database()
        stats = importer.import_all_data()
        importer.validate_database()
        print(f"📊 ETL 完成統計: {stats}")
    finally:
        importer.close()

    # 2. 執行情報分析引擎 (產生 alerts_history)
    engine = UberEatsAlertEngine(db_path=db_path)
    alert_result = engine.detect_all()
    latest_batch = alert_result.get("latest_batch")
    engine.close()

    if not latest_batch:
        print("⚠️ 無有效批次，結束同步。")
        return

    # 3. 產出 D1 批次同步 SQL 檔案
    sql_file = "d1_sync.sql"
    generate_d1_sync_sql(db_path, sql_file, latest_batch)

    # 4. 同步至 Cloudflare D1
    if not cf_token or not cf_account_id:
        print("⚠️ 未設定 CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID，已完成本機 SQLite ETL，跳過 D1 遠端同步。")
        return

    print(f"🚀 正在透過 Wrangler 同步資料至 Cloudflare D1 ({db_name})...")
    try:
        cmd = ["npx", "wrangler", "d1", "execute", db_name, "--remote", f"--file={sql_file}"]
        res = subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ, CLOUDFLARE_API_TOKEN=cf_token, CLOUDFLARE_ACCOUNT_ID=cf_account_id))
        if res.returncode == 0:
            print("✅ Cloudflare D1 遠端同步成功！")
            print(res.stdout)
        else:
            print(f"⚠️ Wrangler 同步警告/錯誤: {res.stderr or res.stdout}")
    except Exception as e:
        print(f"⚠️ 執行 Wrangler 時發生異常: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="同步資料至 Cloudflare D1")
    parser.add_argument("--src-dir", required=True, help="JSON 資料夾")
    parser.add_argument("--db-name", default="ubereats_monitor", help="Cloudflare D1 資料庫名稱")
    args = parser.parse_args()

    sync_to_cloudflare_d1(args.src_dir, args.db_name)
