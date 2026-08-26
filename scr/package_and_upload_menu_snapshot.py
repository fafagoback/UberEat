# -*- coding: utf-8 -*-
"""Validate a complete menu batch, create one archive, and upload it in one HF commit."""

import argparse
import glob
import hashlib
import json
import os
import sys
import tarfile
import time
from datetime import datetime, timedelta, timezone

TW_TZ = timezone(timedelta(hours=8))


def append_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fail(batch_id: str, reason: str, rows: list[str]) -> None:
    rows.append(f"| 最終結果 | 所有 checkpoint 通過 | {reason} | ❌ |")
    append_summary(
        f"## ❌ Stage 5 菜單快照封存失敗\n"
        f"> 批次：`{batch_id}`｜原因：`{reason}`\n\n"
        "| 檢核項目 | 通過標準 | 實際結果 | 狀態 |\n"
        "| :--- | :--- | :--- | :---: |\n" + "\n".join(rows)
    )
    raise SystemExit(reason)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src-dir", required=True)
    parser.add_argument("--stores-file", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--expected-workers", type=int, required=True)
    parser.add_argument("--actual-workers", type=int, required=True)
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--path-in-repo", default="TaiwanMenuSnapshots")
    args = parser.parse_args()

    started = time.time()
    rows: list[str] = []
    batch_id = args.batch_id

    workers_ok = args.actual_workers == args.expected_workers
    rows.append(f"| Worker Artifacts | `{args.expected_workers}` 份 | `{args.actual_workers}` 份 | {'✅' if workers_ok else '❌'} |")
    if not workers_ok:
        fail(batch_id, "Worker Artifact 數量不完整", rows)

    with open(args.stores_file, encoding="utf-8") as fh:
        stores = json.load(fh)
    expected = len(stores)
    files = sorted(glob.glob(os.path.join(args.src_dir, "**", "*.json"), recursive=True))
    valid_files: list[str] = []
    store_keys: list[str] = []
    invalid = 0
    for path in files:
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            if not isinstance(doc, dict) or not doc.get("name"):
                raise ValueError("missing name")
            valid_files.append(path)
            store_keys.append(str(doc.get("store_id") or doc.get("identifier") or os.path.basename(path).split("_", 2)[1]))
        except Exception:
            invalid += 1

    complete = len(valid_files) == expected and invalid == 0
    rows.append(f"| JSON 完整性 | `{expected}` 個有效、0 個無效 | `{len(valid_files)}` 個有效、`{invalid}` 個無效 | {'✅' if complete else '❌'} |")
    if not complete:
        fail(batch_id, "菜單 JSON 未達 100% 完整", rows)

    duplicate_count = len(store_keys) - len(set(store_keys))
    rows.append(f"| 店家主鍵唯一性 | 0 筆重複 | `{duplicate_count}` 筆重複 | {'✅' if duplicate_count == 0 else '❌'} |")
    if duplicate_count:
        fail(batch_id, "菜單成果含重複店家", rows)

    os.makedirs(args.output_dir, exist_ok=True)
    archive_name = f"taiwan_menus_{batch_id}.tar.gz"
    archive_path = os.path.join(args.output_dir, archive_name)
    manifest = {
        "batch_id": batch_id,
        "source_run_id": os.environ.get("GITHUB_RUN_ID", "local"),
        "created_at": datetime.now(TW_TZ).isoformat(),
        "worker_artifacts": args.actual_workers,
        "store_count": expected,
        "format": "Schema.org Restaurant JSON files",
    }
    manifest_path = os.path.join(args.output_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(manifest_path, arcname="manifest.json")
        for path in valid_files:
            tar.add(path, arcname=f"Json/{os.path.basename(path)}")

    with tarfile.open(archive_path, "r:gz") as tar:
        members = tar.getmembers()
    archive_json_count = sum(1 for member in members if member.name.startswith("Json/") and member.name.endswith(".json"))
    archive_ok = archive_json_count == expected
    digest = sha256_file(archive_path)
    size_mb = os.path.getsize(archive_path) / 1024 / 1024
    rows.append(f"| 壓縮檔回讀 | manifest + `{expected}` 個 JSON | `{archive_json_count}` 個 JSON，`{size_mb:.1f} MB` | {'✅' if archive_ok else '❌'} |")
    rows.append(f"| SHA-256 | 64 字元 digest | `{digest}` | ✅ |")
    if not archive_ok:
        fail(batch_id, "壓縮檔回讀數量不一致", rows)

    token = os.environ.get("HF_TOKEN")
    if not token:
        fail(batch_id, "缺少 HF_TOKEN", rows)

    from huggingface_hub import HfApi

    api = HfApi(token=token)
    api.create_repo(repo_id=args.repo_id, repo_type="dataset", exist_ok=True)
    remote_path = f"{args.path_in_repo.strip('/')}/{batch_id}/{archive_name}"
    api.upload_file(
        path_or_fileobj=archive_path,
        path_in_repo=remote_path,
        repo_id=args.repo_id,
        repo_type="dataset",
        commit_message=f"Archive Taiwan menu snapshot {batch_id} ({expected} stores, sha256 {digest[:12]})",
    )
    remote_files = set(api.list_repo_files(repo_id=args.repo_id, repo_type="dataset"))
    remote_ok = remote_path in remote_files
    rows.append(f"| HF 單檔 Commit | 遠端存在 `{remote_path}` | `{'已確認' if remote_ok else '未找到'}` | {'✅' if remote_ok else '❌'} |")
    if not remote_ok:
        fail(batch_id, "HF Commit 後遠端回查失敗", rows)

    elapsed = time.time() - started
    append_summary(
        f"## ✅ Stage 5 菜單快照封存完成\n"
        f"> 批次：`{batch_id}`｜JSON：`{expected:,}`｜壓縮檔：`{archive_name}`｜耗時：`{elapsed:.1f}` 秒\n\n"
        "| 檢核項目 | 通過標準 | 實際結果 | 狀態 |\n"
        "| :--- | :--- | :--- | :---: |\n" + "\n".join(rows) +
        "\n| 最終結果 | 所有 checkpoint 通過 | HF 單一檔案已完成一次 Commit | ✅ |"
    )
    print(f"✅ Stage 5 完成：{remote_path} ({digest})")


if __name__ == "__main__":
    main()
