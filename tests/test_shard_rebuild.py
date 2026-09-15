# -*- coding: utf-8 -*-
import io
import json
import sqlite3
import tarfile
from argparse import Namespace
from pathlib import Path

import pytest

from src.rebuild_database import rebuild
from scripts.merge_rebuild_shards import merge_shards

STORES = [
    ("11111111-1111-4111-8111-111111111111", "Store Alpha"),
    ("22222222-2222-4222-8222-222222222222", "Store Beta"),
    ("33333333-3333-4333-8333-333333333333", "Store Gamma"),
    ("44444444-4444-4444-8444-444444444444", "Store Delta"),
]


def write_multi_store_snapshot(root: Path, batch: str, prices: dict[str, float]):
    path = root / batch / f"taiwan_menus_{batch}.tar.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as archive:
        manifest = {"batch_id": batch, "store_count": len(STORES)}
        raw_m = json.dumps(manifest).encode()
        info_m = tarfile.TarInfo("manifest.json")
        info_m.size = len(raw_m)
        archive.addfile(info_m, io.BytesIO(raw_m))

        for s_id, s_name in STORES:
            doc = {
                "store_uuid": s_id,
                "name": s_name,
                "geo": {"latitude": 25.033, "longitude": 121.5654},
                "hasMenu": {
                    "hasMenuSection": [{
                        "name": "餐點",
                        "hasMenuItem": [{
                            "identifier": f"prod-{s_id[:8]}",
                            "name": f"Item for {s_name}",
                            "offers": {"price": str(prices.get(s_id, 100))},
                        }]
                    }]
                }
            }
            raw_d = json.dumps(doc).encode()
            info_d = tarfile.TarInfo(f"Json/{s_id}.json")
            info_d.size = len(raw_d)
            archive.addfile(info_d, io.BytesIO(raw_d))


def test_sharded_rebuild_and_merge_matches_single_rebuild(tmp_path):
    snapshots_dir = tmp_path / "snapshots"
    batches = ["20260901120000", "20260902120000", "20260903120000"]
    prices_by_batch = {
        "20260901120000": {s[0]: 100 for s in STORES},
        "20260902120000": {STORES[0][0]: 80, STORES[1][0]: 120, STORES[2][0]: 100, STORES[3][0]: 90},
        "20260903120000": {STORES[0][0]: 80, STORES[1][0]: 70, STORES[2][0]: 100, STORES[3][0]: 90},
    }

    for b in batches:
        write_multi_store_snapshot(snapshots_dir, b, prices_by_batch[b])

    # 1. Run single unpartitioned rebuild
    single_db = tmp_path / "single.db"
    rebuild(Namespace(
        source_dir=str(snapshots_dir), repo_id=None, database=str(single_db),
        replace=False, missing_threshold=3, minimum_stores=1, max_snapshots=None,
        shard_id=None, total_shards=1,
    ))

    # 2. Run 3 shards
    shards_dir = tmp_path / "shards"
    total_shards = 3
    for s_id in range(total_shards):
        shard_db = shards_dir / f"shard_{s_id}.db"
        rebuild(Namespace(
            source_dir=str(snapshots_dir), repo_id=None, database=str(shard_db),
            replace=False, missing_threshold=3, minimum_stores=1, max_snapshots=None,
            shard_id=s_id, total_shards=total_shards,
        ))

    # Verify shards partition stores disjointly
    all_sharded_stores = []
    for s_id in range(total_shards):
        c = sqlite3.connect(shards_dir / f"shard_{s_id}.db")
        stores = [r[0] for r in c.execute("SELECT store_uuid FROM stores").fetchall()]
        all_sharded_stores.extend(stores)
        c.close()

    assert len(all_sharded_stores) == len(STORES)
    assert set(all_sharded_stores) == {s[0] for s in STORES}

    # 3. Merge shards
    merged_db = tmp_path / "merged.db"
    merge_summary = merge_shards(shards_dir, merged_db, expected_shards=3, replace=False)
    assert merge_summary["shards_merged"] == 3
    assert merge_summary["batches"] == len(batches)
    assert merge_summary["stores"] == len(STORES)

    # 4. Compare single vs merged
    c_single = sqlite3.connect(single_db)
    c_merged = sqlite3.connect(merged_db)

    # Batches
    single_batches = c_single.execute("SELECT * FROM crawl_batches ORDER BY batch_id").fetchall()
    merged_batches = c_merged.execute("SELECT * FROM crawl_batches ORDER BY batch_id").fetchall()
    assert single_batches == merged_batches

    # Stores
    single_stores = c_single.execute("SELECT store_uuid, name, latitude, longitude, status FROM stores ORDER BY store_uuid").fetchall()
    merged_stores = c_merged.execute("SELECT store_uuid, name, latitude, longitude, status FROM stores ORDER BY store_uuid").fetchall()
    assert single_stores == merged_stores

    # Products
    single_products = c_single.execute("SELECT store_uuid, product_uuid, price, effective_price, status FROM products ORDER BY store_uuid, product_uuid").fetchall()
    merged_products = c_merged.execute("SELECT store_uuid, product_uuid, price, effective_price, status FROM products ORDER BY store_uuid, product_uuid").fetchall()
    assert single_products == merged_products

    # Events count and types
    single_events = c_single.execute("SELECT store_uuid, product_uuid, event_type, old_state, new_state FROM events ORDER BY store_uuid, event_time, event_type").fetchall()
    merged_events = c_merged.execute("SELECT store_uuid, product_uuid, event_type, old_state, new_state FROM events ORDER BY store_uuid, event_time, event_type").fetchall()
    assert single_events == merged_events

    # FTS
    single_search = c_single.execute("SELECT store_name, product_name FROM product_search ORDER BY store_name").fetchall()
    merged_search = c_merged.execute("SELECT store_name, product_name FROM product_search ORDER BY store_name").fetchall()
    assert single_search == merged_search

    c_single.close()
    c_merged.close()
