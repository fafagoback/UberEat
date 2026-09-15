# -*- coding: utf-8 -*-
"""Build the normalized CURRENT/EVENTS serving database from one raw crawl.

Identity is taken from Uber's ``store_uuid`` and menu item's ``identifier``.
Fallback identities are deliberately namespaced and counted in the batch report.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import statistics
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import orjson

    def json_loads(data: bytes | str) -> Any:
        return orjson.loads(data)
except ImportError:
    def json_loads(data: bytes | str) -> Any:
        return json.loads(data)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


TW = timezone(timedelta(hours=8))
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
DEFAULT_MISSING_THRESHOLD = int(os.getenv("MISSING_STREAK_THRESHOLD", "3"))
EVENT_RETENTION_DAYS = int(os.getenv("EVENT_RETENTION_DAYS", "0"))


def canonical_hash(value: dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def fallback_id(kind: str, value: str) -> str:
    return f"fallback:{kind}:{hashlib.sha256(value.encode()).hexdigest()[:32]}"


def store_identity(doc: dict[str, Any]) -> tuple[str, bool]:
    value = str(doc.get("store_uuid") or "").strip().lower()
    if UUID_RE.match(value):
        return value, False
    url = str(doc.get("@id") or doc.get("potentialAction", {}).get("target", {}).get("urlTemplate") or "")
    # Only legacy HTML fallback documents should reach this branch.
    return fallback_id("store-url", url), True


def product_identity(item: dict[str, Any], store_uuid: str) -> tuple[str, bool]:
    value = str(item.get("identifier") or "").strip().lower()
    if UUID_RE.match(value):
        return value, False
    # Uber HTML JSON-LD does not always expose an item UUID. Keep the fallback
    # stable within a store and visibly namespaced; it is never confused with UUID.
    name = html.unescape(str(item.get("name") or "")).strip()
    return fallback_id("item-name", f"{store_uuid}\x1f{name}"), True


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS crawl_batches(
  batch_id TEXT PRIMARY KEY, processed_at TEXT NOT NULL, stores_seen INTEGER NOT NULL,
  products_seen INTEGER NOT NULL, fallback_stores INTEGER NOT NULL, fallback_products INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS stores(
  store_uuid TEXT PRIMARY KEY, name TEXT NOT NULL, address TEXT, city TEXT, locality TEXT,
  latitude REAL, longitude REAL, rating REAL, review_count INTEGER, order_url TEXT, first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL, status TEXT NOT NULL, missing_streak INTEGER NOT NULL DEFAULT 0,
  state_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products(
  store_uuid TEXT NOT NULL, product_uuid TEXT NOT NULL, product_name TEXT NOT NULL,
  category TEXT, description TEXT, price REAL NOT NULL, quantity INTEGER NOT NULL DEFAULT 1,
  promo_type TEXT, effective_price REAL NOT NULL, order_url TEXT, first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL, status TEXT NOT NULL, missing_streak INTEGER NOT NULL DEFAULT 0,
  state_hash TEXT NOT NULL, recent_prices TEXT NOT NULL DEFAULT '[]',
  price_novel_vs_previous_3 INTEGER NOT NULL DEFAULT 0,
  reference_price REAL, discount_amount REAL NOT NULL DEFAULT 0,
  discount_pct REAL NOT NULL DEFAULT 0, is_price_deal INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(store_uuid, product_uuid),
  FOREIGN KEY(store_uuid) REFERENCES stores(store_uuid)
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, event_time TEXT NOT NULL, store_uuid TEXT NOT NULL,
  product_uuid TEXT, event_type TEXT NOT NULL, old_state TEXT, new_state TEXT
);
CREATE INDEX IF NOT EXISTS idx_stores_first_seen ON stores(first_seen);
CREATE INDEX IF NOT EXISTS idx_stores_city ON stores(city);
CREATE INDEX IF NOT EXISTS idx_stores_name ON stores(name);
CREATE INDEX IF NOT EXISTS idx_products_first_seen ON products(first_seen);
CREATE INDEX IF NOT EXISTS idx_products_price ON products(effective_price, price);
CREATE INDEX IF NOT EXISTS idx_products_promo ON products(promo_type);
CREATE INDEX IF NOT EXISTS idx_products_name ON products(product_name);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
CREATE INDEX IF NOT EXISTS idx_events_product_time ON events(store_uuid, product_uuid, event_time DESC);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(event_time);
CREATE INDEX IF NOT EXISTS idx_events_type_time ON events(event_type, event_time DESC);
CREATE VIRTUAL TABLE IF NOT EXISTS product_search USING fts5(
  store_uuid UNINDEXED, product_uuid UNINDEXED, store_name, product_name, category,
  tokenize='unicode61'
);
"""


