# -*- coding: utf-8 -*-
"""Rebuild a serving database by replaying every available menu snapshot.

Snapshots are processed oldest-first.  The first snapshot is a baseline; every
later snapshot produces events using the same rules as the twice-daily pipeline.
The output is written atomically, so a failed rebuild never replaces a good DB.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import tempfile
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterator


class SnapshotItem(tuple):
    """2-tuple (batch_id, path) with timing and metadata attributes."""
    batch_id: str
    path: str
    download_seconds: float
    size_mb: float

    def __new__(cls, batch_id: str, path: str, download_seconds: float = 0.0, size_mb: float = 0.0):
        obj = super().__new__(cls, (batch_id, path))
        obj.batch_id = batch_id
        obj.path = path
        obj.download_seconds = download_seconds
        obj.size_mb = size_mb
        return obj


def format_duration(seconds: float) -> str:
    sec = max(0, int(round(seconds)))
    mins, s = divmod(sec, 60)
    hrs, m = divmod(mins, 60)
    if hrs > 0:
        return f"{hrs}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"

try:
    import orjson

    def json_loads(data: bytes | str) -> Any:
        return orjson.loads(data)
except ImportError:
    def json_loads(data: bytes | str) -> Any:
        return json.loads(data)

try:
    from src.serving_state import (
        ServingStateCache,
        apply_snapshot,
        iter_documents,
        refresh_product_search,
        store_identity,
    )
except ModuleNotFoundError:  # direct execution: python src/rebuild_database.py
    from serving_state import (
        ServingStateCache,
        apply_snapshot,
        iter_documents,
        refresh_product_search,
        store_identity,
    )

SNAPSHOT_RE = re.compile(r"(?:^|/)TaiwanMenuSnapshots/(20\d{12})/taiwan_menus_\1\.tar\.gz$")
LOCAL_RE = re.compile(r"taiwan_menus_(20\d{12})\.tar\.gz$")


def local_snapshots(source_dir: Path) -> list[tuple[str, str]]:
    found: dict[str, str] = {}
    for path in source_dir.rglob("taiwan_menus_*.tar.gz"):
        match = LOCAL_RE.search(path.name)
        if match:
            found[match.group(1)] = str(path)
    return sorted(found.items())


def hf_snapshot_paths(repo_id: str, token: str | None) -> list[tuple[str, str]]:
    from huggingface_hub import HfApi

    files = HfApi(token=token).list_repo_files(repo_id=repo_id, repo_type="dataset")
    found = []
    for path in files:
        match = SNAPSHOT_RE.search(path)
        if match:
            found.append((match.group(1), path))
    return sorted(found)


def materialize_snapshots(args: argparse.Namespace, after_batch: str | None = None) -> Iterator[SnapshotItem]:
    if args.source_dir:
        for batch, path in local_snapshots(Path(args.source_dir)):
            if not after_batch or batch > after_batch:
                size_mb = os.path.getsize(path) / (1024 * 1024) if os.path.exists(path) else 0.0
                yield SnapshotItem(batch, path, download_seconds=0.0, size_mb=size_mb)
        return

    token = os.getenv("HF_TOKEN")
    items = [(batch_id, remote_path) for batch_id, remote_path in hf_snapshot_paths(args.repo_id, token) if not after_batch or batch_id > after_batch]
    if not items:
        return

    from huggingface_hub import hf_hub_download

    def _download(b_id: str, r_path: str):
        t0 = time.perf_counter()
        c_dir = tempfile.mkdtemp(prefix=f"snapshot-{b_id}-")
        local_p = hf_hub_download(
            repo_id=args.repo_id,
            filename=r_path,
            repo_type="dataset",
            token=token,
            cache_dir=c_dir,
        )
        duration = time.perf_counter() - t0
        size_mb = os.path.getsize(local_p) / (1024 * 1024) if os.path.exists(local_p) else 0.0
        return b_id, local_p, c_dir, duration, size_mb

    executor = ThreadPoolExecutor(max_workers=2)
    futures = []
    prefetch_depth = 2
    item_iter = iter(items)
    for _ in range(prefetch_depth):
        try:
            b, r = next(item_iter)
            futures.append(executor.submit(_download, b, r))
        except StopIteration:
            break

    try:
        while futures:
            fut = futures.pop(0)
            b_id, local_path, c_dir, dl_sec, size_mb = fut.result()
            try:
                next_b, next_r = next(item_iter)
                futures.append(executor.submit(_download, next_b, next_r))
            except StopIteration:
                pass
            try:
                yield SnapshotItem(b_id, local_path, download_seconds=dl_sec, size_mb=size_mb)
            finally:
                shutil.rmtree(c_dir, ignore_errors=True)
    finally:
        executor.shutdown(wait=False)


def is_store_in_shard(store_id: str, shard_id: int | None, total_shards: int = 1) -> bool:
    if shard_id is None or total_shards <= 1:
        return True
    import hashlib
    digest = hashlib.md5(store_id.encode("utf-8")).hexdigest()
    return (int(digest[:8], 16) % total_shards) == shard_id


def validate_and_extract_archive(
    path: str,
    batch_id: str,
    minimum_stores: int,
    shard_id: int | None = None,
    total_shards: int = 1,
) -> tuple[bool, None, str]:
    """Validate an archive without retaining its documents in memory."""
    try:
        manifest: dict[str, Any] | None = None
        identities: set[str] = set()

        with tarfile.open(path, "r:*") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                if member.name == "manifest.json":
                    handle = archive.extractfile(member)
                    manifest = json_loads(handle.read()) if handle else {}
                elif member.name.startswith("Json/") and member.name.endswith(".json"):
                    handle = archive.extractfile(member)
                    if not handle:
                        return False, None, f"unable to extract {member.name}"
                    try:
                        document = json_loads(handle.read())
                    except Exception:
                        return False, None, f"invalid JSON document: {member.name}"
                    if not isinstance(document, dict):
                        return False, None, f"invalid JSON document: {member.name}"
                    menu = document.get("hasMenu")
                    if not isinstance(menu, dict) or not isinstance(menu.get("hasMenuSection"), list):
                        return False, None, f"invalid menu structure: {member.name}"
                    identity, _ = store_identity(document)
                    if identity in identities:
                        return False, None, f"duplicate store identity: {identity}"
                    identities.add(identity)

        if manifest is None:
            return False, None, "manifest.json missing"
        declared = int(manifest.get("store_count") or 0)
        if manifest.get("batch_id") != batch_id:
            return False, None, f"manifest batch mismatch: {manifest.get('batch_id')}"
        if declared != len(identities):
            return False, None, f"manifest stores={declared} archive JSON={len(identities)}"
        if len(identities) < minimum_stores:
            return False, None, f"only {len(identities)} stores (minimum {minimum_stores})"
        return True, None, f"{len(identities)} stores validated"
    except (OSError, tarfile.TarError, ValueError, TypeError, AttributeError, UnicodeDecodeError) as error:
        return False, None, str(error)


def validate_archive(path: str, batch_id: str, minimum_stores: int) -> tuple[bool, str]:
    valid, _, reason = validate_and_extract_archive(path, batch_id, minimum_stores)
    return valid, reason


def iter_snapshot_documents(path: str, shard_id: int | None = None, total_shards: int = 1):
    """Stream validated store documents, optionally selecting one rebuild shard."""
    for document in iter_documents(path):
        identity, _ = store_identity(document)
        if is_store_in_shard(identity, shard_id, total_shards):
            yield document


def rebuild(args: argparse.Namespace) -> dict[str, object]:
    output = Path(args.database).resolve()
    if output.exists() and not args.replace:
        raise SystemExit(f"Refusing to replace existing database without --replace: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".rebuilding", dir=output.parent)
    os.close(fd)
    temp_db = Path(temp_name)
    temp_db.unlink()

    shard_id = getattr(args, "shard_id", None)
    total_shards = getattr(args, "total_shards", 1)
    if shard_id is not None:
        if total_shards < 1 or shard_id < 0 or shard_id >= total_shards:
            raise ValueError(f"Invalid shard configuration: shard_id={shard_id}, total_shards={total_shards}")

    processed: list[str] = []
    skipped: list[dict[str, Any]] = []
    try:
        conn = sqlite3.connect(temp_db)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=FILE")
        conn.execute("PRAGMA cache_size=-64000")
        conn.execute("PRAGMA mmap_size=268435456")

        state_cache = ServingStateCache(conn)

        if args.source_dir:
            available_batches = [b for b, _ in local_snapshots(Path(args.source_dir))]
        else:
            token = os.getenv("HF_TOKEN")
            available_batches = [b for b, _ in hf_snapshot_paths(args.repo_id, token)]
        total_snapshots = min(len(available_batches), args.max_snapshots) if args.max_snapshots else len(available_batches)

        shard_desc = f" (Shard {shard_id + 1}/{total_shards})" if shard_id is not None else ""
        print(f"==================================================================", flush=True)
        print(f"[REBUILD START]{shard_desc} Total snapshots to process: {total_snapshots}", flush=True)
        print(f"==================================================================", flush=True)

        rebuild_start_time = time.perf_counter()
        batch_durations: list[float] = []

        snapshots = materialize_snapshots(args)
        for idx, snapshot in enumerate(snapshots, 1):
            if args.max_snapshots and len(processed) >= args.max_snapshots:
                break

            batch_id = snapshot[0]
            source = snapshot[1]
            dl_sec = getattr(snapshot, "download_seconds", 0.0)
            size_mb = getattr(snapshot, "size_mb", 0.0)

            batch_start_time = time.perf_counter()
            prefix = f"[{idx}/{total_snapshots}][{batch_id}]"

            # 1. Download
            if dl_sec > 0:
                print(f"{prefix} STEP 1/3: Download finished in {dl_sec:.2f}s (size: {size_mb:.2f} MB)", flush=True)
            else:
                if size_mb == 0 and os.path.exists(source):
                    size_mb = os.path.getsize(source) / (1024 * 1024)
                print(f"{prefix} STEP 1/3: Archive source ready ({size_mb:.2f} MB)", flush=True)

            # 2. Extract & Validate
            t_extract_start = time.perf_counter()
            valid, _, reason = validate_and_extract_archive(
                source,
                batch_id,
                args.minimum_stores,
                shard_id=shard_id,
                total_shards=total_shards,
            )
            t_extract = time.perf_counter() - t_extract_start

            if not valid:
                skipped.append({"batch_id": batch_id, "reason": reason, "extract_duration_seconds": round(t_extract, 2)})
                print(f"{prefix} STEP 2/3: Extract & Validate FAILED in {t_extract:.2f}s -> SKIPPED ({reason})", flush=True)
                print(json.dumps({
                    "batch_id": batch_id,
                    "step": "skipped",
                    "reason": reason,
                    "extract_duration_seconds": round(t_extract, 2),
                }, ensure_ascii=False), flush=True)
                continue

            print(f"{prefix} STEP 2/3: Archive validation finished in {t_extract:.2f}s ({reason})", flush=True)

            # 3. Apply Snapshot Diff
            t_apply_start = time.perf_counter()
            counts = apply_snapshot(
                conn,
                iter_snapshot_documents(source, shard_id, total_shards),
                batch_id,
                missing_threshold=args.missing_threshold,
                baseline=(len(processed) == 0),
                event_retention_days=None,
                refresh_search=False,
                state_cache=state_cache,
            )
            t_apply = time.perf_counter() - t_apply_start
            total_events = counts.get("new", 0) + counts.get("changed", 0) + counts.get("removed", 0) + counts.get("reappeared", 0)
            print(f"{prefix} STEP 3/3: DB Diff & Apply finished in {t_apply:.2f}s (stores: {counts['stores']}, products: {counts['products']}, events: {total_events})", flush=True)

            # Batch Summary
            t_batch_total = time.perf_counter() - batch_start_time
            batch_durations.append(t_batch_total)
            processed.append(batch_id)

            cumulative_time = time.perf_counter() - rebuild_start_time
            avg_batch_time = sum(batch_durations) / len(batch_durations)
            remaining_batches = total_snapshots - idx
            eta_seconds = remaining_batches * avg_batch_time

            print(
                f"{prefix} >>> BATCH DONE in {t_batch_total:.2f}s "
                f"[DL: {dl_sec:.2f}s | Extract: {t_extract:.2f}s | Diff: {t_apply:.2f}s] "
                f"| Elapsed: {format_duration(cumulative_time)} | ETA: {format_duration(eta_seconds)}",
                flush=True,
            )

            batch_summary = {
                "batch_id": batch_id,
                "step": "batch_complete",
                "progress": f"{idx}/{total_snapshots}",
                "duration_seconds": round(t_batch_total, 2),
                "step_durations": {
                    "download_seconds": round(dl_sec, 2),
                    "extract_seconds": round(t_extract, 2),
                    "apply_seconds": round(t_apply, 2),
                },
                "elapsed_seconds": round(cumulative_time, 2),
                "eta_seconds": round(eta_seconds, 2),
                **counts,
            }
            print(json.dumps(batch_summary, ensure_ascii=False), flush=True)

        if not processed:
            raise RuntimeError("No complete TaiwanMenuSnapshots archives were found")

        print("==================================================================", flush=True)
        print("[POST-PROCESSING] Refreshing FTS product search index...", flush=True)
        t_fts_start = time.perf_counter()
        refresh_product_search(conn)
        t_fts = time.perf_counter() - t_fts_start
        print(f"[POST-PROCESSING] FTS product search index refreshed in {t_fts:.2f}s", flush=True)

        t_meta_start = time.perf_counter()
        conn.execute("INSERT OR REPLACE INTO metadata VALUES('rebuild_first_batch',?)", (processed[0],))
        conn.execute("INSERT OR REPLACE INTO metadata VALUES('rebuild_last_batch',?)", (processed[-1],))
        conn.execute("INSERT OR REPLACE INTO metadata VALUES('rebuild_snapshot_count',?)", (str(len(processed)),))
        conn.execute("INSERT OR REPLACE INTO metadata VALUES('rebuild_skipped_snapshots',?)", (json.dumps(skipped, ensure_ascii=False),))
        if shard_id is not None:
            conn.execute("INSERT OR REPLACE INTO metadata VALUES('rebuild_shard_id',?)", (str(shard_id),))
            conn.execute("INSERT OR REPLACE INTO metadata VALUES('rebuild_total_shards',?)", (str(total_shards),))
        conn.commit()
        t_meta = time.perf_counter() - t_meta_start
        print(f"[POST-PROCESSING] SQLite metadata committed in {t_meta:.2f}s", flush=True)

        total_rebuild_time = time.perf_counter() - rebuild_start_time
        summary = {
            "snapshots": len(processed),
            "first_batch": processed[0],
            "last_batch": processed[-1],
            "stores": conn.execute("SELECT count(*) FROM stores").fetchone()[0],
            "products": conn.execute("SELECT count(*) FROM products").fetchone()[0],
            "events": conn.execute("SELECT count(*) FROM events").fetchone()[0],
            "total_duration_seconds": round(total_rebuild_time, 2),
            "skipped": skipped,
        }
        if shard_id is not None:
            summary["shard_id"] = shard_id
            summary["total_shards"] = total_shards
        conn.close()
        for suffix in ("-wal", "-shm"):
            Path(str(temp_db) + suffix).unlink(missing_ok=True)
        if output.exists():
            output.unlink()
        shutil.move(str(temp_db), str(output))
        print("==================================================================", flush=True)
        print(f"[REBUILD COMPLETE]{shard_desc} Total Time: {format_duration(total_rebuild_time)} ({total_rebuild_time:.2f}s) | Snapshots: {len(processed)}", flush=True)
        print(f"Final Stats: stores={summary['stores']:,}, products={summary['products']:,}, events={summary['events']:,}", flush=True)
        print("==================================================================", flush=True)
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        return summary
    except BaseException:
        temp_db.unlink(missing_ok=True)
        Path(str(temp_db) + "-wal").unlink(missing_ok=True)
        Path(str(temp_db) + "-shm").unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--source-dir", help="Directory containing downloaded snapshot archives")
    source.add_argument("--repo-id", default=os.getenv("HF_REPO_ID", "hub-google/UberEat"))
    parser.add_argument("--database", required=True)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--missing-threshold", type=int, default=3)
    parser.add_argument("--minimum-stores", type=int, default=10000,
                        help="Reject incomplete archives below this store count")
    parser.add_argument("--max-snapshots", type=int, help="Test-only limit; oldest snapshots are used")
    parser.add_argument("--shard-id", type=int, default=None, help="0-based shard index for parallel rebuild")
    parser.add_argument("--total-shards", type=int, default=1, help="Total number of shards for parallel rebuild")
    rebuild(parser.parse_args())


if __name__ == "__main__":
    main()
