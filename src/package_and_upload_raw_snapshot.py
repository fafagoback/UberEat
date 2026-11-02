"""Validate nationwide raw HTTP captures, then publish only to hub-google/UberEat/Raw.

Feed and Store response.content bytes are never transformed. A complete batch is
visible through Raw/latest.json only after all parts have been uploaded,
downloaded and SHA-256 verified. No Turso or FoodRadar access.
"""
import argparse
import csv
import hashlib
import json
import os
import re
import tarfile
from pathlib import Path

HF_REPO = "hub-google/UberEat"
RAW_FORMAT = "ubereat-original-http-body-v1"


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for data in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(data)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def checked_part_dir(base, prefix, i):
    part = Path(base) / f"{prefix}-{i}"
    if not part.is_dir():
        raise ValueError(f"Missing worker artifact {part}")
    return part


def capture_entry(path, root):
    path = Path(path)
    if not path.is_file() or path.is_symlink() or path.suffix != ".body":
        raise ValueError(f"Not a raw HTTP body: {path}")
    relative = path.relative_to(root).as_posix()
    if path.stat().st_size == 0:
        raise ValueError(f"Empty HTTP response body: {path}")
    return {
        "path": relative,
        "size": path.stat().st_size,
        "sha256": digest(path),
    }


def collect_feed(feed_dir, scan_file, expected_workers, batch):
    point_ids = []
    with open(scan_file, encoding="utf-8-sig", newline="") as fh:
        for point in csv.DictReader(fh):
            point_ids.append(int(point["id"]))
    if len(point_ids) < 1500 or len(point_ids) != len(set(point_ids)):
        raise ValueError("Invalid nationwide point seed count or duplicate point IDs")

    parts = []
    all_points = []
    for i in range(expected_workers):
        root = checked_part_dir(feed_dir, "store-chunk-result", i)
        chunk = read_json(root / f"stores_chunk_{i}.json")
        if chunk.get("batch_id") != batch or chunk.get("chunk_id") != i:
            raise ValueError(f"Feed worker {i}: batch/chunk mismatch")
        assigned = chunk.get("point_ids")
        if not isinstance(assigned, list) or len(assigned) != chunk.get("points_scanned"):
            raise ValueError(f"Feed worker {i}: incomplete point checkpoint")
        all_points.extend(assigned)
        responses = []
        for point_id in assigned:
            folder = root / "raw_feed" / f"point_{int(point_id)}"
            bodies = sorted(folder.glob("*.body"))
            if not bodies:
                raise ValueError(f"Feed point {point_id}: no HTTP bodies")
            for body in bodies:
                response = capture_entry(body, root)
                response["point_id"] = int(point_id)
                responses.append(response)
        if not responses:
            raise ValueError(f"Feed worker {i}: no raw HTTP responses")
        parts.append({"kind": "feed", "worker": i, "root": root,
                      "point_ids": assigned, "responses": responses})
    if sorted(all_points) != sorted(point_ids):
        raise ValueError(f"Nationwide feed coverage mismatch {len(all_points)}/{len(point_ids)}")
    return parts, len(point_ids)


