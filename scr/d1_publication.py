"""Fail-closed D1 queries and publication metadata shared by import and recovery."""
import json
import os
import subprocess

TABLES = ("stores", "products", "store_cuisines", "store_business_hours", "alerts_history")

PUBLICATION_DDL = """
CREATE TABLE IF NOT EXISTS batch_publications (
    crawled_time TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK(scope IN ('taiwan', 'regional')),
    status TEXT NOT NULL CHECK(status IN ('staging', 'complete')),
    source_run_id TEXT NOT NULL,
    published_at TEXT
);
CREATE TABLE IF NOT EXISTS publication_legacy_baseline (crawled_time TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS publication_migrations (name TEXT PRIMARY KEY);
CREATE VIEW IF NOT EXISTS published_products AS
 SELECT p.* FROM products p LEFT JOIN batch_publications b USING(crawled_time)
 WHERE (b.status='complete' AND b.scope='taiwan') OR
 (b.crawled_time IS NULL AND p.crawled_time IN (SELECT crawled_time FROM publication_legacy_baseline));
CREATE VIEW IF NOT EXISTS published_stores AS
 SELECT s.* FROM stores s LEFT JOIN batch_publications b USING(crawled_time)
 WHERE (b.status='complete' AND b.scope='taiwan') OR
 (b.crawled_time IS NULL AND s.crawled_time IN (SELECT crawled_time FROM publication_legacy_baseline));
"""


def parse_results(stdout):
    payload = json.loads(stdout)
    if not isinstance(payload, list) or len(payload) != 1:
        raise ValueError("expected exactly one D1 query response")
    result = payload[0]
    if result.get("success") is not True or not isinstance(result.get("results"), list):
        raise ValueError("D1 query did not return successful structured results")
    return result["results"]


def query_remote(db_name, sql, env):
    command = ["npx.cmd" if os.name == "nt" else "npx", "wrangler", "d1", "execute", db_name,
               "--remote", "--json", "--command", sql]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", env=env, timeout=180)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout)[-1500:])
    return parse_results(result.stdout)


def count_query(batch_id):
    if len(batch_id) != 14 or not batch_id.isdigit():
        raise ValueError("invalid batch ID")
    return "SELECT " + ", ".join(
        f"(SELECT count(*) FROM {table} WHERE crawled_time='{batch_id}') AS {table}" for table in TABLES
    ) + ";"


def verify_counts(rows, expected):
    if len(rows) != 1 or any(type(rows[0].get(table)) is not int for table in TABLES):
        raise ValueError("missing or invalid D1 counts")
    if rows[0] != expected or expected["stores"] <= 0:
        raise ValueError(f"D1 count mismatch: expected={expected}, actual={rows[0]}")
    return rows[0]
