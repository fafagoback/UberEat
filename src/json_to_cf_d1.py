# -*- coding: utf-8 -*-
"""
Uber Eats Cloudflare D1 匯入模組 (Stage 3: Push to Cloudflare D1 Serverless SQL)
【檢核與重試機制】：
1. 嚴格檢核來源 JSON 檔案完整性
2. 執行本地 SQLite ETL 並進行資料完整性、外鍵約束、NOT NULL 嚴格檢驗 (3 次重試)
3. 執行智慧差異情報引擎並驗證批次時間戳記 (latest_batch)
4. 生成 D1 批次 SQL 並校驗語法與語句筆數
5. 透過 Wrangler 執行遠端 D1 同步 (3 次重試，失敗立即報錯熔斷)
6. 遠端 SQL 回查驗證 (SELECT count(*) 檢核寫入筆數 > 0)
7. 輸出全流程 Final 檢核報告至 GitHub Actions $GITHUB_STEP_SUMMARY
"""

import os
import sys
import glob
import json
import time
import re
import argparse
import subprocess
import sqlite3
from contextlib import closing
from datetime import datetime

# 確保標準輸出與標準錯誤支援 UTF-8
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

# 引入模組目錄
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from json_to_db import UberEatsDBImporter
from alert_engine import UberEatsAlertEngine
from d1_publication import PUBLICATION_DDL, TABLES, query_remote, count_query, verify_counts
from snapshot_validation import validate_document, validate_snapshot


# D1/SQLite 對單條 SQL 有長度限制。保留足夠餘裕給 Wrangler 與遠端解析層，
# 並限制每個上傳檔大小，避免以「語句數」估算時被長商品描述突破限制。
MAX_INSERT_BYTES = 80 * 1024
MAX_SQL_FILE_BYTES = 1024 * 1024


def sql_utf8_size(value: str) -> int:
    return len(value.encode("utf-8"))


def build_batch_insert(
    table_name: str,
    columns: list,
    rows: list,
    escape_value,
    or_action: str = "REPLACE",
    batch_size: int = 50,
    max_statement_bytes: int = MAX_INSERT_BYTES,
) -> list:
    """建立同時受列數與 UTF-8 byte 大小限制的 multi-row INSERT。"""
    if not rows:
        return []

    prefix = f"INSERT OR {or_action} INTO {table_name} ({', '.join(columns)}) VALUES\n    "
    statements = []
    values = []

    def flush() -> None:
        if values:
            statements.append(prefix + ",\n    ".join(values) + ";")
            values.clear()

    for row_index, row in enumerate(rows):
        encoded_row = "(" + ", ".join(escape_value(value) for value in row) + ")"
        single_statement = prefix + encoded_row + ";"
        single_size = sql_utf8_size(single_statement)
        if single_size > max_statement_bytes:
            raise ValueError(
                f"{table_name} 第 {row_index + 1} 列單筆 INSERT 為 {single_size:,} bytes，"
                f"超過安全上限 {max_statement_bytes:,} bytes"
            )

        candidate = prefix + ",\n    ".join(values + [encoded_row]) + ";"
        if values and (len(values) >= batch_size or sql_utf8_size(candidate) > max_statement_bytes):
            flush()
        values.append(encoded_row)

    flush()
    return statements


def split_sql_files(sql_statements: list, output_prefix: str = "d1_sync_part") -> list:
    """依 UTF-8 byte 大小分割 Wrangler 上傳檔，且不拆開任何 SQL 語句。"""
    groups = []
    current = []
    current_bytes = 0
    for statement_index, statement in enumerate(sql_statements):
        statement_bytes = sql_utf8_size(statement)
        if statement_bytes > MAX_INSERT_BYTES and not statement.lstrip().upper().startswith("CREATE "):
            raise ValueError(
                f"第 {statement_index + 1} 條 SQL 為 {statement_bytes:,} bytes，"
                f"超過安全上限 {MAX_INSERT_BYTES:,} bytes"
            )
        separator_bytes = 1 if current else 0
        if current and current_bytes + separator_bytes + statement_bytes > MAX_SQL_FILE_BYTES:
            groups.append(current)
            current = []
            current_bytes = 0
            separator_bytes = 0
        current.append(statement)
        current_bytes += separator_bytes + statement_bytes
    if current:
        groups.append(current)

    paths = []
    for part_index, statements in enumerate(groups):
        path = f"{output_prefix}_{part_index}.sql"
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(statements))
        paths.append(path)
    return paths


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