def collect_stores(menu_dir, stores_file, expected_workers, batch):
    source = read_json(stores_file)
    if not isinstance(source, list) or len(source) < 10000:
        raise ValueError("Nationwide store input missing or below 10,000 safety floor")
    expected = [str(store.get("store_uuid", "")).lower() for store in source]
    if len(expected) != len(set(expected)) or not all(re.fullmatch(
            r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", key) for key in expected):
        raise ValueError("Store UUIDs must be complete, valid and unique")

    parts = []
    assigned = []
    for i in range(expected_workers):
        root = checked_part_dir(menu_dir, "menu-result-chunk", i)
        index = read_json(root / "capture_index.json")
        if (index.get("format") != RAW_FORMAT or index.get("batch_id") != batch
                or index.get("chunk_id") != i):
            raise ValueError(f"Menu worker {i}: index format/batch/chunk mismatch")
        stores = index.get("stores") or []
        if not stores or len(stores) != index.get("assigned_stores"):
            raise ValueError(f"Menu worker {i}: missing assigned stores")
        if index.get("successful_stores") != len(stores):
            raise ValueError(f"Menu worker {i}: not all stores succeeded")
        responses = []
        for store in stores:
            if not store.get("success"):
                raise ValueError(f"Menu worker {i}: incomplete store {store.get('store_uuid')}")
            uuid = str(store.get("store_uuid", "")).lower()
            if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", uuid):
                raise ValueError(f"Menu worker {i}: invalid store UUID")
            assigned.append(uuid)
            attempts = store.get("attempts") or []
            if not attempts or store.get("response_path") not in [a.get("path") for a in attempts]:
                raise ValueError(f"Menu worker {i}: final response missing for {uuid}")
            for attempt in attempts:
                relative = attempt["path"]
                if not relative.startswith(f"stores/{uuid}/") or ".." in Path(relative).parts:
                    raise ValueError(f"Menu worker {i}: invalid raw file path")
                raw = root / relative
                entry = capture_entry(raw, root)
                if entry["size"] != attempt["bytes"]:
                    raise ValueError(f"Menu worker {i}: response byte count mismatch")
                entry["store_uuid"] = uuid
                entry["http_status"] = attempt["http_status"]
                entry["captured_at"] = attempt["captured_at"]
                responses.append(entry)
        parts.append({"kind": "store", "worker": i, "root": root,
                      "store_count": len(stores), "responses": responses})
    if sorted(expected) != sorted(assigned):
        missing = sorted(set(expected) - set(assigned))[:3]
        raise ValueError(f"Nationwide store set mismatch: {len(assigned)} / {len(expected)}, missing {missing}")
    return parts, len(expected)


def make_archive(part, output, batch):
    kind, worker = part["kind"], part["worker"]
    name = f"{kind}-worker-{worker:02d}.tar.gz"
    archive_path = output / name
    with tarfile.open(archive_path, "w:gz") as archive:
        for record in part["responses"]:
            file = part["root"] / record["path"]
            archive.add(file, arcname=record["path"], recursive=False)
    expected = {r["path"]: r["sha256"] for r in part["responses"]}
    if len(expected) != len(part["responses"]):
        raise ValueError(f"Duplicate raw response paths in {name}")
    with tarfile.open(archive_path, "r:gz") as archive:
        members = {m.name: m for m in archive if m.isfile()}
        if set(members) != set(expected):
            raise ValueError(f"Archive members mismatch: {name}")
        for filename, expected_digest in expected.items():
            stream = archive.extractfile(members[filename])
            h = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(block)
            if h.hexdigest() != expected_digest:
                raise ValueError(f"Raw HTTP response changed in archive: {filename}")
    remote = f"Raw/{batch}/{name}"
    return {
        "kind": kind, "worker": worker,
        "remote_path": remote, "local_path": str(archive_path),
        "archive_sha256": digest(archive_path),
        "archive_bytes": archive_path.stat().st_size,
        "response_count": len(part["responses"]),
        "point_ids": part.get("point_ids", []),
        "store_count": part.get("store_count", 0),
        "responses": part["responses"],
    }


def upload_and_verify(api, token, part):
    from huggingface_hub import CommitOperationAdd, hf_hub_download
    api.create_commit(
        repo_id=HF_REPO, repo_type="dataset",
        operations=[CommitOperationAdd(path_in_repo=part["remote_path"],
                                       path_or_fileobj=part["local_path"])],
        commit_message=f"Capture immutable nationwide {part['kind']} worker {part['worker']:02d}",
    )
    remote_file = hf_hub_download(repo_id=HF_REPO, repo_type="dataset",
                                  filename=part["remote_path"], token=token)
    if digest(remote_file) != part["archive_sha256"]:
        raise RuntimeError(f"HF upload checksum mismatch for {part['remote_path']}")


def publish(args):
    from huggingface_hub import CommitOperationAdd, HfApi, hf_hub_download

    if args.repo_id != HF_REPO:
        raise ValueError("UberEat must only write to independent hub-google/UberEat")
    if not re.fullmatch(r"\d{14}", args.batch_id):
        raise ValueError("Invalid batch ID")
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise ValueError("Missing HF_TOKEN secret")
    feed_parts, point_count = collect_feed(
        args.feed_dir, args.scan_file, args.expected_feed_workers, args.batch_id)
    store_parts, store_count = collect_stores(
        args.menu_dir, args.stores_file, args.expected_menu_workers, args.batch_id)
    parts = feed_parts + store_parts
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    published = [make_archive(part, output, args.batch_id) for part in parts]
    manifest = {
        "format": RAW_FORMAT,
        "batch_id": args.batch_id,
        "complete": True,
        "source": "Uber Eats getFeedV1 and getStoreV1 original response.content",
        "point_count": point_count,
        "store_count": store_count,
        "feed_workers": args.expected_feed_workers,
        "menu_workers": args.expected_menu_workers,
        "response_count": sum(p["response_count"] for p in published),
        "archives": [{k: v for k, v in p.items() if k != "local_path"} for p in published],
    }
    path = output / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")

    # Recreate the removed HF dataset; unrelated FoodRadar dataset is never touched.
    api = HfApi(token=token)
    api.create_repo(repo_id=HF_REPO, repo_type="dataset", exist_ok=True)
    for i, part in enumerate(published, 1):
        upload_and_verify(api, token, part)
        print(f"HF_RAW_VERIFIED {i}/{len(published)} {part['remote_path']} "
              f"{part['response_count']} responses", flush=True)
    remote_manifest = f"Raw/{args.batch_id}/manifest.json"
    api.create_commit(
        repo_id=HF_REPO, repo_type="dataset",
        operations=[CommitOperationAdd(path_in_repo=remote_manifest,
                                       path_or_fileobj=str(path))],
        commit_message=f"Publish verified nationwide Raw manifest {args.batch_id}",
    )
    saved = hf_hub_download(repo_id=HF_REPO, repo_type="dataset",
                            filename=remote_manifest, token=token)
    if digest(saved) != digest(path):
        raise RuntimeError("HF manifest verification failed; latest not updated")
    # Latest pointer is promoted ONLY after ALL 30 archives and manifest verified.
    latest = {
        "format": RAW_FORMAT, "batch_id": args.batch_id,
        "complete": True, "manifest_path": remote_manifest,
        "manifest_sha256": digest(path), "point_count": point_count,
        "store_count": store_count
    }
    latest_path = output / "latest.json"
    latest_path.write_text(json.dumps(latest, ensure_ascii=False), encoding="utf-8")
    api.create_commit(
        repo_id=HF_REPO, repo_type="dataset",
        operations=[CommitOperationAdd(path_in_repo="Raw/latest.json",
                                       path_or_fileobj=str(latest_path))],
        commit_message=f"Promote verified nationwide Raw batch {args.batch_id}",
    )
    downloaded_latest = hf_hub_download(repo_id=HF_REPO, repo_type="dataset",
                                        filename="Raw/latest.json", token=token)
    if digest(downloaded_latest) != digest(latest_path):
        raise RuntimeError("HF latest pointer verification failed")
    print("UBEREAT_HF_RAW_COMPLETE=" + json.dumps({
        "repo": HF_REPO, "batch": args.batch_id, "points": point_count,
        "stores": store_count, "archives": len(published),
        "responses": manifest["response_count"], "manifest": remote_manifest
    }, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--feed-dir", required=True)
    parser.add_argument("--menu-dir", required=True)
    parser.add_argument("--scan-file", required=True)
    parser.add_argument("--stores-file", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--repo-id", default=HF_REPO)
    parser.add_argument("--expected-feed-workers", type=int, required=True)
    parser.add_argument("--expected-menu-workers", type=int, required=True)
    args = parser.parse_args()
    publish(args)


if __name__ == "__main__":
    main()
