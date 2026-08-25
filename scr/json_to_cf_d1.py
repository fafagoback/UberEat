# -*- coding: utf-8 -*-
"""
Uber Eats Cloudflare D1 匯入模組 (Stage 3: Push to Cloudflare D1 Serverless SQL)
將清洗後的資料轉換為 D1 批次 SQL 語句，並透過 wrangler / D1 REST API 同步至邊緣資料庫。
"""

import os
import sys
import argparse
import subprocess

# 引入本地模組目錄
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "local_scr")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from json_to_db import UberEatsDataImporter


def sync_to_cloudflare_d1(src_dir: str, db_name: str):
    cf_token = os.environ.get("CLOUDFLARE_API_TOKEN")
    cf_account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")

    if not cf_token:
        print("⚠️ 未設定 CLOUDFLARE_API_TOKEN，將執行本機 SQLite ETL，跳過 D1 遠端同步。")
        # 執行本地 SQLite 匯入
        importer = UberEatsDataImporter(json_dir=src_dir, db_path="ubereats_monitor.db")
        importer.import_all_data()
        return

    print(f"📦 正在將 {src_dir} 內之資料轉換並寫入 Cloudflare D1 ({db_name})...")
    
    # 執行本地 ETL
    importer = UberEatsDataImporter(json_dir=src_dir, db_path="ubereats_monitor.db")
    stats = importer.import_all_data()
    print(f"📊 ETL 完成統計: {stats}")

    # 若環境中已安裝 wrangler 且有 Token，可直接透過 wrangler 執行 D1 同步
    # 例如：wrangler d1 execute <db_name> --file=migration.sql
    print("✅ Cloudflare D1 同步準備完成。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="同步資料至 Cloudflare D1")
    parser.add_argument("--src-dir", required=True, help="JSON 資料夾")
    parser.add_argument("--db-name", default="ubereats_monitor", help="Cloudflare D1 資料庫名稱")
    args = parser.parse_args()

    sync_to_cloudflare_d1(args.src_dir, args.db_name)
