# -*- coding: utf-8 -*-
"""Build UberEat v2 storage on Hugging Face.

Principles:
- crawler coverage/frequency is unchanged;
- current serving data is sharded and content-hashed;
- history stores only actual product-state changes, never repeated snapshots;
- first migration is a baseline and does NOT duplicate the whole catalog into history;
- full-catalog keyword search uses a sharded CJK/Latin n-gram inverted index;
- processing is streamed/spooled so a million-row catalog does not live in RAM twice.
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
from typing import Any, Dict, Iterable, Iterator, List, Tuple

TW_TZ = timezone(timedelta(hours=8))
BUCKETS = 256
BATCH_SIZE = 10_000


def norm_text(value: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).lower()).strip()


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

    # IMPORTANT: history hash is product-state only. Store rating/address/name changes
    # must not create thousands of fake product history rows for one restaurant.
    product_state = {
        "product_id": record["product_id"],
        "store_id": record["store_id"],
        "product_name": record["product_name"],
        "category_name": record["category_name"],
        "description": record["description"],
        "price": record["price"],
        "quantity": record["quantity"],
        "promo_type": record["promo_type"],
        "eff_price": record["eff_price"],
        "is_open": record["is_open"],
    }
    record["_h"] = sha256_bytes(canonical_json_bytes(product_state))
    return record


def grams(text: str) -> Iterable[str]:
    """All 1/2/3-grams for exact substring candidate lookup.

    This is intentionally exhaustive for names/descriptions: a query is never
    restricted to popular products. Final candidates are re-verified against
    full normalized search_text in the browser.
    """
    seen = set()
    for run in re.findall(r"[0-9a-z\u3400-\u9fff]+", norm_text(text)):
        max_n = min(3, len(run))
        for n in range(1, max_n + 1):
            for i in range(0, len(run) - n + 1):
                token = run[i:i + n]
                if token not in seen:
                    seen.add(token)
                    yield token


def iter_parquet_rows(path: str) -> Iterator[Dict[str, Any]]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise SystemExit("pyarrow is required") from exc
    pf = pq.ParquetFile(path)
    for batch in pf.iter_batches(batch_size=BATCH_SIZE):
        for row in batch.to_pylist():
            yield row


def parquet_row_count(path: str) -> int:
    import pyarrow.parquet as pq
    return pq.ParquetFile(path).metadata.num_rows


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


def remote_record_shard(repo_id: str, token: str | None, bucket: str) -> Dict[str, Dict[str, Any]]:
    path = download_optional(repo_id, f"v2/current/shards/{bucket}.json.gz", token)
    if not path:
        return {}
    try:
        return {str(r["id"]): r for r in read_gzip_json(path)}
    except Exception:
        return {}


def write_history_parquet(path: Path, events: List[Dict[str, Any]]) -> None:
    if not events:
        return
    import pyarrow as pa
    import pyarrow.parquet as pq
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(events), path, compression="zstd")


def open_spool_handles(directory: Path, suffix: str):
    directory.mkdir(parents=True, exist_ok=True)
    return {
        f"{i:02x}": open(directory / f"{i:02x}.{suffix}", "w", encoding="utf-8", buffering=1024 * 1024)
        for i in range(BUCKETS)
    }


def close_handles(handles: Dict[str, Any]) -> None:
    for fh in handles.values():
        try:
            fh.close()
        except Exception:
            pass


def build(args: argparse.Namespace) -> None:
    token = os.environ.get("HF_TOKEN")
    if not token and not args.offline:
        raise SystemExit("HF_TOKEN is required unless --offline is used")

    batch_id = args.batch_id or datetime.now(TW_TZ).strftime("%Y%m%d%H%M%S")
    root = Path(args.output_dir or tempfile.mkdtemp(prefix="ubereat-v2-"))
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    prev_current_manifest = old_manifest(args.repo_id, token, "current") if not args.offline else {}
    prev_search_manifest = old_manifest(args.repo_id, token, "search") if not args.offline else {}
    prev_current_hashes = prev_current_manifest.get("shards", {})
    prev_search_hashes = prev_search_manifest.get("shards", {})
    is_baseline = not bool(prev_current_manifest)

    current_spool = root / "_spool" / "current"
    search_spool = root / "_spool" / "search"
    current_handles = open_spool_handles(current_spool, "jsonl")
    search_handles = open_spool_handles(search_spool, "tsv")

    source_rows = parquet_row_count(args.input_parquet)
    valid_rows = 0
    malformed = 0
    print(f"[v2] stream catalog: {args.input_parquet} ({source_rows:,} parquet rows)")

    try:
        for row in iter_parquet_rows(args.input_parquet):
            try:
                rec = normalize_record(row)
                if not (rec["store_id"] and rec["product_id"] and rec["product_name"] and rec["price"] > 0):
                    malformed += 1
                    continue
                did = rec["id"]
                current_handles[bucket_for_id(did)].write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
                for token_value in grams(rec["search_text"]):
                    search_handles[bucket_for_token(token_value)].write(f"{token_value}\t{did}\n")
                valid_rows += 1
                if valid_rows % 100_000 == 0:
                    print(f"[v2] streamed {valid_rows:,} valid documents")
            except Exception:
                malformed += 1
    finally:
        close_handles(current_handles)
        close_handles(search_handles)

    current_manifest = {
        "version": 2,
        "schema": "current-product-record-v2",
        "batch_id": batch_id,
        "generated_at": datetime.now(TW_TZ).isoformat(),
        "total_documents": valid_rows,
        "shards": {},
    }
    changed_current_buckets: List[str] = []
    history_events: List[Dict[str, Any]] = []
    added = updated = removed = 0
    current_dir = root / "current" / "shards"

    print("[v2] materializing current shards + product change events")
    for i in range(BUCKETS):
        bucket = f"{i:02x}"
        spool_path = current_spool / f"{bucket}.jsonl"
        rows: List[Dict[str, Any]] = []
        if spool_path.exists():
            with open(spool_path, encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        rows.append(json.loads(line))
        rows.sort(key=lambda r: r["id"])

        out_path = current_dir / f"{bucket}.json.gz"
        canonical_hash, size_bytes = write_gzip_json(out_path, rows)
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

        # Baseline is only current state; never duplicate a million rows into history.
        if is_baseline or args.offline:
            continue

        old_rows = remote_record_shard(args.repo_id, token, bucket) if old_hash else {}
        new_rows = {str(r["id"]): r for r in rows}
        changed_at = datetime.now(TW_TZ).isoformat()
        for did, rec in new_rows.items():
            old = old_rows.get(did)
            if old is None:
                added += 1
                history_events.append({
                    "batch_id": batch_id, "changed_at": changed_at, "change_type": "added",
                    **{k: v for k, v in rec.items() if k != "search_text"}, "previous_hash": None,
                })
            elif old.get("_h") != rec.get("_h"):
                updated += 1
                history_events.append({
                    "batch_id": batch_id, "changed_at": changed_at, "change_type": "updated",
                    **{k: v for k, v in rec.items() if k != "search_text"}, "previous_hash": old.get("_h"),
                    "previous_price": old.get("price"), "previous_eff_price": old.get("eff_price"),
                })
        for did, old in old_rows.items():
            if did not in new_rows:
                removed += 1
                history_events.append({
                    "batch_id": batch_id, "changed_at": changed_at, "change_type": "removed",
                    **{k: v for k, v in old.items() if k != "search_text"}, "previous_hash": old.get("_h"),
                })

    if is_baseline:
        added = valid_rows
        updated = removed = 0
        unchanged = 0
    else:
        unchanged = max(0, valid_rows - added - updated)

    search_manifest = {
        "version": 2,
        "schema": "ngram-postings-v2",
        "batch_id": batch_id,
        "generated_at": datetime.now(TW_TZ).isoformat(),
        "algorithm": "nfkc-lower-exact-verify-ngram-1-3",
        "total_documents": valid_rows,
        "shards": {},
    }
    changed_search_buckets: List[str] = []
    search_dir = root / "search" / "shards"

    print("[v2] materializing search shards")
    for i in range(BUCKETS):
        bucket = f"{i:02x}"
        postings: Dict[str, List[str]] = defaultdict(list)
        spool_path = search_spool / f"{bucket}.tsv"
        if spool_path.exists():
            with open(spool_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    token_value, did = line.split("\t", 1)
                    postings[token_value].append(did)
        data = {token_value: sorted(ids) for token_value, ids in sorted(postings.items())}
        out_path = search_dir / f"{bucket}.json.gz"
        canonical_hash, size_bytes = write_gzip_json(out_path, data)
        search_manifest["shards"][bucket] = {
            "sha256": canonical_hash,
            "tokens": len(data),
            "bytes": size_bytes,
            "path": f"v2/search/shards/{bucket}.json.gz",
        }
        old_hash = (prev_search_hashes.get(bucket) or {}).get("sha256")
        if canonical_hash != old_hash:
            changed_search_buckets.append(bucket)
        del postings, data

    manifest_dir = root / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    current_manifest_path = manifest_dir / "current.json"
    search_manifest_path = manifest_dir / "search.json"
    json.dump(current_manifest, open(current_manifest_path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    json.dump(search_manifest, open(search_manifest_path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))

    scan_manifest = {
        "version": 2,
        "batch_id": batch_id,
        "generated_at": datetime.now(TW_TZ).isoformat(),
        "full_scan": True,
        "baseline": is_baseline,
        "source_rows": source_rows,
        "current_documents": valid_rows,
        "malformed_or_filtered": malformed,
        "added": added,
        "updated": updated,
        "removed": removed,
        "unchanged": unchanged,
        "history_events_written": 0 if is_baseline else len(history_events),
        "changed_current_shards": len(changed_current_buckets),
        "changed_search_shards": len(changed_search_buckets),
        "note": "Full crawler scan retained. Repeated unchanged business state is not appended to history.",
    }
    scan_path = manifest_dir / f"scan_{batch_id}.json"
    json.dump(scan_manifest, open(scan_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    history_path = root / "history" / f"events_{batch_id}.parquet"
    if not is_baseline:
        write_history_parquet(history_path, history_events)

    print(json.dumps(scan_manifest, ensure_ascii=False, indent=2))
    if args.offline:
        print(f"[v2] offline build complete: {root}")
        return

    from huggingface_hub import CommitOperationAdd, HfApi
    api = HfApi(token=token)
    api.create_repo(repo_id=args.repo_id, repo_type="dataset", exist_ok=True)

    # Data shards first; manifests are added last so readers never observe a manifest
    # that points at not-yet-uploaded shards.
    data_operations = []
    for bucket in changed_current_buckets:
        data_operations.append(CommitOperationAdd(
            path_in_repo=f"v2/current/shards/{bucket}.json.gz",
            path_or_fileobj=str(current_dir / f"{bucket}.json.gz"),
        ))
    for bucket in changed_search_buckets:
        data_operations.append(CommitOperationAdd(
            path_in_repo=f"v2/search/shards/{bucket}.json.gz",
            path_or_fileobj=str(search_dir / f"{bucket}.json.gz"),
        ))
    if history_path.exists() and history_events:
        data_operations.append(CommitOperationAdd(
            path_in_repo=f"v2/history/events/{batch_id}.parquet",
            path_or_fileobj=str(history_path),
        ))

    chunk_size = 60
    for index in range(0, len(data_operations), chunk_size):
        api.create_commit(
            repo_id=args.repo_id,
            repo_type="dataset",
            operations=data_operations[index:index + chunk_size],
            commit_message=f"v2 data {batch_id} chunk {index // chunk_size + 1}",
        )

    manifest_operations = [
        CommitOperationAdd(path_in_repo="v2/current/manifest.json", path_or_fileobj=str(current_manifest_path)),
        CommitOperationAdd(path_in_repo="v2/search/manifest.json", path_or_fileobj=str(search_manifest_path)),
        CommitOperationAdd(path_in_repo=f"v2/scans/{batch_id}.json", path_or_fileobj=str(scan_path)),
    ]
    api.create_commit(
        repo_id=args.repo_id,
        repo_type="dataset",
        operations=manifest_operations,
        commit_message=f"v2 manifests {batch_id}",
    )

    remote_files = set(api.list_repo_files(repo_id=args.repo_id, repo_type="dataset"))
    required = {"v2/current/manifest.json", "v2/search/manifest.json", f"v2/scans/{batch_id}.json"}
    missing = sorted(required - remote_files)
    if missing:
        raise SystemExit(f"v2 remote validation failed, missing: {missing}")
    print(f"[v2] publish complete: current shards changed={len(changed_current_buckets)}, search shards changed={len(changed_search_buckets)}, history events={0 if is_baseline else len(history_events)}")


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
