# -*- coding: utf-8 -*-
"""Catch a rebuilt serving DB up with every newer valid HF snapshot."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3

try:
    from src.rebuild_database import materialize_snapshots, validate_archive
    from src.serving_state import apply_snapshot, iter_documents, refresh_product_search
except ModuleNotFoundError:
    from rebuild_database import materialize_snapshots, validate_archive
    from serving_state import apply_snapshot, iter_documents, refresh_product_search


def sync(args: argparse.Namespace) -> dict[str, object]:
    if not os.path.exists(args.database):
        raise SystemExit("Full-history database is missing; run rebuild first")
    conn = sqlite3.connect(args.database)
    conn.row_factory = sqlite3.Row
    marker = conn.execute("SELECT value FROM metadata WHERE key='rebuild_snapshot_count'").fetchone()
    latest = conn.execute("SELECT value FROM metadata WHERE key='latest_batch'").fetchone()
    if not marker or not latest:
        raise SystemExit("Legacy baseline rejected: full-history rebuild metadata is missing")

    processed = []
    skipped = []
    for batch_id, source in materialize_snapshots(args, after_batch=latest[0]):
        valid, reason = validate_archive(source, batch_id, args.minimum_stores)
        if not valid:
            skipped.append({"batch_id": batch_id, "reason": reason})
            print(json.dumps({"batch_id": batch_id, "skipped": reason}, ensure_ascii=False))
            continue
        counts = apply_snapshot(
            conn, iter_documents(source), batch_id,
            missing_threshold=args.missing_threshold,
            baseline=False, event_retention_days=None, refresh_search=False,
        )
        processed.append(batch_id)
        print(json.dumps({"batch_id": batch_id, **counts}, ensure_ascii=False))

    if processed:
        refresh_product_search(conn)
        conn.execute("INSERT OR REPLACE INTO metadata VALUES('rebuild_snapshot_count',?)", (str(int(marker[0]) + len(processed)),))
    if skipped:
        old = conn.execute("SELECT value FROM metadata WHERE key='rebuild_skipped_snapshots'").fetchone()
        history = json.loads(old[0]) if old and old[0] else []
        by_batch = {entry["batch_id"]: entry for entry in history + skipped}
        conn.execute("INSERT OR REPLACE INTO metadata VALUES('rebuild_skipped_snapshots',?)", (json.dumps(list(by_batch.values()), ensure_ascii=False),))
    conn.commit()
    summary = {"previous_latest": latest[0], "processed": processed, "skipped": skipped}
    conn.close()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--repo-id", default=os.getenv("HF_REPO_ID", "hub-google/UberEat"))
    parser.add_argument("--source-dir")
    parser.add_argument("--minimum-stores", type=int, default=10000)
    parser.add_argument("--missing-threshold", type=int, default=3)
    sync(parser.parse_args())


if __name__ == "__main__":
    main()
