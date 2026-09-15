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
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

TW = timezone(timedelta(hours=8))
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
DEFAULT_MISSING_THRESHOLD = int(os.getenv("MISSING_STREAK_THRESHOLD", "3"))
EVENT_RETENTION_DAYS = int(os.getenv("EVENT_RETENTION_DAYS", "60"))


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
  state_hash TEXT NOT NULL, PRIMARY KEY(store_uuid, product_uuid),
  FOREIGN KEY(store_uuid) REFERENCES stores(store_uuid)
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, event_time TEXT NOT NULL, store_uuid TEXT NOT NULL,
  product_uuid TEXT, event_type TEXT NOT NULL, old_state TEXT, new_state TEXT
);
CREATE INDEX IF NOT EXISTS idx_stores_first_seen ON stores(first_seen);
CREATE INDEX IF NOT EXISTS idx_stores_city ON stores(city);
CREATE INDEX IF NOT EXISTS idx_products_first_seen ON products(first_seen);
CREATE INDEX IF NOT EXISTS idx_products_price ON products(effective_price, price);
CREATE INDEX IF NOT EXISTS idx_products_promo ON products(promo_type);
CREATE INDEX IF NOT EXISTS idx_events_product_time ON events(store_uuid, product_uuid, event_time DESC);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(event_time);
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
                yield json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
    elif tarfile.is_tarfile(p):
        with tarfile.open(p, "r:*") as tf:
            for member in tf:
                if member.isfile() and member.name.endswith(".json"):
                    fh = tf.extractfile(member)
                    if fh:
                        try:
                            yield json.load(fh)
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


def apply_snapshot(conn: sqlite3.Connection, docs: Iterable[dict[str, Any]], batch_id: str,
                   missing_threshold: int = DEFAULT_MISSING_THRESHOLD, baseline: bool | None = None) -> dict[str, int]:
    conn.executescript(SCHEMA)
    now = datetime.strptime(batch_id, "%Y%m%d%H%M%S").replace(tzinfo=TW).isoformat()
    if baseline is None:
        baseline = conn.execute("SELECT COUNT(*)=0 FROM crawl_batches").fetchone()[0] == 1
    seen_stores: set[str] = set(); seen_products: set[tuple[str, str]] = set()
    counts = {k: 0 for k in ("stores", "products", "new", "changed", "reappeared", "removed", "unchanged", "fallback_stores", "fallback_products")}

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
            status = "active"
            conn.execute("UPDATE stores SET name=?,address=?,city=?,locality=?,rating=?,review_count=?,order_url=?,last_seen=?,status=?,missing_streak=0,state_hash=? WHERE store_uuid=?",
                         (*sstate.values(), now, status, sh, sid))
        else:
            conn.execute("INSERT INTO stores VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (sid, *sstate.values(), now, now, "active", 0, sh))

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
                    conn.execute("INSERT INTO products VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (sid, pid, *state.values(), now, now, "active", 0, ph))
                    counts["new"] += 1
                    if not baseline: _event(conn, now, sid, pid, "NEW", None, state)
                else:
                    old_state = dict(old)
                    if old["status"] != "active":
                        counts["reappeared"] += 1
                        if not baseline: _event(conn, now, sid, pid, "REAPPEARED", {"status": old["status"]}, {"status": "active"})
                    if old["state_hash"] != ph:
                        kinds = []
                        if old["price"] != price or old["effective_price"] != effective: kinds.append("PRICE_CHANGED")
                        if old["promo_type"] != promo or old["quantity"] != qty: kinds.append("PROMOTION_CHANGED")
                        if any(old[k] != state[k] for k in ("product_name", "category", "description", "order_url")): kinds.append("CONTENT_CHANGED")
                        for kind in kinds:
                            if not baseline: _event(conn, now, sid, pid, kind, old_state, state)
                        counts["changed"] += 1
                    else: counts["unchanged"] += 1
                    conn.execute("UPDATE products SET product_name=?,category=?,description=?,price=?,quantity=?,promo_type=?,effective_price=?,order_url=?,last_seen=?,status='active',missing_streak=0,state_hash=? WHERE store_uuid=? AND product_uuid=?",
                                 (*state.values(), now, ph, sid, pid))

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
            conn.execute("UPDATE stores SET missing_streak=?,status=? WHERE store_uuid=?", (streak, "inactive" if streak >= missing_threshold else "active", old["store_uuid"]))

    cutoff = (datetime.fromisoformat(now) - timedelta(days=EVENT_RETENTION_DAYS)).isoformat()
    conn.execute("DELETE FROM events WHERE event_time < ?", (cutoff,))
    conn.execute("DELETE FROM product_search")
    conn.execute("INSERT INTO product_search SELECT p.store_uuid,p.product_uuid,s.name,p.product_name,p.category FROM products p JOIN stores s USING(store_uuid) WHERE p.status='active'")
    conn.execute("INSERT OR REPLACE INTO crawl_batches VALUES(?,?,?,?,?,?)", (batch_id, now, counts["stores"], counts["products"], counts["fallback_stores"], counts["fallback_products"]))
    conn.execute("INSERT OR REPLACE INTO metadata VALUES('latest_batch',?)", (batch_id,))
    conn.commit()
    return counts


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source", required=True); p.add_argument("--database", required=True); p.add_argument("--batch-id", required=True)
    p.add_argument("--missing-threshold", type=int, default=DEFAULT_MISSING_THRESHOLD); p.add_argument("--baseline", action="store_true")
    a = p.parse_args(); conn = sqlite3.connect(a.database); conn.row_factory = sqlite3.Row
    counts = apply_snapshot(conn, iter_documents(a.source), a.batch_id, a.missing_threshold, True if a.baseline else None)
    print(json.dumps(counts, ensure_ascii=False, indent=2)); conn.close()


if __name__ == "__main__": main()