def iter_documents(source: str | Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    if not isinstance(source, (str, Path)):
        yield from source
        return
    p = Path(source)
    if p.is_dir():
        for f in p.rglob("*.json"):
            try:
                doc = json_loads(f.read_bytes())
                if isinstance(doc, dict) and (doc.get("store_uuid") or doc.get("hasMenu")):
                    yield doc
            except (OSError, ValueError, TypeError):
                continue
    elif tarfile.is_tarfile(p):
        with tarfile.open(p, "r:*") as tf:
            for member in tf:
                if member.isfile() and member.name.endswith(".json"):
                    fh = tf.extractfile(member)
                    if fh:
                        try:
                            doc = json_loads(fh.read())
                            if isinstance(doc, dict) and (doc.get("store_uuid") or doc.get("hasMenu")):
                                yield doc
                        except (OSError, ValueError, TypeError):
                            continue
    else:
        raise ValueError(f"unsupported raw source: {source}")


def _num(value: Any, typ=float, default=0):
    try:
        return typ(value)
    except (TypeError, ValueError):
        return default


def _event(conn: sqlite3.Connection, now: str, sid: str, pid: str | None, kind: str, old, new):
    conn.execute(
        "INSERT INTO events(event_time,store_uuid,product_uuid,event_type,old_state,new_state) VALUES(?,?,?,?,?,?)",
        (now, sid, pid, kind, json_dumps(old) if old is not None else None,
         json_dumps(new) if new is not None else None),
    )


def refresh_product_search(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM product_search")
    conn.execute("INSERT INTO product_search SELECT p.store_uuid,p.product_uuid,s.name,p.product_name,p.category FROM products p JOIN stores s USING(store_uuid) WHERE p.status='active'")


def ensure_schema_and_migrations(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    store_columns = {row[1] for row in conn.execute("PRAGMA table_info(stores)")}
    for name in ("latitude", "longitude"):
        if name not in store_columns:
            conn.execute(f"ALTER TABLE stores ADD COLUMN {name} REAL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stores_coordinates ON stores(latitude, longitude)")
    product_columns = {row[1] for row in conn.execute("PRAGMA table_info(products)")}
    if "recent_prices" not in product_columns:
        conn.execute("ALTER TABLE products ADD COLUMN recent_prices TEXT NOT NULL DEFAULT '[]'")
    if "price_novel_vs_previous_3" not in product_columns:
        conn.execute("ALTER TABLE products ADD COLUMN price_novel_vs_previous_3 INTEGER NOT NULL DEFAULT 0")
    for name, definition in (
        ("reference_price", "REAL"),
        ("discount_amount", "REAL NOT NULL DEFAULT 0"),
        ("discount_pct", "REAL NOT NULL DEFAULT 0"),
        ("is_price_deal", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in product_columns:
            conn.execute(f"ALTER TABLE products ADD COLUMN {name} {definition}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_deals ON products(is_price_deal, discount_pct DESC, discount_amount DESC)")


class ServingStateCache:
    """In-memory cache for stores and products to avoid millions of SQLite roundtrips."""

    def __init__(self, conn: sqlite3.Connection | None = None):
        self.stores: dict[str, dict[str, Any]] = {}
        self.products: dict[tuple[str, str], dict[str, Any]] = {}
        self.active_store_keys: set[str] = set()
        self.active_product_keys: set[tuple[str, str]] = set()
        if conn is not None:
            ensure_schema_and_migrations(conn)
            self.load(conn)

    def load(self, conn: sqlite3.Connection) -> None:
        cursor = conn.cursor()
        has_stores = cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='stores'").fetchone()
        if not has_stores:
            return

        cursor.execute(
            "SELECT store_uuid, name, address, city, locality, latitude, longitude, "
            "rating, review_count, order_url, first_seen, last_seen, status, missing_streak, state_hash FROM stores"
        )
        for row in cursor.fetchall():
            sid = row[0]
            sdata = {
                "store_uuid": sid, "name": row[1], "address": row[2], "city": row[3], "locality": row[4],
                "latitude": row[5], "longitude": row[6], "rating": row[7], "review_count": row[8],
                "order_url": row[9], "first_seen": row[10], "last_seen": row[11], "status": row[12],
                "missing_streak": row[13], "state_hash": row[14],
            }
            self.stores[sid] = sdata
            if row[12] == "active":
                self.active_store_keys.add(sid)

        cursor.execute(
            "SELECT store_uuid, product_uuid, product_name, category, description, price, "
            "quantity, promo_type, effective_price, order_url, first_seen, last_seen, "
            "status, missing_streak, state_hash, recent_prices, price_novel_vs_previous_3, "
            "reference_price, discount_amount, discount_pct, is_price_deal FROM products"
        )
        for row in cursor.fetchall():
            key = (row[0], row[1])
            try:
                rec_prices = [float(v) for v in (json_loads(row[15]) if row[15] else [])]
            except Exception:
                rec_prices = []
            pdata = {
                "store_uuid": row[0], "product_uuid": row[1], "product_name": row[2],
                "category": row[3], "description": row[4], "price": row[5], "quantity": row[6],
                "promo_type": row[7], "effective_price": row[8], "order_url": row[9],
                "first_seen": row[10], "last_seen": row[11], "status": row[12],
                "missing_streak": row[13], "state_hash": row[14], "recent_prices": rec_prices,
                "price_novel_vs_previous_3": row[16], "reference_price": row[17],
                "discount_amount": row[18], "discount_pct": row[19], "is_price_deal": row[20],
            }
            self.products[key] = pdata
            if row[12] == "active":
                self.active_product_keys.add(key)


def apply_snapshot(conn: sqlite3.Connection, docs: Iterable[dict[str, Any]], batch_id: str,
                   missing_threshold: int = DEFAULT_MISSING_THRESHOLD, baseline: bool | None = None,
                   event_retention_days: int | None = None,
                   refresh_search: bool = True,
                   state_cache: ServingStateCache | None = None) -> dict[str, int]:
    conn.executescript(SCHEMA)
    # Keep existing serving databases in place and backfill coordinates as stores
    # appear in subsequent snapshots.
    store_columns = {row[1] for row in conn.execute("PRAGMA table_info(stores)")}
    for name in ("latitude", "longitude"):
        if name not in store_columns:
            conn.execute(f"ALTER TABLE stores ADD COLUMN {name} REAL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stores_coordinates ON stores(latitude, longitude)")
    # Forward-compatible migration for serving.db files cached before the
    # three-snapshot price rule was introduced.
    product_columns = {row[1] for row in conn.execute("PRAGMA table_info(products)")}
    if "recent_prices" not in product_columns:
        conn.execute("ALTER TABLE products ADD COLUMN recent_prices TEXT NOT NULL DEFAULT '[]'")
    if "price_novel_vs_previous_3" not in product_columns:
        conn.execute("ALTER TABLE products ADD COLUMN price_novel_vs_previous_3 INTEGER NOT NULL DEFAULT 0")
    for name, definition in (
        ("reference_price", "REAL"),
        ("discount_amount", "REAL NOT NULL DEFAULT 0"),
        ("discount_pct", "REAL NOT NULL DEFAULT 0"),
        ("is_price_deal", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in product_columns:
            conn.execute(f"ALTER TABLE products ADD COLUMN {name} {definition}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_deals ON products(is_price_deal, discount_pct DESC, discount_amount DESC)")
    now = datetime.strptime(batch_id, "%Y%m%d%H%M%S").replace(tzinfo=TW).isoformat()
    latest = conn.execute("SELECT value FROM metadata WHERE key='latest_batch'").fetchone()
    if latest and batch_id <= latest[0]:
        raise ValueError(f"snapshot batch {batch_id} is not newer than latest batch {latest[0]}")
    if baseline is None:
        baseline = conn.execute("SELECT COUNT(*)=0 FROM crawl_batches").fetchone()[0] == 1

    if state_cache is None:
        state_cache = ServingStateCache(conn)

    seen_stores: set[str] = set()
    seen_products: set[tuple[str, str]] = set()
    counts = {k: 0 for k in ("stores", "products", "new", "changed", "reappeared", "removed", "unchanged", "store_new", "store_changed", "store_reappeared", "store_removed", "fallback_stores", "fallback_products")}

    insert_stores_batch: list[tuple] = []
    update_stores_batch: list[tuple] = []
    insert_products_batch: list[tuple] = []
    update_products_batch: list[tuple] = []
    events_batch: list[tuple] = []

    for doc in docs:
        sid, sfallback = store_identity(doc)
        counts["fallback_stores"] += int(sfallback)
        if sid in seen_stores:
            continue
        seen_stores.add(sid)
        counts["stores"] += 1

        address = doc.get("address") or {}
        rating = doc.get("aggregateRating") or {}
        geo = doc.get("geo") or {}
        order_url = str(doc.get("potentialAction", {}).get("target", {}).get("urlTemplate") or doc.get("@id") or "")
        sstate = {
            "name": str(doc.get("name") or ""),
            "address": str(address.get("streetAddress") or ""),
            "city": str(address.get("addressRegion") or address.get("addressLocality") or ""),
            "locality": str(address.get("addressLocality") or ""),
            "latitude": _num(geo.get("latitude"), float, None),
            "longitude": _num(geo.get("longitude"), float, None),
            "rating": _num(rating.get("ratingValue"), float, None),
            "review_count": _num(rating.get("reviewCount"), int, None),
            "order_url": order_url,
        }

        old_s = state_cache.stores.get(sid)
        if old_s:
            for coordinate in ("latitude", "longitude"):
                if sstate[coordinate] is None:
                    sstate[coordinate] = old_s[coordinate]
        sh = canonical_hash(sstate)

        if old_s:
            if old_s["status"] != "active":
                counts["store_reappeared"] += 1
                if not baseline:
                    events_batch.append((now, sid, None, "STORE_REAPPEARED", json_dumps({"status": old_s["status"]}), json_dumps({"status": "active"})))
            if old_s["state_hash"] != sh:
                counts["store_changed"] += 1
                if not baseline:
                    events_batch.append((now, sid, None, "STORE_CHANGED", json_dumps(dict(old_s)), json_dumps(sstate)))
            update_stores_batch.append((*sstate.values(), now, "active", sh, sid))
            old_s.update(sstate)
            old_s["last_seen"] = now
            old_s["status"] = "active"
            old_s["missing_streak"] = 0
            old_s["state_hash"] = sh
            state_cache.active_store_keys.add(sid)
        else:
            insert_stores_batch.append((sid, *sstate.values(), now, now, "active", 0, sh))
            counts["store_new"] += 1
            if not baseline:
                events_batch.append((now, sid, None, "STORE_NEW", None, json_dumps(sstate)))
            state_cache.stores[sid] = {
                "store_uuid": sid, **sstate, "first_seen": now, "last_seen": now,
                "status": "active", "missing_streak": 0, "state_hash": sh
            }
            state_cache.active_store_keys.add(sid)

        sections = (doc.get("hasMenu") or {}).get("hasMenuSection") or []
        for sec in sections:
            category = html.unescape(str(sec.get("name") or "一般"))
            for item in sec.get("hasMenuItem") or []:
                name = html.unescape(str(item.get("name") or "")).strip()
                if not name:
                    continue
                pid, pfallback = product_identity(item, sid)
                counts["fallback_products"] += int(pfallback)
                key = (sid, pid)
                if key in seen_products:
                    continue
                seen_products.add(key)
                counts["products"] += 1

                price = _num((item.get("offers") or {}).get("price"), float, 0)
                desc = str(item.get("description") or "")
                promo = str(item.get("promo_type") or "無")
                qty = max(1, _num(item.get("quantity"), int, 1))
                effective = _num(item.get("effective_price"), float, round(price / qty, 2))
                state = {
                    "product_name": name, "category": category, "description": desc,
                    "price": price, "quantity": qty, "promo_type": promo,
                    "effective_price": effective, "order_url": order_url,
                }
                ph = canonical_hash(state)
                old = state_cache.products.get(key)

                if old is None:
                    insert_products_batch.append((sid, pid, *state.values(), now, now, "active", 0, ph, json_dumps([price]), 0, None, 0, 0, 0))
                    counts["new"] += 1
                    if not baseline:
                        events_batch.append((now, sid, pid, "NEW", None, json_dumps(state)))
                    state_cache.products[key] = {
                        "store_uuid": sid, "product_uuid": pid, **state,
                        "first_seen": now, "last_seen": now, "status": "active",
                        "missing_streak": 0, "state_hash": ph, "recent_prices": [price],
                        "price_novel_vs_previous_3": 0, "reference_price": None,
                        "discount_amount": 0, "discount_pct": 0, "is_price_deal": 0,
                    }
                    state_cache.active_product_keys.add(key)
                else:
                    old_state = dict(old)
                    old_state["recent_prices"] = json_dumps(old["recent_prices"])
                    previous_prices = old["recent_prices"][-3:]
                    price_novel = int(len(previous_prices) == 3 and price not in previous_prices)
                    reference_price = float(statistics.median(previous_prices)) if len(previous_prices) == 3 else None
                    discount_amount = round(reference_price - price, 2) if price_novel and reference_price and price < reference_price else 0
                    discount_pct = round(discount_amount / reference_price * 100, 2) if discount_amount and reference_price else 0
                    is_price_deal = int(discount_amount > 0)
                    next_recent_prices = (previous_prices + [price])[-3:]

                    if old["status"] != "active":
                        counts["reappeared"] += 1
                        if not baseline:
                            events_batch.append((now, sid, pid, "REAPPEARED", json_dumps({"status": old["status"]}), json_dumps({"status": "active"})))
                    if old["state_hash"] != ph:
                        kinds = []
                        if (old["price"] != price or old["effective_price"] != effective) and price_novel:
                            kinds.append("PRICE_CHANGED")
                        if old["promo_type"] != promo or old["quantity"] != qty:
                            kinds.append("PROMOTION_CHANGED")
                        if any(old[k] != state[k] for k in ("product_name", "category", "description", "order_url")):
                            kinds.append("CONTENT_CHANGED")
                        for kind in kinds:
                            if not baseline:
                                events_batch.append((now, sid, pid, kind, json_dumps(old_state), json_dumps(state)))
                        counts["changed"] += 1
                    else:
                        counts["unchanged"] += 1

                    update_products_batch.append((*state.values(), now, ph, json_dumps(next_recent_prices), price_novel, reference_price, discount_amount, discount_pct, is_price_deal, sid, pid))
                    old.update(state)
                    old["last_seen"] = now
                    old["status"] = "active"
                    old["missing_streak"] = 0
                    old["state_hash"] = ph
                    old["recent_prices"] = next_recent_prices
                    old["price_novel_vs_previous_3"] = price_novel
                    old["reference_price"] = reference_price
                    old["discount_amount"] = discount_amount
                    old["discount_pct"] = discount_pct
                    old["is_price_deal"] = is_price_deal
                    state_cache.active_product_keys.add(key)

    missing_products_batch: list[tuple] = []
    missing_product_keys = list(state_cache.active_product_keys - seen_products)
    for key in missing_product_keys:
        old = state_cache.products[key]
        streak = old["missing_streak"] + 1
        status = "inactive" if streak >= missing_threshold else "active"
        old["missing_streak"] = streak
        old["status"] = status
        missing_products_batch.append((streak, status, key[0], key[1]))
        if status == "inactive":
            state_cache.active_product_keys.discard(key)
            counts["removed"] += 1
            if not baseline:
                events_batch.append((now, key[0], key[1], "REMOVED", json_dumps({"status": "active"}), json_dumps({"status": "inactive"})))

    missing_stores_batch: list[tuple] = []
    missing_store_keys = list(state_cache.active_store_keys - seen_stores)
    for sid in missing_store_keys:
        old = state_cache.stores[sid]
        streak = old["missing_streak"] + 1
        status = "inactive" if streak >= missing_threshold else "active"
        old["missing_streak"] = streak
        old["status"] = status
        missing_stores_batch.append((streak, status, sid))
        if status == "inactive":
            state_cache.active_store_keys.discard(sid)
            counts["store_removed"] += 1
            if not baseline:
                events_batch.append((now, sid, None, "STORE_REMOVED", json_dumps({"status": "active"}), json_dumps({"status": "inactive"})))

    # Execute batched queries
    if insert_stores_batch:
        conn.executemany(
            "INSERT INTO stores(store_uuid,name,address,city,locality,latitude,longitude,rating,review_count,order_url,first_seen,last_seen,status,missing_streak,state_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            insert_stores_batch,
        )
    if update_stores_batch:
        conn.executemany(
            "UPDATE stores SET name=?,address=?,city=?,locality=?,latitude=?,longitude=?,rating=?,review_count=?,order_url=?,last_seen=?,status=?,missing_streak=0,state_hash=? WHERE store_uuid=?",
            update_stores_batch,
        )
    if missing_stores_batch:
        conn.executemany("UPDATE stores SET missing_streak=?,status=? WHERE store_uuid=?", missing_stores_batch)

    if insert_products_batch:
        conn.executemany(
            "INSERT INTO products(store_uuid,product_uuid,product_name,category,description,price,quantity,promo_type,effective_price,order_url,first_seen,last_seen,status,missing_streak,state_hash,recent_prices,price_novel_vs_previous_3,reference_price,discount_amount,discount_pct,is_price_deal) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            insert_products_batch,
        )
    if update_products_batch:
        conn.executemany(
            "UPDATE products SET product_name=?,category=?,description=?,price=?,quantity=?,promo_type=?,effective_price=?,order_url=?,last_seen=?,status='active',missing_streak=0,state_hash=?,recent_prices=?,price_novel_vs_previous_3=?,reference_price=?,discount_amount=?,discount_pct=?,is_price_deal=? WHERE store_uuid=? AND product_uuid=?",
            update_products_batch,
        )
    if missing_products_batch:
        conn.executemany("UPDATE products SET missing_streak=?,status=? WHERE store_uuid=? AND product_uuid=?", missing_products_batch)

    if events_batch:
        conn.executemany(
            "INSERT INTO events(event_time,store_uuid,product_uuid,event_type,old_state,new_state) VALUES(?,?,?,?,?,?)",
            events_batch,
        )

    if event_retention_days is not None:
        cutoff = (datetime.fromisoformat(now) - timedelta(days=event_retention_days)).isoformat()
        conn.execute("DELETE FROM events WHERE event_time < ?", (cutoff,))
    if refresh_search:
        refresh_product_search(conn)
    conn.execute("INSERT OR REPLACE INTO crawl_batches VALUES(?,?,?,?,?,?)", (batch_id, now, counts["stores"], counts["products"], counts["fallback_stores"], counts["fallback_products"]))
    conn.execute("INSERT OR REPLACE INTO metadata VALUES('latest_batch',?)", (batch_id,))
    conn.commit()
    return counts


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source", required=True)
    p.add_argument("--database", required=True)
    p.add_argument("--batch-id", required=True)
    p.add_argument("--missing-threshold", type=int, default=DEFAULT_MISSING_THRESHOLD)
    p.add_argument("--baseline", action="store_true")
    p.add_argument("--event-retention-days", type=int, default=EVENT_RETENTION_DAYS,
                   help="0 keeps all derived events (default)")
    a = p.parse_args()
    conn = sqlite3.connect(a.database)
    conn.row_factory = sqlite3.Row
    retention = a.event_retention_days if a.event_retention_days > 0 else None
    counts = apply_snapshot(conn, iter_documents(a.source), a.batch_id, a.missing_threshold, True if a.baseline else None, retention)
    print(json.dumps(counts, ensure_ascii=False, indent=2))
    conn.close()


if __name__ == "__main__":
    main()
