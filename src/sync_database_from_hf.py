# -*- coding: utf-8 -*-
"""Catch a rebuilt serving DB up with every newer valid HF snapshot."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time

try:
    from src.rebuild_database import format_duration, iter_snapshot_documents, materialize_snapshots, validate_archive
    from src.serving_state import ServingStateCache, apply_snapshot, iter_documents, refresh_product_search
except ModuleNotFoundError:
    from rebuild_database import format_duration, iter_snapshot_documents, materialize_snapshots, validate_archive
    from serving_state import ServingStateCache, apply_snapshot, iter_documents, refresh_product_search


def sync(args: argparse.Namespace) -> dict[str, object]:
    if not os.path.exists(args.database):
        raise SystemExit("Full-history database is missing; run rebuild first")
    conn = sqlite3.connect(args.database)
    conn.row_factory = sqlite3.Row
    marker = conn.execute("SELECT value FROM metadata WHERE key='rebuild_snapshot_count'").fetchone()
    latest = conn.execute("SELECT value FROM metadata WHERE key='latest_batch'").fetchone()
    if not marker or not latest:
        raise SystemExit("Legacy baseline rejected: full-history rebuild metadata is missing")

    state_cache = ServingStateCache(conn)
    processed = []
    skipped = []
    sync_start = time.perf_counter()
    print(f"==================================================================", flush=True)
    print(f"[SYNC START] Catching up database from latest batch {latest[0]}", flush=True)
    print(f"==================================================================", flush=True)

    for idx, snapshot in enumerate(materialize_snapshots(args, after_batch=latest[0]), 1):
        batch_id = snapshot[0]
        source = snapshot[1]
        dl_sec = getattr(snapshot, "download_seconds", 0.0)
        size_mb = getattr(snapshot, "size_mb", 0.0)
        t_batch_start = time.perf_counter()

        prefix = f"[{idx}][{batch_id}]"
        if dl_sec > 0:
            print(f"{prefix} STEP 1/3: Download finished in {dl_sec:.2f}s ({size_mb:.2f} MB)", flush=True)

        t_extract_start = time.perf_counter()
        valid, reason = validate_archive(source, batch_id, args.minimum_stores)
        t_extract = time.perf_counter() - t_extract_start
        if not valid:
            skipped.append({"batch_id": batch_id, "reason": reason, "extract_duration_seconds": round(t_extract, 2)})
            print(f"{prefix} STEP 2/3: Validation FAILED in {t_extract:.2f}s -> SKIPPED ({reason})", flush=True)
            print(json.dumps({"batch_id": batch_id, "skipped": reason, "extract_duration_seconds": round(t_extract, 2)}, ensure_ascii=False), flush=True)
            continue
        print(f"{prefix} STEP 2/3: Archive validated in {t_extract:.2f}s ({reason})", flush=True)

        t_apply_start = time.perf_counter()
        counts = apply_snapshot(
            conn, iter_snapshot_documents(source), batch_id,
            missing_threshold=args.missing_threshold,
            baseline=False, event_retention_days=getattr(args, "event_retention_days", 60), refresh_search=False,
            state_cache=state_cache,
        )
        t_apply = time.perf_counter() - t_apply_start
        t_batch_total = time.perf_counter() - t_batch_start
        processed.append(batch_id)
        total_events = counts.get("new", 0) + counts.get("changed", 0) + counts.get("removed", 0) + counts.get("reappeared", 0)
        print(f"{prefix} STEP 3/3: DB Diff & Apply finished in {t_apply:.2f}s (stores: {counts['stores']}, products: {counts['products']}, events: {total_events})", flush=True)
        print(f"{prefix} >>> BATCH DONE in {t_batch_total:.2f}s [DL: {dl_sec:.2f}s | Extract: {t_extract:.2f}s | Diff: {t_apply:.2f}s]", flush=True)
        print(json.dumps({
            "batch_id": batch_id,
            "step": "sync_batch_complete",
            "duration_seconds": round(t_batch_total, 2),
            **counts,
        }, ensure_ascii=False), flush=True)

    if processed:
        t_fts_start = time.perf_counter()
        refresh_product_search(conn)
        print(f"[SYNC] FTS search index refreshed in {time.perf_counter() - t_fts_start:.2f}s", flush=True)
        conn.execute("INSERT OR REPLACE INTO metadata VALUES('rebuild_snapshot_count',?)", (str(int(marker[0]) + len(processed)),))
    if skipped:
        old = conn.execute("SELECT value FROM metadata WHERE key='rebuild_skipped_snapshots'").fetchone()
        history = json.loads(old[0]) if old and old[0] else []
        by_batch = {entry["batch_id"]: entry for entry in history + skipped}
        conn.execute("INSERT OR REPLACE INTO metadata VALUES('rebuild_skipped_snapshots',?)", (json.dumps(list(by_batch.values()), ensure_ascii=False),))
    conn.commit()
    total_sync_time = time.perf_counter() - sync_start
    summary = {
        "previous_latest": latest[0],
        "processed": processed,
        "skipped": skipped,
        "duration_seconds": round(total_sync_time, 2),
    }
    conn.close()
    print("==================================================================", flush=True)
    print(f"[SYNC COMPLETE] Total Time: {format_duration(total_sync_time)} ({total_sync_time:.2f}s) | Processed: {len(processed)}", flush=True)
    print("==================================================================", flush=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--repo-id", default=os.getenv("HF_REPO_ID", "hub-google/UberEat"))
    parser.add_argument("--source-dir")
    parser.add_argument("--minimum-stores", type=int, default=10000)
    parser.add_argument("--missing-threshold", type=int, default=3)
    parser.add_argument("--event-retention-days", type=int, default=60)
    sync(parser.parse_args())


if __name__ == "__main__":
    main()
