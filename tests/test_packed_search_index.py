import hashlib
import sqlite3
import sys
from pathlib import Path

import msgpack
import zstandard as zstd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from prototype_pack_db import build


def _bucket(term: str, count: int) -> int:
    return int.from_bytes(hashlib.blake2s(term.encode(), digest_size=4).digest(), "big") % count


def _terms(conn, term, count=32):
    row = conn.execute("select payload from search_buckets where bucket_id=?", (_bucket(term, count),)).fetchone()
    return msgpack.unpackb(zstd.ZstdDecompressor().decompress(row[0]), raw=False).get(term, [])


def test_packed_search_indexes_only_active_products_and_quantity_promos(tmp_path):
    source = tmp_path / "source.db"
    packed = tmp_path / "packed.db"
    conn = sqlite3.connect(source)
    conn.executescript("""
    create table stores(store_uuid text primary key,name text,city text,locality text,address text,
      latitude real,longitude real,rating real,review_count integer,order_url text,first_seen text,
      last_seen text,status text,missing_streak integer,state_hash text,is_open integer);
    create table products(store_uuid text,product_uuid text,product_name text,category text,description text,
      price real,quantity integer,promo_type text,effective_price real,order_url text,first_seen text,last_seen text,
      status text,missing_streak integer,state_hash text,recent_prices text,price_novel_vs_previous_3 integer,
      reference_price real,discount_amount real,discount_pct real,is_price_deal integer,is_open integer,
      primary key(store_uuid,product_uuid));
    create table events(id integer primary key,event_time text,store_uuid text,product_uuid text,event_type text,old_state text,new_state text);
    create table crawl_batches(batch_id text primary key,processed_at text,stores_seen integer,products_seen integer,fallback_stores integer,fallback_products integer);
    create table metadata(key text primary key,value text);
    """)
    store = ("s1", "店", "台北市", "信義區", "地址", 25.0, 121.0, 4.5, 10, "", "2026-09-01", "2026-09-22", "active", 0, "h", 1)
    conn.execute("insert into stores values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", store)
    base = ("s1", "p1", "套餐", "主餐", "", 100, 2, "無", 50, "", "2026-09-22", "2026-09-22", "active", 0, "h", "[]", 0, None, 0, 0, 0, 1)
    conn.execute("insert into products values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", base)
    conn.execute("insert into products values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple("inactive" if i == 12 else ("p2" if i == 1 else v) for i, v in enumerate(base)))
    conn.commit(); conn.close()

    build(str(source), str(packed), bucket_count=32)
    out = sqlite3.connect(packed)
    assert len(_terms(out, "f:catalog")) == 1
    assert len(_terms(out, "f:promo")) == 1
    out.close()
