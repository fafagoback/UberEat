# -*- coding: utf-8 -*-
"""Delete obsolete Hugging Face serving formats after v2 is verified.

Safety policy:
- NEVER delete TaiwanMenuSnapshots/** (daily full crawler archives).
- NEVER delete TaiwanStores/** (crawler store discovery source).
- NEVER delete v2/**.
- Keep Parquet/history/** temporarily because the legacy dashboard intelligence
  still reads it until that part is migrated to v2 change events.
- Delete only explicitly allow-listed obsolete serving artifacts.
"""

from __future__ import annotations

import argparse
import os
from typing import Iterable

PRESERVE_PREFIXES = (
    "TaiwanMenuSnapshots/",
    "TaiwanStores/",
    "v2/",
    "Parquet/history/",  # transitional dependency; remove after alert migration
)

DELETE_PREFIXES = (
    "Parquet/partitions/",
)

DELETE_EXACT = {
    "Parquet/taiwan_catalog_latest.parquet",
}


def is_preserved(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in PRESERVE_PREFIXES)


def is_deletable(path: str) -> bool:
    if is_preserved(path):
        return False
    return path in DELETE_EXACT or any(path.startswith(prefix) for prefix in DELETE_PREFIXES)


def chunks(values: list[str], size: int = 80) -> Iterable[list[str]]:
    for i in range(0, len(values), size):
        yield values[i:i + size]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-id", default=os.environ.get("HF_REPO_ID") or "hub-google/UberEat")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN")
    if not token:
        raise SystemExit("HF_TOKEN is required")

    from huggingface_hub import CommitOperationDelete, HfApi
    api = HfApi(token=token)
    files = sorted(api.list_repo_files(repo_id=args.repo_id, repo_type="dataset"))

    # Hard gate: v2 must be published before any legacy deletion.
    required = {"v2/current/manifest.json", "v2/search/manifest.json"}
    missing = sorted(required - set(files))
    if missing:
        raise SystemExit(f"Refusing cleanup: v2 validation files missing: {missing}")

    deletable = [path for path in files if is_deletable(path)]
    preserved_daily = [p for p in files if p.startswith("TaiwanMenuSnapshots/")]
    preserved_stores = [p for p in files if p.startswith("TaiwanStores/")]
    preserved_history = [p for p in files if p.startswith("Parquet/history/")]

    print(f"HF repo: {args.repo_id}")
    print(f"Daily snapshots preserved: {len(preserved_daily)}")
    print(f"TaiwanStores preserved: {len(preserved_stores)}")
    print(f"Transitional Parquet history preserved: {len(preserved_history)}")
    print(f"Obsolete serving files selected for deletion: {len(deletable)}")
    for path in deletable:
        print(f"DELETE {path}")

    if args.dry_run or not deletable:
        return

    for index, batch in enumerate(chunks(deletable), start=1):
        operations = [CommitOperationDelete(path_in_repo=path) for path in batch]
        api.create_commit(
            repo_id=args.repo_id,
            repo_type="dataset",
            operations=operations,
            commit_message=f"cleanup obsolete v7 serving artifacts (chunk {index})",
        )

    after = set(api.list_repo_files(repo_id=args.repo_id, repo_type="dataset"))
    survivors = sorted(path for path in deletable if path in after)
    if survivors:
        raise SystemExit(f"Cleanup verification failed; survivors: {survivors[:20]}")
    if not any(p.startswith("TaiwanMenuSnapshots/") for p in after) and preserved_daily:
        raise SystemExit("Safety verification failed: daily snapshots unexpectedly disappeared")
    print("HF legacy serving cleanup verified successfully")


if __name__ == "__main__":
    main()
