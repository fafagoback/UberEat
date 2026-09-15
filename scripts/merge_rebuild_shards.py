# -*- coding: utf-8 -*-
"""Merge partitioned shard SQLite databases into a unified serving database."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.serving_state import SCHEMA, refresh_product_search

SHARD_RE = re.compile(r"shard_(\d+)\.db$")


def discover_shards(shards_dir: Path, expected_shards: int | None = None) -> list[tuple[int, Path]]:
    found: dict[int, Path] = {}
    for path in shards_dir.rglob("*.db"):
        match = SHARD_RE.search(path.name)
        if match:
            shard_id = int(match.group(1))
            found[shard_id] = path

    if not found:
        raise FileNotFoundError(f"No shard_*.db databases found in {shards_dir}")

    sorted_shards = sorted(found.items())
    if expected_shards is not None:
        missing = [i for i in range(expected_shards) if i not in found]
        if missing:
            raise ValueError(f"Missing expected shards {missing} in {shards_dir}")

    return sorted_shards


def merge_shards(shards_dir: Path, output_path: Path, expected_shards: int | None = None, replace: bool = False) -> dict[str, object]:
    output = output_path.resolve()
    if output.exists() and not replace:
        raise SystemExit(f"Refusing to replace existing database without --replace: {output}")

    shards = discover_shards(shards_dir, expected_shards)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".merging", dir=output.parent)
    os.close(fd)
    temp_db = Path(temp_name)
    temp_db.unlink(missing_ok=True)

    try:
        conn = sqlite3.connect(temp_db)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-64000")
        conn.execute("PRAGMA mmap_size=268435456")

        conn.executescript(SCHEMA)

        batch_aggregates: dict[str, list[Any]] = {}
        metadata_dict: dict[str, str] = {}

        merge_start = time.perf_counter()
        print(f"==================================================================", flush=True)
        print(f"[REDUCER START] Merging {len(shards)} shards into unified serving database", flush=True)
        print(f"==================================================================", flush=True)

        for idx, (shard_id, shard_path) in enumerate(shards, 1):
            t_shard_start = time.perf_counter()
            s_conn = sqlite3.connect(shard_path)
            s_conn.row_factory = sqlite3.Row
            for row in s_conn.execute("SELECT batch_id, processed_at, stores_seen, products_seen, fallback_stores, fallback_products FROM crawl_batches"):
                b_id = row["batch_id"]
                if b_id not in batch_aggregates:
                    batch_aggregates[b_id] = [row["processed_at"], 0, 0, 0, 0]
                batch_aggregates[b_id][1] += int(row["stores_seen"] or 0)
                batch_aggregates[b_id][2] += int(row["products_seen"] or 0)
                batch_aggregates[b_id][3] += int(row["fallback_stores"] or 0)
                batch_aggregates[b_id][4] += int(row["fallback_products"] or 0)

            if not metadata_dict:
                for row in s_conn.execute("SELECT key, value FROM metadata"):
                    metadata_dict[row["key"]] = row["value"]

            s_conn.close()

            conn.execute("ATTACH DATABASE ? AS shard", (str(shard_path),))
            conn.execute("""
                INSERT OR REPLACE INTO stores (
                    store_uuid, name, address, city, locality, latitude, longitude,
                    rating, review_count, order_url, first_seen, last_seen, status,
                    missing_streak, state_hash
                )
                SELECT
                    store_uuid, name, address, city, locality, latitude, longitude,
                    rating, review_count, order_url, first_seen, last_seen, status,
                    missing_streak, state_hash
                FROM shard.stores
            """)
            conn.execute("""
                INSERT OR REPLACE INTO products (
                    store_uuid, product_uuid, product_name, category, description,
                    price, quantity, promo_type, effective_price, order_url, first_seen,
                    last_seen, status, missing_streak, state_hash, recent_prices,
                    price_novel_vs_previous_3, reference_price, discount_amount,
                    discount_pct, is_price_deal
                )
                SELECT
                    store_uuid, product_uuid, product_name, category, description,
                    price, quantity, promo_type, effective_price, order_url, first_seen,
                    last_seen, status, missing_streak, state_hash, recent_prices,
                    price_novel_vs_previous_3, reference_price, discount_amount,
                    discount_pct, is_price_deal
                FROM shard.products
            """)
            conn.execute("""
                INSERT INTO events (
                    event_time, store_uuid, product_uuid, event_type, old_state, new_state
                )
                SELECT
                    event_time, store_uuid, product_uuid, event_type, old_state, new_state
                FROM shard.events
                ORDER BY event_time ASC, id ASC
            """)
            conn.commit()
            conn.execute("DETACH DATABASE shard")
            t_shard = time.perf_counter() - t_shard_start
            print(f"[{idx}/{len(shards)}] Shard {shard_id} attached and merged in {t_shard:.2f}s", flush=True)

        # Insert aggregated batches
        for b_id, vals in sorted(batch_aggregates.items()):
            conn.execute(
                "INSERT OR REPLACE INTO crawl_batches VALUES (?, ?, ?, ?, ?, ?)",
                (b_id, vals[0], vals[1], vals[2], vals[3], vals[4]),
            )

        # Insert metadata
        for k, v in metadata_dict.items():
            if k not in ("rebuild_shard_id", "rebuild_total_shards"):
                conn.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (k, v))
        conn.execute("INSERT OR REPLACE INTO metadata VALUES ('rebuild_merged_shards', ?)", (str(len(shards)),))

        # Refresh FTS search
        print(f"[REDUCER] Refreshing FTS search index...", flush=True)
        t_fts_start = time.perf_counter()
        refresh_product_search(conn)
        t_fts = time.perf_counter() - t_fts_start
        print(f"[REDUCER] FTS search index refreshed in {t_fts:.2f}s", flush=True)

        # Re-enable and verify foreign keys
        t_fk_start = time.perf_counter()
        conn.execute("PRAGMA foreign_keys=ON")
        fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_errors:
            raise RuntimeError(f"Foreign key violations found after merge: {fk_errors[:5]}")
        t_fk = time.perf_counter() - t_fk_start
        print(f"[REDUCER] Foreign key integrity check passed in {t_fk:.2f}s", flush=True)

        conn.commit()
        total_merge_time = time.perf_counter() - merge_start
        summary = {
            "shards_merged": len(shards),
            "batches": conn.execute("SELECT count(*) FROM crawl_batches").fetchone()[0],
            "stores": conn.execute("SELECT count(*) FROM stores").fetchone()[0],
            "products": conn.execute("SELECT count(*) FROM products").fetchone()[0],
            "events": conn.execute("SELECT count(*) FROM events").fetchone()[0],
            "merge_duration_seconds": round(total_merge_time, 2),
        }
        conn.close()

        for suffix in ("-wal", "-shm"):
            Path(str(temp_db) + suffix).unlink(missing_ok=True)
        if output.exists():
            output.unlink()
        shutil.move(str(temp_db), str(output))
        print("==================================================================", flush=True)
        print(f"[REDUCER COMPLETE] Total Merge Time: {total_merge_time:.2f}s | Shards: {len(shards)}", flush=True)
        print(f"Final Stats: batches={summary['batches']:,}, stores={summary['stores']:,}, products={summary['products']:,}, events={summary['events']:,}", flush=True)
        print("==================================================================", flush=True)
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        return summary
    except BaseException:
        try:
            conn.close()
        except Exception:
            pass
        temp_db.unlink(missing_ok=True)
        Path(str(temp_db) + "-wal").unlink(missing_ok=True)
        Path(str(temp_db) + "-shm").unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge rebuild shards into single serving.db")
    parser.add_argument("--shards-dir", required=True, help="Directory containing shard_*.db files")
    parser.add_argument("--output", required=True, help="Output serving.db path")
    parser.add_argument("--expected-shards", type=int, help="Verify that exactly this many shards exist")
    parser.add_argument("--replace", action="store_true", help="Replace output file if it exists")
    args = parser.parse_args()
    merge_shards(Path(args.shards_dir), Path(args.output), args.expected_shards, args.replace)


if __name__ == "__main__":
    main()
