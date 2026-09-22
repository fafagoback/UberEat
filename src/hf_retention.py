# -*- coding: utf-8 -*-
"""Strict 60-day retention for raw snapshots and v2 event files only."""
from __future__ import annotations
import argparse, os, re
from datetime import datetime, timedelta, timezone

RAW_PREFIX = "TaiwanMenuSnapshots/"
EVENT_PREFIX = "v2/history/events/"
BATCH_RE = re.compile(r"(?<!\d)(20\d{12})(?!\d)")

def batch_time(path: str):
    m = BATCH_RE.search(path)
    if not m: return None
    try: return datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone(timedelta(hours=8)))
    except ValueError: return None

def retention_plan(paths, now, days=60):
    cutoff = now - timedelta(days=days); delete=[]; keep=[]
    for path in paths:
        if not path.startswith((RAW_PREFIX, EVENT_PREFIX)): continue
        ts=batch_time(path)
        (delete if ts and ts < cutoff else keep).append(path)
    return sorted(delete), sorted(keep)

def main():
    p=argparse.ArgumentParser(); p.add_argument("--repo-id", default=os.getenv("HF_REPO_ID","hub-google/UberEat")); p.add_argument("--days",type=int,default=60); p.add_argument("--apply",action="store_true")
    a=p.parse_args(); token=os.getenv("HF_TOKEN")
    if not token: raise SystemExit("HF_TOKEN is required")
    from huggingface_hub import CommitOperationDelete,HfApi
    api=HfApi(token=token); before=sorted(api.list_repo_files(a.repo_id,repo_type="dataset")); now=datetime.now(timezone.utc)
    delete,keep=retention_plan(before,now,a.days)
    print(f"mode={'APPLY' if a.apply else 'DRY-RUN'} cutoff={now-timedelta(days=a.days):%Y-%m-%dT%H:%M:%SZ} delete={len(delete)} keep={len(keep)}")
    for x in delete: print("DELETE",x)
    if a.apply and delete:
        raise SystemExit("Deletion blocked: no verified durable baseline/recovery manifest is implemented. Keep Raw for rebuild.")
    print(f"oldest_kept={keep[0] if keep else '-'} newest_kept={keep[-1] if keep else '-'} verified={'yes' if a.apply else 'dry-run'}")

if __name__ == "__main__": main()
