"""Shared identity and snapshot checks. Never use filename hash prefixes as keys."""
import hashlib
import html
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def canonical_url(value):
    if not isinstance(value, str):
        raise ValueError("missing store URL")
    parts = urlsplit(html.unescape(value.strip()))
    if parts.scheme != "https" or parts.hostname not in {"www.ubereats.com", "ubereats.com"} or "/store/" not in parts.path:
        raise ValueError("invalid Uber Eats store URL")
    return urlunsplit(("https", "www.ubereats.com", parts.path.rstrip("/"), "", ""))


def validate_document(doc, batch_id=None, filename=None):
    if not isinstance(doc, dict) or not isinstance(doc.get("name"), str) or not doc["name"].strip():
        raise ValueError("missing store name")
    key = canonical_url(doc.get("@id"))
    if batch_id:
        if doc.get("batch_id", batch_id) != batch_id:
            raise ValueError("wrong document batch")
        if filename and not Path(filename).name.startswith(batch_id + "_"):
            raise ValueError("wrong filename batch")
    menu = doc.get("hasMenu")
    if not isinstance(menu, dict) or not isinstance(menu.get("hasMenuSection"), list):
        raise ValueError("missing menu structure")
    sections = menu["hasMenuSection"]
    for section in sections:
        if not isinstance(section, dict) or not isinstance(section.get("hasMenuItem"), list):
            raise ValueError("invalid menu section")
        for item in section["hasMenuItem"]:
            if not isinstance(item, dict) or not item.get("name") or not isinstance(item.get("offers"), dict):
                raise ValueError("invalid menu item")
    if not any(section["hasMenuItem"] for section in sections):
        # An empty, explicitly closed store is legitimate; an incomplete response is not.
        if doc.get("isOpen") is not False and doc.get("menu_status") != "empty_confirmed":
            raise ValueError("empty menu without explicit closed status")
    return key


def validate_snapshot(src_dir, stores, batch_id):
    expected = [canonical_url(store.get("store_url")) for store in stores]
    if not expected or len(expected) != len(set(expected)):
        raise ValueError("empty or duplicate assigned store identities")
    found = {}
    for path in sorted(Path(src_dir).rglob("*.json")):
        with path.open(encoding="utf-8") as handle:
            doc = json.load(handle)
        key = validate_document(doc, batch_id, path)
        if key in found:
            raise ValueError(f"duplicate store: {key}: {found[key]} and {path}")
        found[key] = str(path)
    missing = set(expected) - found.keys()
    extra = found.keys() - set(expected)
    if missing or extra:
        raise ValueError(f"store set mismatch: missing={len(missing)}, unexpected={len(extra)}; examples={sorted(missing | extra)[:3]}")
    return list(found.values())


def archive_member(path, batch_id):
    with open(path, encoding="utf-8") as handle:
        key = canonical_url(json.load(handle)["@id"])
    return f"Json/{batch_id}_{hashlib.sha256(key.encode()).hexdigest()}.json"
