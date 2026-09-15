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
  rating REAL, review_count INTEGER, order_url TEXT, first_seen TEXT NOT NULL,
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


def iter_documents(source: str) -> Iterable[dict[str, Any]]:
    p = Path(source)
    if p.is_dir():
        for f in p.rglob("*.json"):
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(doc, dict) and (doc.get("store_uuid") or doc.get("hasMenu")):
                    yield doc
            except (OSError, json.JSONDecodeError):
                continue
    elif tarfile.is_tarfile(p):
        with tarfile.open(p, "r:*") as tf:
            for member in tf:
                if member.isfile() and member.name.endswith(".json"):
                    fh = tf.extractfile(member)
                    if fh:
                        try:
                            doc = json.load(fh)
                            if isinstance(doc, dict) and (doc.get("store_uuid") or doc.get("hasMenu")):
                                yield doc
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
    else:
        raise ValueError(f"unsupported raw source: {source}")


def _num(value: Any, typ=float, default=0):
    try:
        return typ(value)
    except (TypeError, ValueError):
        return default


def _event(conn: sqlite3.Connection, now: str, sid: str, pid: str | None, kind: str, old, new):
    conn.execute("INSERT INTO events(event_time,store_uuid,product_uuid,event_type,old_state,new_state) VALUES(?,?,?,?,?,?)",
                 (now, sid, pid, kind, json.dumps(old, ensure_ascii=False) if old is not None else None,
                  json.dumps(new, ensure_ascii=False) if new is not None else None))