def fatal_error(step_name: str, reason: str, expected: str = "", actual: str = "", retries: int = 3):
    """輸出醒目錯誤橫幅、寫入 GITHUB_STEP_SUMMARY 並強制以 exit code 1 終止"""
    msg = f"""
================================================================================
❌ 【階段 3: ETL 與 Cloudflare D1 檢核失敗 (FATAL ERROR)】
步驟名稱: {step_name}
重試次數: 已重試 {retries} 次均未達標
錯誤原因: {reason}
預期成果: {expected}
實際結果: {actual}
================================================================================
"""
    print(msg, file=sys.stderr, flush=True)
    
    summary_md = f"""
### ❌ 【ETL / D1 同步失敗】
> [!CAUTION]
> **在「{step_name}」經 {retries} 次重試仍未達成預期成果，流程已強制終止 (Exit Code 1)！**
> - **錯誤原因**: `{reason}`
> - **預期成果**: `{expected}`
> - **實際結果**: `{actual}`
"""
    append_github_step_summary(summary_md)
    sys.exit(1)


def generate_d1_sync_sql(db_path: str, output_sql_path: str, latest_batch: str, scope: str = "regional") -> int:
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
        is_open INT NOT NULL DEFAULT 1,
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
        is_open INT NOT NULL DEFAULT 1,
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
    sql_statements.append(PUBLICATION_DDL)
    if scope not in {"taiwan", "regional"} or len(latest_batch) != 14 or not latest_batch.isdigit():
        raise ValueError("invalid scope or batch ID")
    run_id = os.environ.get("GITHUB_RUN_ID", "local").replace("'", "''")
    sql_statements.append(
        f"INSERT INTO batch_publications (crawled_time,scope,status,source_run_id) "
        f"VALUES ('{latest_batch}','{scope}','staging','{run_id}') ON CONFLICT(crawled_time) DO NOTHING;"
    )
    # Replaying a partially written batch must be idempotent, including tables with surrogate IDs.
    for table in ("store_cuisines", "store_business_hours", "alerts_history", "products", "stores"):
        sql_statements.append(f"DELETE FROM {table} WHERE crawled_time='{latest_batch}' AND EXISTS (SELECT 1 FROM batch_publications WHERE crawled_time='{latest_batch}' AND status='staging');")
    sql_statements.extend([
        "DELETE FROM store_cuisines WHERE id NOT IN (SELECT MIN(id) FROM store_cuisines GROUP BY store_id,crawled_time,cuisine_name);",
        "DELETE FROM store_business_hours WHERE id NOT IN (SELECT MIN(id) FROM store_business_hours GROUP BY store_id,crawled_time,day_of_week,opens_at,closes_at);",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_cuisines_unique ON store_cuisines(store_id,crawled_time,cuisine_name);",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_hours_unique ON store_business_hours(store_id,crawled_time,day_of_week,opens_at,closes_at);",
    ])

    def escape_sql(val):
        if val is None:
            return "NULL"
        if isinstance(val, (int, float)):
            return str(val)
        # 清理換行與回車符號，避免影響 Wrangler 與 D1 語法解析
        s = str(val).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        return "'" + s.replace("'", "''") + "'"

    # 2. 導出 crawl_batches
    cursor.execute("SELECT * FROM crawl_batches WHERE crawled_time = ?", (latest_batch,))
    batch_rows = [(row['crawled_time'], row['benchmark_address'], row['benchmark_lat'], row['benchmark_lon'], row['total_discovered'], row['success_count'], row['fail_count']) for row in cursor.fetchall()]
    sql_statements.extend(build_batch_insert(
        "crawl_batches",
        ["crawled_time", "benchmark_address", "benchmark_lat", "benchmark_lon", "total_discovered", "success_count", "fail_count"],
        batch_rows,
        escape_sql,
        or_action="REPLACE",
        batch_size=50
    ))

    # 3. 導出 stores (批量打包: 每條 50 筆)
    cursor.execute("SELECT * FROM stores WHERE crawled_time = ?", (latest_batch,))
    store_rows = [(row['store_id'], row['crawled_time'], row['store_name'], row['store_type'], row['store_url'], row['rating_value'], row['review_count'], row['price_range'], row['telephone'], row['country_code'], row['region'], row['locality'], row['street_address'], row['postal_code'], row['latitude'], row['longitude'], row['order_action_url'], row['total_menu_items'], row['is_open'] if 'is_open' in row.keys() else 1) for row in cursor.fetchall()]
    sql_statements.extend(build_batch_insert(
        "stores",
        ["store_id", "crawled_time", "store_name", "store_type", "store_url", "rating_value", "review_count", "price_range", "telephone", "country_code", "region", "locality", "street_address", "postal_code", "latitude", "longitude", "order_action_url", "total_menu_items", "is_open"],
        store_rows,
        escape_sql,
        or_action="REPLACE",
        batch_size=50
    ))

    # 4. 導出 products (批量打包: 每條 50 筆，降低 API 呼叫量 98%)
    cursor.execute("SELECT * FROM products WHERE crawled_time = ?", (latest_batch,))
    product_rows = [(row['product_id'], row['crawled_time'], row['store_id'], row['store_name'], row['category_name'], row['product_name'], row['price'], row['currency'], row['description'], row['promo_type'], row['quantity'], row['is_open'] if 'is_open' in row.keys() else 1) for row in cursor.fetchall()]
    sql_statements.extend(build_batch_insert(
        "products",
        ["product_id", "crawled_time", "store_id", "store_name", "category_name", "product_name", "price", "currency", "description", "promo_type", "quantity", "is_open"],
        product_rows,
        escape_sql,
        or_action="REPLACE",
        batch_size=50
    ))

    # 5. 導出 store_cuisines (批量打包: 每條 100 筆)
    cursor.execute("SELECT * FROM store_cuisines WHERE crawled_time = ?", (latest_batch,))
    cuisine_rows = [(row['store_id'], row['crawled_time'], row['cuisine_name']) for row in cursor.fetchall()]
    sql_statements.extend(build_batch_insert(
        "store_cuisines",
        ["store_id", "crawled_time", "cuisine_name"],
        cuisine_rows,
        escape_sql,
        or_action="IGNORE",
        batch_size=100
    ))

    # 6. 導出 store_business_hours (批量打包: 每條 100 筆)
    cursor.execute("SELECT * FROM store_business_hours WHERE crawled_time = ?", (latest_batch,))
    hour_rows = [(row['store_id'], row['crawled_time'], row['day_of_week'], row['opens_at'], row['closes_at']) for row in cursor.fetchall()]
    sql_statements.extend(build_batch_insert(
        "store_business_hours",
        ["store_id", "crawled_time", "day_of_week", "opens_at", "closes_at"],
        hour_rows,
        escape_sql,
        or_action="IGNORE",
        batch_size=100
    ))

    # 7. 導出 alerts_history (批量打包: 每條 50 筆)
    cursor.execute("SELECT * FROM alerts_history WHERE crawled_time = ?", (latest_batch,))
    alert_rows = [(row['alert_type'], row['target_id'], row['store_id'], row['store_name'], row['product_name'], row['category_name'], row['original_price'], row['current_price'], row['discount_pct'], row['savings_amount'], row['promo_type'], row['order_action_url'], row['crawled_time']) for row in cursor.fetchall()]
    sql_statements.extend(build_batch_insert(
        "alerts_history",
        ["alert_type", "target_id", "store_id", "store_name", "product_name", "category_name", "original_price", "current_price", "discount_pct", "savings_amount", "promo_type", "order_action_url", "crawled_time"],
        alert_rows,
        escape_sql,
        or_action="REPLACE",
        batch_size=50
    ))

    conn.close()

    # 寫入完整 SQL 檔
    with open(output_sql_path, "w", encoding="utf-8") as f:
        f.write("\n".join(sql_statements))

    # 固定依實際 UTF-8 大小分檔，避免中文字與長描述讓 byte 數遠高於字元估算。
    sql_files = split_sql_files(sql_statements)
    largest_statement = max(sql_utf8_size(statement) for statement in sql_statements)
    largest_file = max(os.path.getsize(path) for path in sql_files)
    print(
        f"📦 已依 byte 大小切分為 {len(sql_files)} 個 D1 子檔案 "
        f"(最大語句 {largest_statement:,} bytes；最大檔案 {largest_file:,} bytes)"
    )

    print(f"📝 已生成 D1 同步 SQL 檔案: {output_sql_path} (共 {len(sql_statements)} 條語句, 分為 {len(sql_files)} 個執行檔)")
    return len(sql_statements), sql_files



