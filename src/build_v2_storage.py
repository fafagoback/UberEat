# -*- coding: utf-8 -*-
"""Build UberEat v2 storage on Hugging Face.

Design goals:
- keep crawler coverage unchanged;
- store only changed records in history;
- keep one current sharded serving view;
- build a real full-catalog substring index (CJK/Latin n-grams);
- upload only shards whose canonical content changed.

The browser never scans the full Parquet catalog. Parquet is used only as the
Stage-6 ETL input and compact change-event history format.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

TW_TZ = timezone(timedelta(hours=8))
BUCKETS = 256


def norm_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def doc_id(store_id: Any, product_id: Any) -> str:
    raw = f"{store_id}\x1f{product_id}".encode("utf-8")
    return hashlib.blake2b(raw, digest_size=8).hexdigest()


def bucket_for_id(did: str) -> str:
    return did[:2]


def bucket_for_token(token: str) -> str:
    return hashlib.sha1(token.encode("utf-8")).hexdigest()[:2]


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_gzip_json(path: Path, value: Any) -> Tuple[str, int]:
    payload = canonical_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as gz:
            gz.write(payload)
    return sha256_bytes(payload), path.stat().st_size


def read_gzip_json(path: str | Path) -> Any:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def normalize_record(row: Dict[str, Any]) -> Dict[str, Any]:
    store_id = str(row.get("store_id") or "")
    product_id = str(row.get("product_id") or "")
    did = doc_id(store_id, product_id)
    record = {
        "id": did,
        "product_id": product_id,
        "store_id": store_id,
        "store_name": str(row.get("store_name") or ""),
        "product_name": str(row.get("product_name") or ""),
        "category_name": str(row.get("category_name") or "一般"),
        "description": str(row.get("description") or ""),
        "price": float(row.get("price") or 0),
        "quantity": int(row.get("quantity") or 1),
        "promo_type": str(row.get("promo_type") or "無"),
        "eff_price": float(row.get("eff_price") or row.get("price") or 0),
        "order_action_url": str(row.get("order_action_url") or ""),
        "rating_value": float(row["rating_value"]) if row.get("rating_value") is not None else None,
        "review_count": int(row["review_count"]) if row.get("review_count") is not None else None,
        "locality": str(row.get("locality") or ""),
        "street_address": str(row.get("street_address") or ""),
        "city": str(row.get("city") or ""),
        "is_open": int(row.get("is_open") if row.get("is_open") is not None else 1),
    }
    record["search_text"] = norm_text(" ".join([
        record["store_name"], record["product_name"], record["category_name"],
        record["description"], record["city"], record["locality"], record["street_address"]
    ]))
    hash_payload = {k: v for k, v in record.items() if k not in {"id", "search_text"}}
    record["_h"] = sha256_bytes(canonical_json_bytes(hash_payload))
    return record


def grams(text: str) -> Iterable[str]:
    """Generate exact-substring candidate grams. Final results are verified."""
    seen = set()
    for run in re.findall(r"[0-9a-z\u3400-\u9fff]+", norm_text(text)):
        length = len(run)
        max_n = min(3, length)
        for n in range(1, max_n + 1):
            for i in range(0, length - n + 1):
                token = run[i:i+n]
                if token not in seen:
                    seen.add(token)
                    yield token


def load_parquet(path: str) -> List[Dict[str, Any]]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise SystemExit("pyarrow is required") from exc
    table = pq.read_table(path)
    return table.to_pylist()


def download_optional(repo_id: str, filename: str, token: str | None) -> str | None:
    try:
        from huggingface_hub import hf_hub_download
        return hf_hub_download(repo_id=repo_id, filename=filename, repo_type="dataset", token=token)
    except Exception:
        return None


def old_manifest(repo_id: str, token: str | None, kind: str) -> Dict[str, Any]:
    path = download_optional(repo_id, f"v2/{kind}/manifest.json", token)
    if not path:
        return {}
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}


def write_history_parquet(path: Path, events: List[Dict[str, Any]]) -> None:
    if not events:
        return
    import pyarrow as pa
    import pyarrow.parquet as pq
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(events)
    pq.write_table(table, path, compression="zstd")


def remote_record_shard(repo_id: str, token: str | None, bucket: str) -> Dict[str, Dict[str, Any]]:
    path = download_optional(repo_id, f"v2/current/shards/{bucket}.json.gz", token)
    if not path:
        return {}
    try:
        rows = read_gzip_json(path)
        return {str(r["id"]): r for r in rows}
    except Exception:
        return {}


def build(args: argparse.Namespace) -> None:
    token = os.environ.get("HF_TOKEN")
    if not token and not args.offline:
        raise SystemExit("HF_TOKEN is required unless --offline is used")

    batch_id = args.batch_id or datetime.now(TW_TZ).strftime("%Y%m%d%H%M%S")
    root = Path(args.output_dir or tempfile.mkdtemp(prefix="ubereat-v2-"))
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    print(f"[v2] loading full current catalog: {args.input_parquet}")
    raw_rows = load_parquet(args.input_parquet)
    records: List[Dict[str, Any]] = []
    for row in raw_rows:
        try:
            rec = normalize_record(row)
            if rec["store_id"] and rec["product_id"] and rec["product_name"] and rec["price"] > 0:
                records.append(rec)
        except Exception as exc:
            print(f"[v2] skip malformed row: {exc}")

    # Deterministic order makes unchanged shard hashes stable.
    records.sort(key=lambda r: r["id"])
    current_buckets: Dict[str, List[Dict[str, Any]]] = {f"{i:02x}": [] for i in range(BUCKETS)}
    for rec in records:
        current_buckets[bucket_for_id(rec["id"])].append(rec)

    prev_current_manifest = old_manifest(args.repo_id, token, "current") if not args.offline else {}
    prev_current_hashes = prev_current_manifest.get("shards", {})

    current_manifest = {
        "version": 2,
        "batch_id": batch_id,
        "generated_at": datetime.now(TW_TZ).isoformat(),
        "total_documents": len(records),
        "shards": {},
    }
    changed_current_buckets: List[str] = []
    history_events: List[Dict[str, Any]] = []
    added = updated = removed = 0

    current_dir = root / "current" / "shards"
    for bucket, rows in current_buckets.items():
        path = current_dir / f"{bucket}.json.gz"
        canonical_hash, size_bytes = write_gzip_json(path, rows)
        current_manifest["shards"][bucket] = {
            "sha256": canonical_hash,
            "rows": len(rows),
            "bytes": size_bytes,
            "path": f"v2/current/shards/{bucket}.json.gz",
        }
        old_hash = (prev_current_hashes.get(bucket) or {}).get("sha256")
        if canonical_hash == old_hash:
            continue
        changed_current_buckets.append(bucket)
        old_rows = remote_record_shard(args.repo_id, token, bucket) if old_hash and not args.offline else {}
        new_rows = {r["id"]: r for r in rows}
        for did, rec in new_rows.items():
            old = old_rows.get(did)
            if old is None:
                change_type = "added"
                added += 1
            elif old.get("_h") != rec.get("_h"):
                change_type = "updated"
                updated += 1
            else:
                continue
            history_events.append({
                "batch_id": batch_id,
                "changed_at": datetime.now(TW_TZ).isoformat(),
                "change_type": change_type,
                **{k: v for k, v in rec.items() if k not in {"search_text"}},
                "previous_hash": old.get("_h") if old else None,
            })
        for did, old in old_rows.items():
            if did not in new_rows:
                removed += 1
                history_events.append({
                    "batch_id": batch_id,
                    "changed_at": datetime.now(TW_TZ).isoformat(),
                    "change_type": "removed",
                    **{k: v for k, v in old.items() if k not in {"search_text"}},
                    "previous_hash": old.get("_h"),
                })

    unchanged = len(records) - added - updated
    if not prev_current_manifest:
        # First migration is a baseline, not a meaningful "all products changed" day.
        added = len(records)
        unchanged = 0

    # Search postings. Every candidate is verified against record.search_text by the browser.
    postings_by_bucket: Dict[str, Dict[str, List[str]]] = {f"{i:02x}": defaultdict(list) for i in range(BUCKETS)}
    for rec in records:
        did = rec["id"]
        for token_value in grams(rec["search_text"]):
            postings_by_bucket[bucket_for_token(token_value)][token_value].append(did)

    prev_search_manifest = old_manifest(args.repo_id, token, "search") if not args.offline else {}
    prev_search_hashes = prev_search_manifest.get("shards", {})
    search_manifest = {
        "version": 2,
        "batch_id": batch_id,
        "generated_at": datetime.now(TW_TZ).isoformat(),
        "algorithm": "nfkc-lower-exact-verify-ngram-1-3",
        "total_documents": len(records),
        "shards": {},
    }
    changed_search_buckets: List[str] = []
    search_dir = root / "search" / "shards"
    for bucket in [f"{i:02x}" for i in range(BUCKETS)]:
        data = {token_value: sorted(ids) for token_value, ids in sorted(postings_by_bucket[bucket].items())}
        path = search_dir / f"{bucket}.json.gz"
        canonical_hash, size_bytes = write_gzip_json(path, data)
        search_manifest["shards"][bucket] = {
            "sha256": canonical_hash,
            "tokens": len(data),
            "bytes": size_bytes,
            "path": f"v2/search/shards/{bucket}.json.gz",
        }
        old_hash = (prev_search_hashes.get(bucket) or {}).get("sha256")
        if canonical_hash != old_hash:
            changed_search_buckets.append(bucket)

    manifest_dir = root / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    current_manifest_path = manifest_dir / "current.json"
    search_manifest_path = manifest_dir / "search.json"
    scan_manifest = {
        "version": 2,
        "batch_id": batch_id,
        "generated_at": datetime.now(TW_TZ).isoformat(),
        "full_scan": True,
        "source_rows": len(raw_rows),
        "current_documents": len(records),
        "added": added,
        "updated": updated,
        "removed": removed,
        "unchanged": max(0, unchanged),
        "changed_current_shards": len(changed_current_buckets),
        "changed_search_shards": len(changed_search_buckets),
        "note": "Crawler coverage is unchanged; unchanged business state is deduplicated by shard/content hashes.",
    }
    json.dump(current_manifest, open(current_manifest_path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    json.dump(search_manifest, open(search_manifest_path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    scan_path = manifest_dir / f"scan_{batch_id}.json"
    json.dump(scan_manifest, open(scan_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    history_path = root / "history" / f"events_{batch_id}.parquet"
    write_history_parquet(history_path, history_events)

    print(json.dumps(scan_manifest, ensure_ascii=False, indent=2))
    if args.offline:
        print(f"[v2] offline build complete: {root}")
        return

    from huggingface_hub import CommitOperationAdd, CommitOperationDelete, HfApi
    api = HfApi(token=token)
    api.create_repo(repo_id=args.repo_id, repo_type="dataset", exist_ok=True)

    operations = []
    for bucket in changed_current_buckets:
        local_path = current_dir / f"{bucket}.json.gz"
        operations.append(CommitOperationAdd(path_in_repo=f"v2/current/shards/{bucket}.json.gz", path_or_fileobj=str(local_path)))
    for bucket in changed_search_buckets:
        local_path = search_dir / f"{bucket}.json.gz"
        operations.append(CommitOperationAdd(path_in_repo=f"v2/search/shards/{bucket}.json.gz", path_or_fileobj=str(local_path)))

    # If a previously non-empty shard becomes empty the local empty shard replaces it, so no stale data survives.
    operations.extend([
        CommitOperationAdd(path_in_repo="v2/current/manifest.json", path_or_fileobj=str(current_manifest_path)),
        CommitOperationAdd(path_in_repo="v2/search/manifest.json", path_or_fileobj=str(search_manifest_path)),
        CommitOperationAdd(path_in_repo=f"v2/scans/{batch_id}.json", path_or_fileobj=str(scan_path)),
    ])
    if history_path.exists() and history_events:
        operations.append(CommitOperationAdd(path_in_repo=f"v2/history/events/{batch_id}.parquet", path_or_fileobj=str(history_path)))

    # HF recommends smaller commits. Split while keeping manifests in the last chunk.
    chunk_size = 80
    for index in range(0, len(operations), chunk_size):
        chunk = operations[index:index + chunk_size]
        api.create_commit(
            repo_id=args.repo_id,
            repo_type="dataset",
            operations=chunk,
            commit_message=f"v2 storage {batch_id} chunk {index // chunk_size + 1}",
        )

    # Validate required manifests exist remotely before the caller may clean legacy data.
    remote_files = set(api.list_repo_files(repo_id=args.repo_id, repo_type="dataset"))
    required = {"v2/current/manifest.json", "v2/search/manifest.json", f"v2/scans/{batch_id}.json"}
    missing = sorted(required - remote_files)
    if missing:
        raise SystemExit(f"v2 remote validation failed, missing: {missing}")
    print(f"[v2] remote publish complete; changed current shards={len(changed_current_buckets)}, search shards={len(changed_search_buckets)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-parquet", required=True)
    parser.add_argument("--batch-id", default="")
    parser.add_argument("--repo-id", default=os.environ.get("HF_REPO_ID") or "hub-google/UberEat")
    parser.add_argument("--output-dir", default="v2_build")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    build(args)


if __name__ == "__main__":
    main()
