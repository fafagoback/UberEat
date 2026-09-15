"""Local-only prototype for losslessly packing the normalized serving database.

This intentionally has no Turso publishing code.  It stores canonical MessagePack
payloads compressed with Zstandard and a bucketed n-gram inverted index.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from collections import defaultdict
from pathlib import Path

import msgpack
import zstandard as zstd

TABLES = ("stores", "products", "events", "crawl_batches", "metadata")
SCHEMA = """
PRAGMA journal_mode=OFF;
PRAGMA synchronous=OFF;
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE store_directory(
  store_id INTEGER PRIMARY KEY,store_uuid TEXT UNIQUE NOT NULL,name TEXT NOT NULL,
  city TEXT,locality TEXT,rating REAL,review_count INTEGER,chunk_count INTEGER NOT NULL
);
CREATE TABLE store_bundles(
  store_id INTEGER NOT NULL,chunk_no INTEGER NOT NULL,codec TEXT NOT NULL,
  raw_bytes INTEGER NOT NULL,checksum TEXT NOT NULL,payload BLOB NOT NULL,
  PRIMARY KEY(store_id,chunk_no)
);
CREATE TABLE search_buckets(
  bucket_id INTEGER PRIMARY KEY,codec TEXT NOT NULL,raw_bytes INTEGER NOT NULL,
  checksum TEXT NOT NULL,payload BLOB NOT NULL
);
CREATE TABLE auxiliary_bundles(
  name TEXT PRIMARY KEY,codec TEXT NOT NULL,raw_bytes INTEGER NOT NULL,
  checksum TEXT NOT NULL,payload BLOB NOT NULL
);
"""


def columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def normalize(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")).casefold())


def tokens(value):
    s = normalize(value)
    out = set()
    # Full normalized value preserves exact short-query lookup.  Character and
    # n-grams support Chinese substring intersections and fuzzy candidate recall.
    if s:
        out.add("w:" + s)
    out.update("u:" + c for c in s)
    out.update("b:" + s[i:i + 2] for i in range(max(0, len(s) - 1)))
    out.update("t:" + s[i:i + 3] for i in range(max(0, len(s) - 2)))
    return out


def query_tokens(value):
    s = normalize(value)
    if len(s) >= 3:
        return ["t:" + s[i:i + 3] for i in range(len(s) - 2)]
    if len(s) == 2:
        return ["b:" + s]
    return ["u:" + s] if s else []


def packed(value, compressor):
    raw = msgpack.packb(value, use_bin_type=True)
    return raw, compressor.compress(raw), hashlib.sha256(raw).hexdigest()


def row_array(row, cols):
    # msgpack preserves None/int/float/str/bytes storage classes.
    return [row[c] for c in cols]


def source_digest(conn, table, order):
    cols = columns(conn, table)
    h = hashlib.sha256()
    count = 0
    for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}"):
        h.update(msgpack.packb(list(row), use_bin_type=True))
        count += 1
    return count, h.hexdigest()


def build(source, output, bucket_count=1024, level=10, max_items=2048):
    started = time.perf_counter()
    src = sqlite3.connect(source)
    src.row_factory = sqlite3.Row
    out_path = Path(output)
    if out_path.exists():
        out_path.unlink()
    dst = sqlite3.connect(output)
    dst.executescript(SCHEMA)
    compressor = zstd.ZstdCompressor(level=level)
    postings = [defaultdict(list) for _ in range(bucket_count)]
    sc, pc, ec = (columns(src, t) for t in ("stores", "products", "events"))
    product_gid = 0
    max_raw = max_blob = chunks = 0

    stores = list(src.execute("SELECT * FROM stores ORDER BY store_uuid"))
    for store_id, store in enumerate(stores):
        sid = store["store_uuid"]
        products = list(src.execute("SELECT * FROM products WHERE store_uuid=? ORDER BY product_uuid", (sid,)))
        events = list(src.execute("SELECT * FROM events WHERE store_uuid=? ORDER BY id", (sid,)))
        product_arrays = []
        store_terms = tokens(store["name"]) | tokens(store["city"]) | tokens(store["locality"]) | tokens(store["address"])
        for local_index, product in enumerate(products):
            product_gid += 1
            arr = row_array(product, pc)
            product_arrays.append([product_gid, arr])
            terms = store_terms | tokens(product["product_name"]) | tokens(product["category"]) | tokens(product["promo_type"])
            price = float(product["effective_price"] or product["price"] or 0)
            terms.add(f"f:price:{int(price // 50)}")
            if product["promo_type"] not in (None, "", "無"):
                terms.add("f:promo")
            if "discount_pct" in pc and float(product["discount_pct"] or 0) > 0:
                terms.add(f"f:discount:{int(float(product['discount_pct']) // 5)}")
            for term in terms:
                bid = int.from_bytes(hashlib.blake2s(term.encode(), digest_size=4).digest(), "big") % bucket_count
                # One integer identifies store and product without a row-per-product
                # lookup table (20 bits allows >1M products in one store).
                postings[bid][term].append((store_id << 20) | local_index)

        # Deterministic chunks keep pathological stores bounded. Store data is in
        # chunk zero; all product/event arrays remain present exactly once.
        pchunks = [product_arrays[i:i + max_items] for i in range(0, len(product_arrays), max_items)] or [[]]
        echunks = [[row_array(e, ec) for e in events[i:i + max_items]] for i in range(0, len(events), max_items)] or [[]]
        chunk_count = max(len(pchunks), len(echunks))
        dst.execute("INSERT INTO store_directory VALUES(?,?,?,?,?,?,?,?)", (
            store_id, sid, store["name"], store["city"], store["locality"], store["rating"], store["review_count"], chunk_count))
        for chunk_no in range(chunk_count):
            value = {
                "v": 1, "store_columns": sc if chunk_no == 0 else None,
                "store": row_array(store, sc) if chunk_no == 0 else None,
                "product_columns": pc if chunk_no == 0 else None,
                "products": pchunks[chunk_no] if chunk_no < len(pchunks) else [],
                "event_columns": ec if chunk_no == 0 else None,
                "events": echunks[chunk_no] if chunk_no < len(echunks) else [],
            }
            raw, blob, digest = packed(value, compressor)
            dst.execute("INSERT INTO store_bundles VALUES(?,?,?,?,?,?)", (store_id, chunk_no, "msgpack+zstd", len(raw), digest, blob))
            max_raw, max_blob, chunks = max(max_raw, len(raw)), max(max_blob, len(blob)), chunks + 1
        if store_id % 1000 == 0:
            print(f"packed stores {store_id:,}/{len(stores):,}", flush=True)

    for bid, terms in enumerate(postings):
        raw, blob, digest = packed(dict(terms), compressor)
        dst.execute("INSERT INTO search_buckets VALUES(?,?,?,?,?)", (bid, "msgpack+zstd", len(raw), digest, blob))

    auxiliary = {}
    for table in ("crawl_batches", "metadata"):
        auxiliary[table] = {"columns": columns(src, table), "rows": [list(r) for r in src.execute(f"SELECT * FROM {table} ORDER BY 1")]}
    raw, blob, digest = packed(auxiliary, compressor)
    dst.execute("INSERT INTO auxiliary_bundles VALUES(?,?,?,?,?)", ("normalized_auxiliary", "msgpack+zstd", len(raw), digest, blob))

    digests = {}
    for table, order in (("stores", "store_uuid"), ("products", "store_uuid,product_uuid"), ("events", "store_uuid,id"), ("crawl_batches", "batch_id"), ("metadata", "key")):
        count, digest = source_digest(src, table, order)
        digests[table] = {"count": count, "sha256": digest}
    info = {"format": 1, "source": str(source), "buckets": bucket_count, "chunks": chunks,
            "products": product_gid, "max_raw_bundle": max_raw, "max_compressed_bundle": max_blob,
            "source_tables": digests}
    for key, value in info.items():
        dst.execute("INSERT INTO metadata VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False, separators=(",", ":"))))
    dst.commit()
    dst.execute("VACUUM")
    dst.close(); src.close()
    info["database_bytes"] = out_path.stat().st_size
    info["build_seconds"] = round(time.perf_counter() - started, 3)
    info["packed_rows"] = chunks + len(stores) + bucket_count + 1 + len(info) - 2
    print(json.dumps(info, ensure_ascii=False, indent=2))


def unpack_blob(row, decompressor):
    raw = decompressor.decompress(row[0])
    if hashlib.sha256(raw).hexdigest() != row[1]:
        raise ValueError("bundle checksum mismatch")
    return msgpack.unpackb(raw, raw=False)


def verify(source, packed_db):
    started = time.perf_counter(); dec = zstd.ZstdDecompressor()
    src = sqlite3.connect(source); dst = sqlite3.connect(packed_db)
    expected = {r[0]: json.loads(r[1]) for r in dst.execute("SELECT key,value FROM metadata")}["source_tables"]
    hashes = {t: hashlib.sha256() for t in TABLES}; counts = defaultdict(int)
    for blob, checksum in dst.execute("SELECT payload,checksum FROM store_bundles ORDER BY store_id,chunk_no"):
        value = unpack_blob((blob, checksum), dec)
        if value["store"] is not None:
            hashes["stores"].update(msgpack.packb(value["store"], use_bin_type=True)); counts["stores"] += 1
        for _, row in value["products"]:
            hashes["products"].update(msgpack.packb(row, use_bin_type=True)); counts["products"] += 1
        for row in value["events"]:
            hashes["events"].update(msgpack.packb(row, use_bin_type=True)); counts["events"] += 1
    auxrow = dst.execute("SELECT payload,checksum FROM auxiliary_bundles WHERE name='normalized_auxiliary'").fetchone()
    aux = unpack_blob(auxrow, dec)
    for table in ("crawl_batches", "metadata"):
        for row in aux[table]["rows"]:
            hashes[table].update(msgpack.packb(row, use_bin_type=True)); counts[table] += 1
    result = {}
    for table in TABLES:
        result[table] = {"count": counts[table], "sha256": hashes[table].hexdigest(),
                         "expected": expected[table], "ok": counts[table] == expected[table]["count"] and hashes[table].hexdigest() == expected[table]["sha256"]}
    result["ok"] = all(result[t]["ok"] for t in TABLES)
    result["verify_seconds"] = round(time.perf_counter() - started, 3)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"]: raise SystemExit(1)


def main():
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("source"); b.add_argument("output"); b.add_argument("--buckets", type=int, default=1024); b.add_argument("--level", type=int, default=10); b.add_argument("--max-items", type=int, default=2048)
    v = sub.add_parser("verify"); v.add_argument("source"); v.add_argument("packed")
    args = ap.parse_args()
    if args.cmd == "build": build(args.source, args.output, args.buckets, args.level, args.max_items)
    else: verify(args.source, args.packed)


if __name__ == "__main__": main()