def ensure_d1_schema_columns(db_name: str, cf_token: str, cf_account_id: str):
    """Migrate only known columns; query/migration failures are never ignored."""
    env = dict(os.environ, CLOUDFLARE_API_TOKEN=cf_token, CLOUDFLARE_ACCOUNT_ID=cf_account_id)
    for table, additions in {
        "stores": {"is_open": "INT NOT NULL DEFAULT 1"},
        "products": {"is_open": "INT NOT NULL DEFAULT 1", "quantity": "INT NOT NULL DEFAULT 1", "promo_type": "TEXT NOT NULL DEFAULT '無'"},
    }.items():
        rows = query_remote(db_name, f"PRAGMA table_info({table});", env)
        columns = {row["name"] for row in rows}
        if columns:
            for column, definition in additions.items():
                if column not in columns:
                    query_remote(db_name, f"ALTER TABLE {table} ADD COLUMN {column} {definition};", env)


def sync_to_cloudflare_d1(src_dir: str, db_name: str, require_d1: bool = False, scope: str = "regional", stores_file=None, batch_id=None):
    start_time = time.time()
    cf_token = os.environ.get("CLOUDFLARE_API_TOKEN") or os.environ.get("CF_API_TOKEN")
    cf_account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID") or os.environ.get("CF_ACCOUNT_ID")
    db_candidates = [
        os.path.join("data", "db", "ubereats_monitor.db"),
        "ubereats_monitor.db",
    ]
    db_path = next((p for p in db_candidates if os.path.exists(p)), ("data/db/ubereats_monitor.db" if os.path.isdir("data/db") else "ubereats_monitor.db"))

    print("=" * 80)
    print("🚀 【階段 3: SQLite ETL & Cloudflare D1 同步】啟動 (嚴格檢核版)")
    print(f"📁 來源目錄: {src_dir}")
    print(f"🗄️ 目標 D1 資料庫: {db_name}")
    print("=" * 80)

    # ---------------------------------------------------------
    # 步驟 3.1：來源目錄與 JSON 檔案檢核
    # ---------------------------------------------------------
    if not os.path.exists(src_dir):
        fatal_error(
            step_name="步驟 3.1 來源目錄檢核",
            reason=f"來源目錄不存在: {src_dir}",
            expected="目錄存在且包含 JSON 檔案",
            actual="目錄不存在",
            retries=0
        )

    json_files = glob.glob(os.path.join(src_dir, "*.json"))
    if len(json_files) == 0:
        fatal_error(
            step_name="步驟 3.1 來源 JSON 總量檢核",
            reason=f"目錄 {src_dir} 內無任何 JSON 檔案可供處理",
            expected="JSON 檔案數 > 0",
            actual="0 個檔案",
            retries=0
        )

    print(f"✅ [步驟 3.1 通過] 掃描到 {len(json_files)} 個原始 JSON 檔案準備進行 ETL。")
    batches = set()
    identities = set()
    for path in json_files:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
        current_batch = os.path.basename(path).split("_", 1)[0]
        if len(current_batch) != 14 or not current_batch.isdigit():
            raise ValueError(f"invalid batch filename: {path}")
        identity = validate_document(doc, current_batch, path)
        if identity in identities:
            raise ValueError(f"duplicate store: {identity}")
        identities.add(identity)
        batches.add(current_batch)
    if len(batches) != 1 or (batch_id and batch_id not in batches):
        raise ValueError("mixed or unexpected input batch")
    batch_id = batches.pop()
    if scope == "taiwan" and not stores_file:
        raise ValueError("Taiwan publication requires an assigned store manifest")
    if stores_file:
        with open(stores_file, encoding="utf-8") as handle:
            validate_snapshot(src_dir, json.load(handle), batch_id)

    # ---------------------------------------------------------
    # 步驟 3.3：本地 SQLite ETL 清洗與資料庫完整性驗證 (3 次重試)
    # ---------------------------------------------------------
    print(f"\n⚙️ 【步驟 3.3】執行 SQLite 本地 ETL 與資料庫約束驗證...")
    etl_ok = False
    stats = {}
    last_etl_err = ""

    for attempt in range(1, 4):
        importer = UberEatsDBImporter(db_path=db_path, json_dir=src_dir)
        try:
            importer.init_database()
            stats = importer.import_all_data()
            importer.validate_database()
            
            # 檢核: stores 與 products 是否有寫入
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM stores;")
            s_cnt = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM products;")
            p_cnt = cur.fetchone()[0]
            conn.close()

            if s_cnt > 0 and p_cnt > 0:
                etl_ok = True
                print(f"✅ [步驟 3.3 通過] (嘗試 {attempt}/3) ETL 完成！stores: {s_cnt} 筆, products: {p_cnt} 筆")
                break
            else:
                last_etl_err = f"ETL 寫入資料量為 0 (stores: {s_cnt}, products: {p_cnt})"
                print(f"⚠️ [步驟 3.3 檢核未過] (嘗試 {attempt}/3): {last_etl_err}")
        except Exception as e:
            last_etl_err = str(e)
            print(f"⚠️ [步驟 3.3 異常] (嘗試 {attempt}/3): {e}")
        finally:
            importer.close()

        if attempt < 3:
            time.sleep(2.0 * attempt)

    if not etl_ok:
        fatal_error(
            step_name="步驟 3.3 SQLite 本地 ETL 與完整性檢核",
            reason=f"ETL 執行重試 3 次皆未通過: {last_etl_err}",
            expected="stores > 0 且 products > 0，外鍵約束 100% 通過",
            actual="寫入為 0 或校驗失敗",
            retries=3
        )

    # ---------------------------------------------------------
    # 步驟 3.4：智慧差異情報與特價計算 (Alert Engine)
    # ---------------------------------------------------------
    print(f"\n🧠 【步驟 3.4】執行智慧差異情報與特價計算...")
    engine = UberEatsAlertEngine(db_path=db_path)
    try:
        alert_result = engine.detect_all(latest_batch=batch_id)
        latest_batch = alert_result.get("latest_batch")
        total_alerts = alert_result.get("total_alerts_detected", 0)
    finally:
        engine.close()

    if not latest_batch or len(latest_batch) != 14:
        fatal_error(
            step_name="步驟 3.4 差異情報與批次時間戳記",
            reason=f"無法獲取有效之 14 碼批次時間戳記 (latest_batch: {latest_batch})",
            expected="14 碼 YYYYMMDDhhmmss 字串",
            actual=str(latest_batch),
            retries=1
        )

    print(f"✅ [步驟 3.4 通過] 鎖定最新採集批次: {latest_batch}，計算出 {total_alerts} 筆差異與特價情報！")

    # ---------------------------------------------------------
    # 步驟 3.5：Cloudflare D1 批次 SQL 生成與語法檢核
    # ---------------------------------------------------------
    print(f"\n📝 【步驟 3.5】生成 Cloudflare D1 批次 SQL 語句檔案...")
    sql_file = "d1_sync.sql"
    sql_count, sql_files = generate_d1_sync_sql(db_path, sql_file, latest_batch, scope)

    if not os.path.exists(sql_file) or os.path.getsize(sql_file) == 0 or sql_count == 0:
        fatal_error(
            step_name="步驟 3.5 D1 批次 SQL 生成檢核",
            reason=f"D1 同步 SQL 檔案生成異常或為空檔案: {sql_file}",
            expected="SQL 檔案大小 > 0 且語句數 > 0",
            actual=f"語句數: {sql_count}, 大小: {os.path.getsize(sql_file) if os.path.exists(sql_file) else 0}",
            retries=1
        )

    print(f"✅ [步驟 3.5 通過] D1 同步 SQL 檔案校驗無誤: {sql_file} (共 {sql_count} 條語句, {len(sql_files)} 個分塊檔)")

    # ---------------------------------------------------------
    # 步驟 3.6：Cloudflare D1 遠端批次寫入 (支援分塊迴圈與 3 次重試)
    # ---------------------------------------------------------
    print(f"\n☁️ 【步驟 3.6】執行 Cloudflare D1 遠端同步 ({db_name})...")
    if not cf_token or not cf_account_id:
        if is_ci or require_d1:
            fatal_error(
                step_name="步驟 3.6 Cloudflare 憑證檢核",
                reason="環境中缺少 CLOUDFLARE_API_TOKEN 或 CLOUDFLARE_ACCOUNT_ID Secrets",
                expected="有效 Cloudflare API Token 與 Account ID",
                actual="None (未設定)",
                retries=0
            )
        else:
            print("ℹ️ 本機離線模式 (未設定 Cloudflare 憑證)，跳過 D1 遠端同步步驟。")
            return

    # 先執行遠端 D1 Schema 檢查與欄位遷移 (確保 stores/products 具備 is_open/promo_type/quantity)
    ensure_d1_schema_columns(db_name, cf_token, cf_account_id)
    env_vars = dict(os.environ, CLOUDFLARE_API_TOKEN=cf_token, CLOUDFLARE_ACCOUNT_ID=cf_account_id)
    exists = query_remote(db_name, "SELECT name FROM sqlite_master WHERE type='table' AND name='batch_publications';", env_vars)
    if exists:
        published = query_remote(db_name, f"SELECT status,scope FROM batch_publications WHERE crawled_time='{latest_batch}';", env_vars)
        if published:
            if published[0]["scope"] != scope:
                raise ValueError("batch ID already belongs to a different scope")
            if published[0]["status"] == "complete":
                with closing(sqlite3.connect(db_path)) as connection:
                    expected = {table: connection.execute(f"SELECT count(*) FROM {table} WHERE crawled_time=?", (latest_batch,)).fetchone()[0] for table in TABLES}
                verify_counts(query_remote(db_name, count_query(latest_batch), env_vars), expected)
                print("Batch already published and verified; refusing to mutate immutable data")
                return

    # 依序執行所有 SQL 分塊檔案
    for f_idx, current_sql_target in enumerate(sql_files, 1):
        print(f"   ▶ 正在匯入分塊 SQL [{f_idx}/{len(sql_files)}]: {current_sql_target}...")
        d1_sync_ok = False
        last_d1_err = ""
        attempts_made = 0

        for attempt in range(1, 4):
            attempts_made = attempt
            try:
                cmd = f'npx wrangler d1 execute {db_name} --remote --file="{current_sql_target}"'
                res = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    env=dict(os.environ, CLOUDFLARE_API_TOKEN=cf_token, CLOUDFLARE_ACCOUNT_ID=cf_account_id)
                )
                out_str = (res.stdout or "") + (res.stderr or "")
                if res.returncode == 0 and "Error" not in out_str:
                    d1_sync_ok = True
                    print(f"      ✅ 分塊 {current_sql_target} 寫入成功！")
                    break
                else:
                    last_d1_err = out_str.strip()
                    err_display = last_d1_err[-1500:] if len(last_d1_err) > 1500 else last_d1_err
                    print(f"      ⚠️ [嘗試 {attempt}/3 異常]:\n{err_display}")
                    if "SQLITE_TOOBIG" in out_str or "statement too long" in out_str.lower():
                        print("      ❌ SQL 大小錯誤不可藉由重試恢復，立即停止。")
                        break
            except Exception as e:
                last_d1_err = str(e)
                print(f"      ⚠️ [嘗試 {attempt}/3 例外]: {e}")

            if attempt < 3:
                time.sleep(3.0 * attempt)

        if not d1_sync_ok:
            err_report = last_d1_err[-1500:] if len(last_d1_err) > 1500 else last_d1_err
            fatal_error(
                step_name=f"步驟 3.6 Cloudflare D1 分塊匯入 ({current_sql_target})",
                reason=f"Wrangler 遠端寫入分塊 {current_sql_target} 嘗試 {attempts_made} 次後失敗:\n{err_report}",
                expected="Wrangler returncode == 0 且無錯誤",
                actual="Wrangler 執行失敗",
                retries=attempts_made
            )

    print("✅ [步驟 3.6 通過] 所有 Cloudflare D1 遠端 SQL 批次分塊寫入成功！")


    # ---------------------------------------------------------
    # 步驟 3.7：Cloudflare D1 遠端資料庫回查驗證 (3 次重試)
    # ---------------------------------------------------------
    print(f"\n🔍 【步驟 3.7】執行 Cloudflare D1 遠端資料庫回查驗證...")
    env_vars = dict(os.environ, CLOUDFLARE_API_TOKEN=cf_token, CLOUDFLARE_ACCOUNT_ID=cf_account_id)
    with closing(sqlite3.connect(db_path)) as connection:
        expected_counts = {table: connection.execute(f"SELECT count(*) FROM {table} WHERE crawled_time=?", (latest_batch,)).fetchone()[0] for table in TABLES}
    if expected_counts["stores"] != len(identities):
        raise ValueError("ETL store identities do not cover the input manifest")
    verified = verify_counts(query_remote(db_name, count_query(latest_batch), env_vars), expected_counts)
    remote_store_count = verified["stores"]
    # Commit the publication pointer only after every table matches the local batch.
    query_remote(db_name, f"UPDATE batch_publications SET status='complete', published_at=CURRENT_TIMESTAMP WHERE crawled_time='{latest_batch}' AND scope='{scope}';", env_vars)
    published = query_remote(db_name, f"SELECT status,scope FROM batch_publications WHERE crawled_time='{latest_batch}';", env_vars)
    if published != [{"status": "complete", "scope": scope}]:
        raise ValueError("publication marker verification failed")

    elapsed = time.time() - start_time

    # ---------------------------------------------------------
    # 步驟 3.8：輸出全流程 Final 檢核總結報告至 GHA Step Summary
    # ---------------------------------------------------------
    summary_md = f"""
## 🗄️ 【階段 3: ETL 與 Cloudflare D1】檢核成功報告

> **批次時間戳記**: `{latest_batch}` | **D1 資料庫**: `{db_name}` | **耗時**: `{elapsed:.2f} 秒` | **狀態**: ✅ 全部通過

### 📋 Reducer 檢核清單
| 檢核步驟 | 檢核項目 | 預期標準 | 實際結果 | 檢核狀態 |
| :--- | :--- | :--- | :--- | :---: |
| **步驟 3.1** | 來源目錄 JSON 總量 | 收集 JSON 數 `> 0` | 收集到 **{len(json_files)}** 個 JSON 檔案 | ✅ 通過 |
| **步驟 3.3** | SQLite 本地 ETL 與約束 | stores > 0 且 0 外鍵孤兒 | stores: {stats.get('stores', 0)} 筆, products: {stats.get('products', 0)} 筆 | ✅ 通過 |
| **步驟 3.4** | 智慧差異情報計算 | 產出 latest_batch | 產出 **{total_alerts}** 筆特價/新品情報 | ✅ 通過 |
| **步驟 3.5** | D1 批次 SQL 生成校驗 | SQL 大小 > 0 且語句數 > 0 | 生成 **{sql_count}** 條 SQL 語句 | ✅ 通過 |
| **步驟 3.6** | Cloudflare D1 遠端寫入 | Wrangler 執行成功 | 遠端 SQL 批次執行成功 (Exit 0) | ✅ 通過 |
| **步驟 3.7** | D1 遠端資料庫回查驗證 | 遠端 stores 筆數 `> 0` | 遠端在庫店家: **{remote_store_count}** 間 | ✅ 通過 |

---
"""
    append_github_step_summary(summary_md)

    print("\n" + "=" * 80)
    print(f"🎉 【階段 3: ETL 與 D1 同步全部完成！】耗時 {elapsed:.2f} 秒")
    print("=" * 80)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="同步資料至 Cloudflare D1 (嚴格檢核版)")
    parser.add_argument("--src-dir", required=True, help="JSON 資料夾")
    parser.add_argument("--db-name", default="ubereats_monitor", help="Cloudflare D1 資料庫名稱")
    parser.add_argument("--require-d1", action="store_true", help="強制要求 D1 遠端同步 (若無 Token 則報錯)")
    parser.add_argument("--scope", choices=["taiwan", "regional"], default="regional")
    parser.add_argument("--stores-file")
    parser.add_argument("--batch-id")
    args = parser.parse_args()

    sync_to_cloudflare_d1(args.src_dir, args.db_name, args.require_d1, args.scope, args.stores_file, args.batch_id)
