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
from pathlib import Path
from typing import Iterator

try:
    from src.serving_state import apply_snapshot, iter_documents, refresh_product_search, store_identity
except ModuleNotFoundError:  # direct execution: python src/rebuild_database.py
    from serving_state import apply_snapshot, iter_documents, refresh_product_search, store_identity

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


def materialize_snapshots(args: argparse.Namespace, after_batch: str | None = None) -> Iterator[tuple[str, str]]:
    if args.source_dir:
        yield from ((batch, path) for batch, path in local_snapshots(Path(args.source_dir)) if not after_batch or batch > after_batch)
        return

    token = os.getenv("HF_TOKEN")
    for batch_id, remote_path in hf_snapshot_paths(args.repo_id, token):
        if after_batch and batch_id <= after_batch:
            continue
        with tempfile.TemporaryDirectory(prefix=f"snapshot-{batch_id}-") as cache_dir:
            from huggingface_hub import hf_hub_download

            local = hf_hub_download(
                repo_id=args.repo_id,
                filename=remote_path,
                repo_type="dataset",
                token=token,
                cache_dir=cache_dir,
            )
            yield batch_id, local


def validate_archive(path: str, batch_id: str, minimum_stores: int) -> tuple[bool, str]:
    try:
        with tarfile.open(path, "r:*") as archive:
            members = archive.getmembers()
            json_members = [member for member in members if member.isfile() and member.name.startswith("Json/") and member.name.endswith(".json")]
            json_count = len(json_members)
            manifest_member = next((member for member in members if member.name == "manifest.json"), None)
            if manifest_member is None:
                return False, "manifest.json missing"
            handle = archive.extractfile(manifest_member)
            manifest = json.load(handle) if handle else {}
            identities = set()
            for member in json_members:
                handle = archive.extractfile(member)
                document = json.load(handle) if handle else None
                if not isinstance(document, dict):
                    return False, f"invalid JSON document: {member.name}"
                menu = document.get("hasMenu")
                if not isinstance(menu, dict) or not isinstance(menu.get("hasMenuSection"), list):
                    return False, f"invalid menu structure: {member.name}"
                identity, _ = store_identity(document)
                if identity in identities:
                    return False, f"duplicate store identity: {identity}"
                identities.add(identity)
        declared = int(manifest.get("store_count") or 0)
        if manifest.get("batch_id") != batch_id:
            return False, f"manifest batch mismatch: {manifest.get('batch_id')}"
        if declared != json_count:
            return False, f"manifest stores={declared} archive JSON={json_count}"
        if json_count < minimum_stores:
            return False, f"only {json_count} stores (minimum {minimum_stores})"
        return True, f"{json_count} stores"
    except (OSError, tarfile.TarError, ValueError, TypeError, AttributeError, json.JSONDecodeError, UnicodeDecodeError) as error:
        return False, str(error)


def rebuild(args: argparse.Namespace) -> dict[str, object]:
    output = Path(args.database).resolve()
    if output.exists() and not args.replace:
        raise SystemExit(f"Refusing to replace existing database without --replace: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".rebuilding", dir=output.parent)
    os.close(fd)
    temp_db = Path(temp_name)
    temp_db.unlink()

    processed: list[str] = []
    skipped: list[dict[str, str]] = []
    try:
        conn = sqlite3.connect(temp_db)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")

        snapshots = materialize_snapshots(args)
        for batch_id, source in snapshots:
            if args.max_snapshots and len(processed) >= args.max_snapshots:
                break
            valid, reason = validate_archive(source, batch_id, args.minimum_stores)
            if not valid:
                skipped.append({"batch_id": batch_id, "reason": reason})
                print(json.dumps({"batch_id": batch_id, "skipped": reason}, ensure_ascii=False))
                continue
            counts = apply_snapshot(
                conn,
                iter_documents(source),
                batch_id,
                missing_threshold=args.missing_threshold,
                baseline=(len(processed) == 0),
                event_retention_days=None,
                refresh_search=False,
            )
            processed.append(batch_id)
            print(json.dumps({"batch_id": batch_id, **counts}, ensure_ascii=False))

        if not processed:
            raise RuntimeError("No complete TaiwanMenuSnapshots archives were found")

        refresh_product_search(conn)
        conn.execute("INSERT OR REPLACE INTO metadata VALUES('rebuild_first_batch',?)", (processed[0],))
        conn.execute("INSERT OR REPLACE INTO metadata VALUES('rebuild_last_batch',?)", (processed[-1],))
        conn.execute("INSERT OR REPLACE INTO metadata VALUES('rebuild_snapshot_count',?)", (str(len(processed)),))
        conn.execute("INSERT OR REPLACE INTO metadata VALUES('rebuild_skipped_snapshots',?)", (json.dumps(skipped, ensure_ascii=False),))
        conn.commit()
        summary = {
            "snapshots": len(processed),
            "first_batch": processed[0],
            "last_batch": processed[-1],
            "stores": conn.execute("SELECT count(*) FROM stores").fetchone()[0],
            "products": conn.execute("SELECT count(*) FROM products").fetchone()[0],
            "events": conn.execute("SELECT count(*) FROM events").fetchone()[0],
            "skipped": skipped,
        }
        conn.close()
        for suffix in ("-wal", "-shm"):
            Path(str(temp_db) + suffix).unlink(missing_ok=True)
        if output.exists():
            output.unlink()
        shutil.move(str(temp_db), str(output))
        print(json.dumps(summary, ensure_ascii=False, indent=2))
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
    rebuild(parser.parse_args())


if __name__ == "__main__":
    main()
