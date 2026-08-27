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
from snapshot_validation import validate_snapshot, archive_member

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
    parser.add_argument("--offline", action="store_true", help="Validate and package without uploading")
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
    try:
        valid_files = validate_snapshot(args.src_dir, stores, batch_id)
    except ValueError as exc:
        fail(batch_id, str(exc), rows)
    rows.append(f"| 身分、批次與 Schema | 指派集合完全相同 | `{len(valid_files)}` 個唯一店家 | ✅ |")

    os.makedirs(args.output_dir, exist_ok=True)
    archive_name = f"taiwan_menus_{batch_id}.tar.gz"
    archive_path = os.path.join(args.output_dir, archive_name)

    inactive_stores = []
    for path in valid_files:
        try:
            with open(path, encoding="utf-8") as handle:
                doc = json.load(handle)
                if doc.get("menu_status") == "inactive_account":
                    inactive_stores.append(doc.get("name", "未命名店家"))
        except Exception:
            pass

    inactive_count = len(inactive_stores)
    active_count = expected - inactive_count
    inactive_rate = (inactive_count / max(1, expected)) * 100.0

    inactive_ok = inactive_rate < 10.0
    rows.append(f"| 全台失效店家率 | 失效比例 < 10.0% | 正常: `{active_count:,}` 間, 失效: `{inactive_count:,}` 間 (`{inactive_rate:.2f}%`) | {'✅' if inactive_ok else '❌'} |")
    if not inactive_ok:
        fail(batch_id, f"全台網頁已失效店家比例達 {inactive_rate:.2f}% (超過 10% 門檻)", rows)

    manifest = {
        "batch_id": batch_id,
        "source_run_id": os.environ.get("GITHUB_RUN_ID", "local"),
        "created_at": datetime.now(TW_TZ).isoformat(),
        "worker_artifacts": args.actual_workers,
        "store_count": expected,
        "active_store_count": active_count,
        "inactive_store_count": inactive_count,
        "format": "Schema.org Restaurant JSON files",
    }
    manifest_path = os.path.join(args.output_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(manifest_path, arcname="manifest.json")
        for path in valid_files:
            tar.add(path, arcname=archive_member(path, batch_id))

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

    if args.offline:
        print(f"Validated offline snapshot: {archive_path} ({digest})")
        return

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
    inactive_section_md = ""
    if inactive_stores:
        items_md = "\n".join([f"- `{name}`" for name in inactive_stores])
        inactive_section_md = f"\n\n<details>\n<summary><b>⚠️ 全台網頁已失效店家名單 (共 {inactive_count} 間)</b></summary>\n\n{items_md}\n\n</details>"

    append_summary(
        f"## ✅ Stage 5 菜單快照封存完成\n"
        f"> 批次：`{batch_id}`｜總店家：`{expected:,}` (正常: `{active_count:,}` / 失效: `{inactive_count:,}`)｜壓縮檔：`{archive_name}`｜耗時：`{elapsed:.1f}` 秒\n\n"
        "| 檢核項目 | 通過標準 | 實際結果 | 狀態 |\n"
        "| :--- | :--- | :--- | :---: |\n" + "\n".join(rows) +
        "\n| 最終結果 | 所有 checkpoint 通過 | HF 單一檔案已完成一次 Commit | ✅ |"
        + inactive_section_md
    )
    print(f"✅ Stage 5 完成：{remote_path} ({digest})")


if __name__ == "__main__":
    main()
