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
from collections import OrderedDict
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
  state_hash TEXT NOT NULL, is_open INTEGER NOT NULL DEFAULT 1
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
  is_open INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(store_uuid, product_uuid),
  FOREIGN KEY(store_uuid) REFERENCES stores(store_uuid)
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, event_time TEXT NOT NULL, store_uuid TEXT NOT NULL,
  product_uuid TEXT, event_type TEXT NOT NULL, old_state TEXT, new_state TEXT
);
CREATE TABLE IF NOT EXISTS pending_store_changes(store_uuid TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS pending_product_changes(
  store_uuid TEXT NOT NULL, product_uuid TEXT NOT NULL,
  PRIMARY KEY(store_uuid, product_uuid)
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
    if "is_open" not in store_columns:
        conn.execute("ALTER TABLE stores ADD COLUMN is_open INTEGER NOT NULL DEFAULT 1")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stores_coordinates ON stores(latitude, longitude)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stores_is_open ON stores(is_open)")
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
        ("is_open", "INTEGER NOT NULL DEFAULT 1"),
    ):
        if name not in product_columns:
            conn.execute(f"ALTER TABLE products ADD COLUMN {name} {definition}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_deals ON products(is_price_deal, discount_pct DESC, discount_amount DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_is_open ON products(is_open)")


class ServingStateCache:
    """Bounded read-through cache for full rows.

    Active/seen identities intentionally live in SQLite, not Python sets: production
    databases contain millions of products and two identity sets can exhaust a
    GitHub-hosted runner before a snapshot starts applying.
    """

    def __init__(self, conn: sqlite3.Connection | None = None, max_rows: int = 20_000):
        self.conn = conn
        self.max_rows = max_rows
        self.stores: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.products: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        if conn is not None:
            ensure_schema_and_migrations(conn)
            self.load(conn)

    def load(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def _remember(self, cache: OrderedDict, key, value):
        cache[key] = value
        cache.move_to_end(key)
        if len(cache) > self.max_rows:
            cache.popitem(last=False)
        return value

    def get_store(self, sid: str) -> dict[str, Any] | None:
        cached = self.stores.get(sid)
        if cached is not None:
            self.stores.move_to_end(sid)
            return cached
        row = self.conn.execute("SELECT * FROM stores WHERE store_uuid=?", (sid,)).fetchone()
        return self._remember(self.stores, sid, dict(row)) if row else None

    def put_store(self, sid: str, value: dict[str, Any]) -> None:
        self._remember(self.stores, sid, value)

    def get_product(self, key: tuple[str, str]) -> dict[str, Any] | None:
        cached = self.products.get(key)
        if cached is not None:
            self.products.move_to_end(key)
            return cached
        row = self.conn.execute("SELECT * FROM products WHERE store_uuid=? AND product_uuid=?", key).fetchone()
        if not row:
            return None
        value = dict(row)
        try:
            value["recent_prices"] = [float(v) for v in json_loads(value.get("recent_prices") or "[]")]
        except Exception:
            value["recent_prices"] = []
        return self._remember(self.products, key, value)

    def put_product(self, key: tuple[str, str], value: dict[str, Any]) -> None:
        self._remember(self.products, key, value)


def apply_snapshot(conn: sqlite3.Connection, docs: Iterable[dict[str, Any]], batch_id: str,
                   missing_threshold: int = DEFAULT_MISSING_THRESHOLD, baseline: bool | None = None,
                   event_retention_days: int | None = None,
                   refresh_search: bool = True,
                   state_cache: ServingStateCache | None = None) -> dict[str, int]:
    ensure_schema_and_migrations(conn)
    now = datetime.strptime(batch_id, "%Y%m%d%H%M%S").replace(tzinfo=TW).isoformat()
    latest = conn.execute("SELECT value FROM metadata WHERE key='latest_batch'").fetchone()
    if latest and batch_id <= latest[0]:
        raise ValueError(f"snapshot batch {batch_id} is not newer than latest batch {latest[0]}")
    if baseline is None:
        baseline = conn.execute("SELECT COUNT(*)=0 FROM crawl_batches").fetchone()[0] == 1

    if state_cache is None:
        state_cache = ServingStateCache(conn)

    # TEMP tables are connection-local and backed by SQLite's temp_store policy.
    # They bound Python memory while retaining indexed missing-item lookups.
    conn.executescript("""
        CREATE TEMP TABLE IF NOT EXISTS snapshot_seen_stores(
            store_uuid TEXT PRIMARY KEY, is_open INTEGER NOT NULL
        ) WITHOUT ROWID;
        CREATE TEMP TABLE IF NOT EXISTS snapshot_seen_products(
            store_uuid TEXT NOT NULL, product_uuid TEXT NOT NULL,
            PRIMARY KEY(store_uuid, product_uuid)
        ) WITHOUT ROWID;
        DELETE FROM snapshot_seen_stores;
        DELETE FROM snapshot_seen_products;
    """)
    seen_stores: set[str] = set()
    counts = {k: 0 for k in ("stores", "products", "new", "changed", "reappeared", "removed", "unchanged", "store_new", "store_changed", "store_reappeared", "store_removed", "fallback_stores", "fallback_products")}

    insert_stores_batch: list[tuple] = []
    update_stores_batch: list[tuple] = []
    insert_products_batch: list[tuple] = []
    update_products_batch: list[tuple] = []
    events_batch: list[tuple] = []
    changed_store_keys: set[str] = set()
    seen_product_rows: list[tuple[str, str]] = []
    changed_product_rows: list[tuple[str, str]] = []

    def flush_snapshot_batches() -> None:
        if insert_stores_batch:
            conn.executemany("INSERT INTO stores(store_uuid,name,address,city,locality,latitude,longitude,rating,review_count,order_url,is_open,first_seen,last_seen,status,missing_streak,state_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", insert_stores_batch)
            insert_stores_batch.clear()
        if update_stores_batch:
            conn.executemany("UPDATE stores SET name=?,address=?,city=?,locality=?,latitude=?,longitude=?,rating=?,review_count=?,order_url=?,is_open=?,last_seen=?,status=?,missing_streak=0,state_hash=? WHERE store_uuid=?", update_stores_batch)
            update_stores_batch.clear()
        if insert_products_batch:
            conn.executemany("INSERT INTO products(store_uuid,product_uuid,product_name,category,description,price,quantity,promo_type,effective_price,order_url,is_open,first_seen,last_seen,status,missing_streak,state_hash,recent_prices,price_novel_vs_previous_3,reference_price,discount_amount,discount_pct,is_price_deal) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", insert_products_batch)
            insert_products_batch.clear()
        if update_products_batch:
            conn.executemany("UPDATE products SET product_name=?,category=?,description=?,price=?,quantity=?,promo_type=?,effective_price=?,order_url=?,is_open=?,last_seen=?,status='active',missing_streak=0,state_hash=?,recent_prices=?,price_novel_vs_previous_3=?,reference_price=?,discount_amount=?,discount_pct=?,is_price_deal=? WHERE store_uuid=? AND product_uuid=?", update_products_batch)
            update_products_batch.clear()
        if events_batch:
            conn.executemany("INSERT INTO events(event_time,store_uuid,product_uuid,event_type,old_state,new_state) VALUES(?,?,?,?,?,?)", events_batch)
            events_batch.clear()
        if seen_product_rows:
            conn.executemany("INSERT OR IGNORE INTO snapshot_seen_products VALUES(?,?)", seen_product_rows)
            seen_product_rows.clear()
        if changed_product_rows:
            conn.executemany("INSERT OR IGNORE INTO pending_product_changes VALUES(?,?)", changed_product_rows)
            changed_product_rows.clear()

    for doc in docs:
        sid, sfallback = store_identity(doc)
        counts["fallback_stores"] += int(sfallback)
        if sid in seen_stores:
            continue
        seen_stores.add(sid)
        counts["stores"] += 1
        if counts["stores"] > 1 and counts["stores"] % 100 == 1:
            flush_snapshot_batches()

        is_open_val = doc.get("isOpen")
        is_open = 1 if (is_open_val is True or is_open_val == 1 or is_open_val is None) else 0
        conn.execute("INSERT OR REPLACE INTO snapshot_seen_stores VALUES(?,?)", (sid, is_open))

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
            "is_open": is_open,
        }

        old_s = state_cache.get_store(sid)
        if old_s:
            for coordinate in ("latitude", "longitude"):
                if sstate[coordinate] is None:
                    sstate[coordinate] = old_s[coordinate]
        sh = canonical_hash(sstate)

        if old_s:
            store_needs_publish = old_s["status"] != "active" or old_s["state_hash"] != sh
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
            if store_needs_publish:
                changed_store_keys.add(sid)
        else:
            insert_stores_batch.append((sid, *sstate.values(), now, now, "active", 0, sh))
            counts["store_new"] += 1
            if not baseline:
                events_batch.append((now, sid, None, "STORE_NEW", None, json_dumps(sstate)))
            state_cache.put_store(sid, {
                "store_uuid": sid, **sstate, "first_seen": now, "last_seen": now,
                "status": "active", "missing_streak": 0, "state_hash": sh
            })
            changed_store_keys.add(sid)

        sections = (doc.get("hasMenu") or {}).get("hasMenuSection") or []
        store_product_keys: set[str] = set()
        for sec in sections:
            category = html.unescape(str(sec.get("name") or "一般"))
            for item in sec.get("hasMenuItem") or []:
                name = html.unescape(str(item.get("name") or "")).strip()
                if not name:
                    continue
                pid, pfallback = product_identity(item, sid)
                counts["fallback_products"] += int(pfallback)
                key = (sid, pid)
                if pid in store_product_keys:
                    continue
                store_product_keys.add(pid)
                seen_product_rows.append(key)
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
                    "is_open": is_open,
                }
                ph = canonical_hash(state)
                old = state_cache.get_product(key)

                if old is None:
                    init_recent = [price] if (is_open == 1 and price > 0) else []
                    insert_products_batch.append((sid, pid, *state.values(), now, now, "active", 0, ph, json_dumps(init_recent), 0, None, 0, 0, 0))
                    counts["new"] += 1
                    if not baseline:
                        events_batch.append((now, sid, pid, "NEW", None, json_dumps(state)))
                    state_cache.put_product(key, {
                        "store_uuid": sid, "product_uuid": pid, **state,
                        "first_seen": now, "last_seen": now, "status": "active",
                        "missing_streak": 0, "state_hash": ph, "recent_prices": init_recent,
                        "price_novel_vs_previous_3": 0, "reference_price": None,
                        "discount_amount": 0, "discount_pct": 0, "is_price_deal": 0,
                    })
                    changed_product_rows.append(key)
                else:
                    old_state = dict(old)
                    old_state["recent_prices"] = json_dumps(old["recent_prices"])
                    if is_open == 1 and price > 0:
                        previous_prices = old["recent_prices"][-3:]
                        price_novel = int(len(previous_prices) == 3 and price not in previous_prices)
                        reference_price = float(statistics.median(previous_prices)) if len(previous_prices) == 3 else None
                        discount_amount = round(reference_price - price, 2) if price_novel and reference_price and price < reference_price else 0
                        discount_pct = round(discount_amount / reference_price * 100, 2) if discount_amount and reference_price else 0
                        is_price_deal = int(discount_amount > 0)
                        next_recent_prices = (previous_prices + [price])[-3:]
                    else:
                        price_novel = 0
                        reference_price = old.get("reference_price")
                        discount_amount = 0
                        discount_pct = 0
                        is_price_deal = 0
                        next_recent_prices = old["recent_prices"]

                    product_needs_publish = (
                        old["status"] != "active" or old["state_hash"] != ph
                        or old["recent_prices"] != next_recent_prices
                        or old.get("price_novel_vs_previous_3") != price_novel
                        or old.get("reference_price") != reference_price
                        or old.get("discount_amount") != discount_amount
                        or old.get("discount_pct") != discount_pct
                        or old.get("is_price_deal") != is_price_deal
                    )

                    if old["status"] != "active":
                        counts["reappeared"] += 1
                        if not baseline:
                            events_batch.append((now, sid, pid, "REAPPEARED", json_dumps({"status": old["status"]}), json_dumps({"status": "active"})))
                    if old["state_hash"] != ph:
                        kinds = []
                        if is_open == 1 and price > 0 and (old["price"] != price or old["effective_price"] != effective) and price_novel:
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
                    if product_needs_publish:
                        changed_product_rows.append(key)

    flush_snapshot_batches()

    missing_products_batch: list[tuple] = []
    missing_products = conn.execute("""
        SELECT p.store_uuid,p.product_uuid,p.missing_streak
        FROM products p
        LEFT JOIN snapshot_seen_stores ss ON ss.store_uuid=p.store_uuid
        LEFT JOIN stores s ON s.store_uuid=p.store_uuid
        WHERE p.status='active'
          AND NOT EXISTS (
              SELECT 1 FROM snapshot_seen_products sp
              WHERE sp.store_uuid=p.store_uuid AND sp.product_uuid=p.product_uuid
          )
          AND COALESCE(ss.is_open,s.is_open,1)=1
    """)
    for store_id, product_id, old_streak in missing_products:
        key = (store_id, product_id)
        streak = old_streak + 1
        status = "inactive" if streak >= missing_threshold else "active"
        missing_products_batch.append((streak, status, store_id, product_id))
        changed_product_rows.append(key)
        if status == "inactive":
            counts["removed"] += 1
            if not baseline:
                events_batch.append((now, store_id, product_id, "REMOVED", json_dumps({"status": "active"}), json_dumps({"status": "inactive"})))
        if len(missing_products_batch) >= 10_000:
            conn.executemany("UPDATE products SET missing_streak=?,status=? WHERE store_uuid=? AND product_uuid=?", missing_products_batch)
            missing_products_batch.clear()
            flush_snapshot_batches()

    missing_stores_batch: list[tuple] = []
    missing_store_keys = conn.execute("""
        SELECT s.store_uuid FROM stores s
        WHERE s.status='active'
          AND NOT EXISTS (SELECT 1 FROM snapshot_seen_stores ss WHERE ss.store_uuid=s.store_uuid)
    """)
    for (sid,) in missing_store_keys:
        old = state_cache.get_store(sid)
        if old is None:
            continue
        streak = old["missing_streak"] + 1
        status = "inactive" if streak >= missing_threshold else "active"
        old["missing_streak"] = streak
        old["status"] = status
        missing_stores_batch.append((streak, status, sid))
        changed_store_keys.add(sid)
        if status == "inactive":
            counts["store_removed"] += 1
            if not baseline:
                events_batch.append((now, sid, None, "STORE_REMOVED", json_dumps({"status": "active"}), json_dumps({"status": "inactive"})))

    # Execute batched queries
    if insert_stores_batch:
        conn.executemany(
            "INSERT INTO stores(store_uuid,name,address,city,locality,latitude,longitude,rating,review_count,order_url,is_open,first_seen,last_seen,status,missing_streak,state_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            insert_stores_batch,
        )
    if update_stores_batch:
        conn.executemany(
            "UPDATE stores SET name=?,address=?,city=?,locality=?,latitude=?,longitude=?,rating=?,review_count=?,order_url=?,is_open=?,last_seen=?,status=?,missing_streak=0,state_hash=? WHERE store_uuid=?",
            update_stores_batch,
        )
    if missing_stores_batch:
        conn.executemany("UPDATE stores SET missing_streak=?,status=? WHERE store_uuid=?", missing_stores_batch)

    if insert_products_batch:
        conn.executemany(
            "INSERT INTO products(store_uuid,product_uuid,product_name,category,description,price,quantity,promo_type,effective_price,order_url,is_open,first_seen,last_seen,status,missing_streak,state_hash,recent_prices,price_novel_vs_previous_3,reference_price,discount_amount,discount_pct,is_price_deal) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            insert_products_batch,
        )
    if update_products_batch:
        conn.executemany(
            "UPDATE products SET product_name=?,category=?,description=?,price=?,quantity=?,promo_type=?,effective_price=?,order_url=?,is_open=?,last_seen=?,status='active',missing_streak=0,state_hash=?,recent_prices=?,price_novel_vs_previous_3=?,reference_price=?,discount_amount=?,discount_pct=?,is_price_deal=? WHERE store_uuid=? AND product_uuid=?",
            update_products_batch,
        )
    if missing_products_batch:
        conn.executemany("UPDATE products SET missing_streak=?,status=? WHERE store_uuid=? AND product_uuid=?", missing_products_batch)

    if events_batch:
        conn.executemany(
            "INSERT INTO events(event_time,store_uuid,product_uuid,event_type,old_state,new_state) VALUES(?,?,?,?,?,?)",
            events_batch,
        )

    if changed_store_keys:
        conn.executemany("INSERT OR IGNORE INTO pending_store_changes(store_uuid) VALUES(?)", ((sid,) for sid in changed_store_keys))
    if changed_product_rows:
        conn.executemany("INSERT OR IGNORE INTO pending_product_changes VALUES(?,?)", changed_product_rows)

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