def refresh_product_search(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM product_search")
    conn.execute("INSERT INTO product_search SELECT p.store_uuid,p.product_uuid,s.name,p.product_name,p.category FROM products p JOIN stores s USING(store_uuid) WHERE p.status='active'")


def apply_snapshot(conn: sqlite3.Connection, docs: Iterable[dict[str, Any]], batch_id: str,
                   missing_threshold: int = DEFAULT_MISSING_THRESHOLD, baseline: bool | None = None,
                   event_retention_days: int | None = None,
                   refresh_search: bool = True) -> dict[str, int]:
    conn.executescript(SCHEMA)
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
    if baseline is None:
        baseline = conn.execute("SELECT COUNT(*)=0 FROM crawl_batches").fetchone()[0] == 1
    seen_stores: set[str] = set(); seen_products: set[tuple[str, str]] = set()
    counts = {k: 0 for k in ("stores", "products", "new", "changed", "reappeared", "removed", "unchanged", "store_new", "store_changed", "store_reappeared", "store_removed", "fallback_stores", "fallback_products")}

    for doc in docs:
        sid, sfallback = store_identity(doc); counts["fallback_stores"] += int(sfallback)
        if sid in seen_stores:
            continue
        seen_stores.add(sid); counts["stores"] += 1
        address = doc.get("address") or {}; rating = doc.get("aggregateRating") or {}
        order_url = str(doc.get("potentialAction", {}).get("target", {}).get("urlTemplate") or doc.get("@id") or "")
        sstate = {"name": str(doc.get("name") or ""), "address": str(address.get("streetAddress") or ""),
                  "city": str(address.get("addressRegion") or address.get("addressLocality") or ""),
                  "locality": str(address.get("addressLocality") or ""), "rating": _num(rating.get("ratingValue"), float, None),
                  "review_count": _num(rating.get("reviewCount"), int, None), "order_url": order_url}
        sh = canonical_hash(sstate)
        old_s = conn.execute("SELECT * FROM stores WHERE store_uuid=?", (sid,)).fetchone()
        if old_s:
            if old_s["status"] != "active":
                counts["store_reappeared"] += 1
                if not baseline: _event(conn, now, sid, None, "STORE_REAPPEARED", {"status": old_s["status"]}, {"status": "active"})
            if old_s["state_hash"] != sh:
                counts["store_changed"] += 1
                if not baseline: _event(conn, now, sid, None, "STORE_CHANGED", dict(old_s), sstate)
            status = "active"
            conn.execute("UPDATE stores SET name=?,address=?,city=?,locality=?,rating=?,review_count=?,order_url=?,last_seen=?,status=?,missing_streak=0,state_hash=? WHERE store_uuid=?",
                         (*sstate.values(), now, status, sh, sid))
        else:
            conn.execute("INSERT INTO stores VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (sid, *sstate.values(), now, now, "active", 0, sh))
            counts["store_new"] += 1
            if not baseline: _event(conn, now, sid, None, "STORE_NEW", None, sstate)

        sections = (doc.get("hasMenu") or {}).get("hasMenuSection") or []
        for sec in sections:
            category = html.unescape(str(sec.get("name") or "一般"))
            for item in sec.get("hasMenuItem") or []:
                name = html.unescape(str(item.get("name") or "")).strip()
                if not name: continue
                pid, pfallback = product_identity(item, sid); counts["fallback_products"] += int(pfallback)
                key = (sid, pid)
                if key in seen_products: continue
                seen_products.add(key); counts["products"] += 1
                price = _num((item.get("offers") or {}).get("price"), float, 0)
                desc = str(item.get("description") or "")
                promo = str(item.get("promo_type") or "無"); qty = max(1, _num(item.get("quantity"), int, 1))
                effective = _num(item.get("effective_price"), float, round(price / qty, 2))
                state = {"product_name": name, "category": category, "description": desc, "price": price,
                         "quantity": qty, "promo_type": promo, "effective_price": effective, "order_url": order_url}
                ph = canonical_hash(state)
                old = conn.execute("SELECT * FROM products WHERE store_uuid=? AND product_uuid=?", key).fetchone()
                if old is None:
                    conn.execute("INSERT INTO products(store_uuid,product_uuid,product_name,category,description,price,quantity,promo_type,effective_price,order_url,first_seen,last_seen,status,missing_streak,state_hash,recent_prices,price_novel_vs_previous_3,reference_price,discount_amount,discount_pct,is_price_deal) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (sid, pid, *state.values(), now, now, "active", 0, ph, json.dumps([price]), 0, None, 0, 0, 0))
                    counts["new"] += 1
                    if not baseline: _event(conn, now, sid, pid, "NEW", None, state)
                else:
                    old_state = dict(old)
                    try:
                        previous_prices = [float(v) for v in json.loads(old["recent_prices"] or "[]")][-3:]
                    except (TypeError, ValueError, json.JSONDecodeError):
                        previous_prices = []
                    price_novel = int(len(previous_prices) == 3 and price not in previous_prices)
                    reference_price = float(statistics.median(previous_prices)) if len(previous_prices) == 3 else None
                    discount_amount = round(reference_price - price, 2) if price_novel and reference_price and price < reference_price else 0
                    discount_pct = round(discount_amount / reference_price * 100, 2) if discount_amount and reference_price else 0
                    is_price_deal = int(discount_amount > 0)
                    next_recent_prices = (previous_prices + [price])[-3:]
                    if old["status"] != "active":
                        counts["reappeared"] += 1
                        if not baseline: _event(conn, now, sid, pid, "REAPPEARED", {"status": old["status"]}, {"status": "active"})
                    if old["state_hash"] != ph:
                        kinds = []
                        # A price is publishable only when it did not occur in
                        # any of the preceding three snapshots. This suppresses
                        # recurring closed-store values such as 100,1,100 -> 1.
                        if (old["price"] != price or old["effective_price"] != effective) and price_novel:
                            kinds.append("PRICE_CHANGED")
                        if old["promo_type"] != promo or old["quantity"] != qty: kinds.append("PROMOTION_CHANGED")
                        if any(old[k] != state[k] for k in ("product_name", "category", "description", "order_url")): kinds.append("CONTENT_CHANGED")
                        for kind in kinds:
                            if not baseline: _event(conn, now, sid, pid, kind, old_state, state)
                        counts["changed"] += 1
                    else: counts["unchanged"] += 1
                    conn.execute("UPDATE products SET product_name=?,category=?,description=?,price=?,quantity=?,promo_type=?,effective_price=?,order_url=?,last_seen=?,status='active',missing_streak=0,state_hash=?,recent_prices=?,price_novel_vs_previous_3=?,reference_price=?,discount_amount=?,discount_pct=?,is_price_deal=? WHERE store_uuid=? AND product_uuid=?",
                                 (*state.values(), now, ph, json.dumps(next_recent_prices), price_novel, reference_price, discount_amount, discount_pct, is_price_deal, sid, pid))

    for old in conn.execute("SELECT * FROM products WHERE status='active'").fetchall():
        key = (old["store_uuid"], old["product_uuid"])
        if key not in seen_products:
            streak = old["missing_streak"] + 1
            status = "inactive" if streak >= missing_threshold else "active"
            conn.execute("UPDATE products SET missing_streak=?,status=? WHERE store_uuid=? AND product_uuid=?", (streak, status, *key))
            if status == "inactive":
                counts["removed"] += 1
                if not baseline: _event(conn, now, *key, "REMOVED", {"status": "active"}, {"status": "inactive"})
    for old in conn.execute("SELECT store_uuid,missing_streak FROM stores WHERE status='active'").fetchall():
        if old["store_uuid"] not in seen_stores:
            streak = old["missing_streak"] + 1
            status = "inactive" if streak >= missing_threshold else "active"
            conn.execute("UPDATE stores SET missing_streak=?,status=? WHERE store_uuid=?", (streak, status, old["store_uuid"]))
            if status == "inactive":
                counts["store_removed"] += 1
                if not baseline: _event(conn, now, old["store_uuid"], None, "STORE_REMOVED", {"status": "active"}, {"status": "inactive"})

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
    p.add_argument("--source", required=True); p.add_argument("--database", required=True); p.add_argument("--batch-id", required=True)
    p.add_argument("--missing-threshold", type=int, default=DEFAULT_MISSING_THRESHOLD); p.add_argument("--baseline", action="store_true")
    p.add_argument("--event-retention-days", type=int, default=EVENT_RETENTION_DAYS,
                   help="0 keeps all derived events (default)")
    a = p.parse_args(); conn = sqlite3.connect(a.database); conn.row_factory = sqlite3.Row
    retention = a.event_retention_days if a.event_retention_days > 0 else None
    counts = apply_snapshot(conn, iter_documents(a.source), a.batch_id, a.missing_threshold, True if a.baseline else None, retention)
    print(json.dumps(counts, ensure_ascii=False, indent=2)); conn.close()


if __name__ == "__main__": main()
