# -*- coding: utf-8 -*-
"""UberEat HF: retain latest 60 days of Raw, then permanently squash Git/LFS history.

This targets ONLY hub-google/UberEat (Dataset). It does not touch FoodRadar,
Turso, GitHub Pages, or any other HF repository.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPOSITORY = "hub-google/UberEat"
TW = timezone(timedelta(hours=8))
RAW_BATCH_RE = re.compile(r"^Raw/(\d{14})/(.+)$")
ROOT_FILES = {".gitattributes", "README.md", "Raw/latest.json"}
# Old transformed data must never be published again.
LEGACY_PREFIXES = ("TaiwanMenuSnapshots/", "TaiwanStores/", "v2/history/events/")


def parse_batch(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{14}", value):
        raise ValueError(f"Invalid batch timestamp: {value!r}")
    return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=TW)


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def classify_paths(paths, now, days=60):
    """Fail closed on unknown paths; never delete a recent completed batch."""
    deadline = now.astimezone(TW) - timedelta(days=days)
    remove, keep, batches = [], [], {}
    for path in sorted(paths):
        if path in ROOT_FILES:
            keep.append(path)
            continue
        if path.startswith(LEGACY_PREFIXES):
            remove.append(path)
            continue
        match = RAW_BATCH_RE.fullmatch(path)
        if not match:
            raise ValueError(f"Unexpected HF dataset path (refuse cleanup): {path}")
        batch, member = match.groups()
        date = parse_batch(batch)
        batches.setdefault(batch, []).append(path)
        if date < deadline:
            remove.append(path)
        else:
            keep.append(path)
    return remove, keep, batches


def verify_latest(api, token, paths, cutoff):
    """Protect pointer/complete batch before any irreversible history rewrite."""
    from huggingface_hub import hf_hub_download

    if "Raw/latest.json" not in paths:
        raise ValueError("HF Raw/latest.json missing; refusing destructive cleanup")
    latest_file = hf_hub_download(
        repo_id=REPOSITORY, repo_type="dataset",
        filename="Raw/latest.json", token=token
    )
    latest = json.loads(Path(latest_file).read_text(encoding="utf-8"))
    batch = str(latest.get("batch_id", ""))
    timestamp = parse_batch(batch)
    manifest_path = f"Raw/{batch}/manifest.json"
    if (latest.get("format") != "ubereat-original-http-body-v1"
            or latest.get("complete") is not True
            or latest.get("manifest_path") != manifest_path):
        raise ValueError("Invalid or incomplete nationwide Raw/latest.json")
    if timestamp < cutoff:
        raise ValueError(
            f"Latest verified batch {batch} is older than {cutoff.isoformat()}; "
            "do not destroy the last known complete dataset"
        )
    if manifest_path not in paths:
        raise ValueError(f"Missing latest batch manifest: {manifest_path}")
    file = hf_hub_download(
        repo_id=REPOSITORY, repo_type="dataset",
        filename=manifest_path, token=token
    )
    if sha256_file(file) != latest.get("manifest_sha256"):
        raise ValueError("Latest manifest SHA-256 mismatch")
    manifest = json.loads(Path(file).read_text(encoding="utf-8"))
    if (manifest.get("batch_id") != batch
            or manifest.get("complete") is not True
            or manifest.get("format") != "ubereat-original-http-body-v1"
            or manifest.get("point_count", 0) < 1500
            or manifest.get("store_count", 0) < 10000
            or manifest.get("feed_workers") != 15
            or manifest.get("menu_workers") != 15):
        raise ValueError("Latest nationwide manifest not complete")
    archives = manifest.get("archives")
    if not isinstance(archives, list) or len(archives) != 30:
        raise ValueError("Latest nationwide Raw archive count is not 30")
    archive_paths = [entry.get("remote_path") for entry in archives]
    if len(archive_paths) != len(set(archive_paths)) or any(
        not isinstance(p, str) or not p.startswith(f"Raw/{batch}/")
        or p not in paths for p in archive_paths
    ):
        raise ValueError("Latest Raw archives are missing or duplicated")
    return latest, manifest


def main():
    parser = argparse.ArgumentParser(description="UberEat Hugging Face 60-day Raw retention and Git history squash")
    parser.add_argument("--repo-id", default=REPOSITORY)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--apply", action="store_true", help="Delete expired Raw objects and super-squash HF history")
    args = parser.parse_args()
    if args.repo_id != REPOSITORY:
        parser.error("Refusing to operate on any repository other than hub-google/UberEat")
    if args.days != 60:
        parser.error("Retention period is locked at 60 days for this project")
    token = os.getenv("HF_TOKEN")
    if not token:
        raise SystemExit("HF_TOKEN missing")

    from huggingface_hub import CommitOperationDelete, HfApi
    from huggingface_hub.utils import RepositoryNotFoundError
    api = HfApi(token=token)
    try:
        info = api.repo_info(repo_id=REPOSITORY, repo_type="dataset")
    except RepositoryNotFoundError:
        print("HF dataset does not yet exist; the next completed crawl will create it.")
        return
    if info.id != REPOSITORY:
        raise RuntimeError("Unexpected HF repository identity")

    all_files = sorted(api.list_repo_files(repo_id=REPOSITORY, repo_type="dataset"))
    cutoff = datetime.now(TW) - timedelta(days=60)
    if not any(x.startswith("Raw/") for x in all_files):
        print("No new Raw archive yet; skip cleanup to avoid squashing an incomplete dataset.")
        return

    latest, _ = verify_latest(api, token, set(all_files), cutoff)
    expired, retained, batches = classify_paths(all_files, datetime.now(TW))
    if any(x.startswith(f"Raw/{latest['batch_id']}/") for x in expired):
        raise RuntimeError("Current complete batch would be removed")
    if not any(x.startswith("Raw/") and x != "Raw/latest.json" for x in retained):
        raise RuntimeError("No Raw batches would remain after cleanup")

    print("HF_RETENTION_PLAN=" + json.dumps({
        "repo_id": REPOSITORY, "cutoff": cutoff.isoformat(),
        "latest_batch": latest["batch_id"],
        "expired_files": len(expired), "retained_files": len(retained),
        "batch_count": len(batches), "delete_paths": expired[:50],
        "squash_git_and_lfs_history": args.apply
    }, ensure_ascii=False), flush=True)

    if not args.apply:
        print("DRY_RUN: no files deleted; no Git history rewritten")
        return

    # Explicitly check refs: old commits can stay referenced by branches/PRs.
    refs = api.list_repo_refs(repo_id=REPOSITORY, repo_type="dataset")
    branches = [x.name for x in refs.branches]
    tags = [x.name for x in refs.tags]
    other_refs = [x.name for x in getattr(refs, "converts", [])]
    if branches != ["main"] or tags or other_refs:
        raise RuntimeError(
            "Additional HF refs may pin old LFS history; refuse destructive "
            f"squash until resolved: branches={branches}, tags={tags}, extra={other_refs}"
        )

    # Delete old visible files first, in bounded commits; then remove old
    # LFS/Git versions by super-squashing main. Both operations are necessary.
    for offset in range(0, len(expired), 100):
        chunk = expired[offset:offset + 100]
        api.create_commit(
            repo_id=REPOSITORY, repo_type="dataset",
            operations=[CommitOperationDelete(path_in_repo=p) for p in chunk],
            commit_message=f"Expire UberEat Raw older than 60d ({offset + 1}-{offset + len(chunk)})",
        )
        print(f"HF_EXPIRED_FILES_REMOVED={offset + len(chunk)}/{len(expired)}", flush=True)

    after = sorted(api.list_repo_files(repo_id=REPOSITORY, repo_type="dataset"))
    if set(after) != set(retained):
        raise RuntimeError("HF file verification failed; refusing history squash")
    verify_latest(api, token, set(after), cutoff)

    # HF documents this as irreversible: squashes the entire main history to
    # a single commit so deleted versions no longer retain old LFS objects.
    api.super_squash_history(
        repo_id=REPOSITORY, repo_type="dataset", branch="main",
        commit_message=f"保留 UberEat 最近 60 天 Raw，清除歷史版本"
    )
    final_files = set(api.list_repo_files(repo_id=REPOSITORY, repo_type="dataset"))
    if final_files != set(retained):
        raise RuntimeError("Files changed during HF super-squash")
    verify_latest(api, token, final_files, cutoff)
    commits = list(api.list_repo_commits(repo_id=REPOSITORY, repo_type="dataset", revision="main"))
    if len(commits) != 1:
        raise RuntimeError(f"Git history squash not verified: {len(commits)} commits")
    print("HF_RETENTION_COMPLETE=" + json.dumps({
        "repo": REPOSITORY, "expired_deleted": len(expired),
        "remaining_files": len(final_files), "git_commits": len(commits),
        "latest_batch": latest["batch_id"],
        "lfs_history_squashed": True,
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
