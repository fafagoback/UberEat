"""Nationwide getStoreV1 HTTP-body capture, following FoodRadar Raw semantics.

One assigned store produces one logical result and all received HTTP responses,
including failed attempts. No Schema.org conversion, price calculation or Turso.
"""
import argparse
import json
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from location_batch_scraper import normalize_store_uuid

TW = timezone(timedelta(hours=8))


def attach_delivery_location(session, store):
    """Use a real point which discovered the store, as in FoodRadar."""
    points = store.get("discovered_points") or []
    if not points or not isinstance(points[0], dict):
        raise ValueError("missing discovery coordinates")
    point = points[0]
    latitude, longitude = float(point["lat"]), float(point["lon"])
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("invalid discovery coordinates")
    county = str(point.get("county") or "台灣")
    reference = f"{county}座標點#{point.get('point_id', 'unknown')}"
    cookie = {
        "address": {
            "address1": reference, "address2": "", "aptOrSuite": "",
            "city": county, "country": "TW", "postalCode": "100", "region": ""
        },
        "latitude": latitude,
        "longitude": longitude,
        "reference": reference,
        "referenceType": "google_places",
        "type": "google_places"
    }
    session.cookies.set(
        "uev2.loc",
        urllib.parse.quote(json.dumps(cookie, ensure_ascii=False)),
        domain=".ubereats.com"
    )
    return {"point_id": point.get("point_id"), "lat": latitude, "lon": longitude,
            "county": county}


def capture_store(store, output, max_retries=5):
    identity = normalize_store_uuid(store.get("store_uuid"), store.get("store_url", ""))
    item = {
        "store_uuid": identity,
        "store_url": store.get("store_url"),
        "store_name": store.get("name"),
        "success": False,
        "inactive": False,
        "attempts": [],
    }
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", identity):
        item["error"] = "invalid store UUID"
        return item
    folder = output / "stores" / identity
    folder.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8",
        "x-csrf-token": "x",
        "Content-Type": "application/json",
    }
    try:
        item["delivery_point"] = attach_delivery_location(session, store)
        for attempt in range(1, max_retries + 1):
            try:
                response = session.post(
                    "https://www.ubereats.com/api/getStoreV1",
                    json={"storeUuid": identity, "sfNuggetCount": 24},
                    headers=headers, timeout=12
                )
                filename = f"attempt-{attempt:02d}-status-{response.status_code}.body"
                relative = f"stores/{identity}/{filename}"
                path = output / relative
                # Preserve response.content exactly BEFORE decoding or interpreting it.
                if path.exists():
                    raise FileExistsError(f"Refusing to overwrite {path}")
                path.write_bytes(response.content)
                meta = {
                    "path": relative, "http_status": response.status_code,
                    "captured_at": datetime.now(TW).isoformat(),
                    "bytes": len(response.content)
                }
                item["attempts"].append(meta)
                if response.status_code in (404, 410):
                    item.update(success=True, inactive=True, response_path=relative)
                    break
                if response.status_code == 200:
                    payload = response.json()
                    data = payload.get("data") if isinstance(payload, dict) else None
                    if isinstance(payload, dict) and payload.get("status") == "failure":
                        code = str(data.get("code", "")) if isinstance(data, dict) else ""
                        message = str(data.get("message", "")).lower() if isinstance(data, dict) else ""
                        if code in ("404", "410") or "inactive_account" in message or "store_inactive" in message:
                            item.update(success=True, inactive=True, response_path=relative)
                            break
                    if (isinstance(data, dict) and
                            (data.get("title") or data.get("catalogSectionsMap")) and
                            payload.get("status") in (None, "success")):
                        item.update(success=True, inactive=False, response_path=relative)
                        break
                item["error"] = f"HTTP {response.status_code} or incomplete response"
            except (requests.RequestException, ValueError, TypeError) as exc:
                item["error"] = f"{type(exc).__name__}: {exc}"
            if attempt < max_retries:
                time.sleep(min(20.0, attempt * 2.0))
    except (ValueError, KeyError, TypeError) as exc:
        item["error"] = str(exc)
    finally:
        session.close()
    return item


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunk-file", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--concurrency", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 10:
        parser.error("concurrency must be 1..10")
    task = json.loads(Path(args.chunk_file).read_text(encoding="utf-8"))
    stores = task.get("stores") or []
    if not stores or len(stores) != task.get("stores_count") or len(stores) != len({
        s.get("store_uuid") or s.get("store_url") for s in stores
    }):
        raise ValueError("assigned store list is empty, incomplete or duplicated")
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    responses = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(capture_store, store, output): store for store in stores}
        for future in as_completed(futures):
            store = futures[future]
            try:
                record = future.result()
            except Exception as exc:
                record = {
                    "store_uuid": store.get("store_uuid"),
                    "store_url": store.get("store_url"),
                    "success": False, "attempts": [],
                    "error": f"{type(exc).__name__}: {exc}"
                }
            responses.append(record)
            if len(responses) % 100 == 0:
                print(f"Worker {task['chunk_id']}: {len(responses)}/{len(stores)}", flush=True)
    responses.sort(key=lambda x: str(x.get("store_uuid")))
    index = {
        "format": "ubereat-original-http-body-v1",
        "batch_id": task["batch_id"],
        "chunk_id": task["chunk_id"],
        "assigned_stores": len(stores),
        "captured_stores": len(responses),
        "successful_stores": sum(bool(x.get("success")) for x in responses),
        "stores": responses
    }
    (output / "capture_index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = [x for x in responses if not x.get("success")]
    print("RAW_STORE_RESULT=" + json.dumps({
        "chunk": task["chunk_id"], "assigned": len(stores),
        "successful": len(responses) - len(failed),
        "inactive": sum(bool(x.get("inactive")) for x in responses),
        "failed": len(failed),
        "http_responses": sum(len(x.get("attempts", [])) for x in responses),
    }, ensure_ascii=False), flush=True)
    # Raw files remain in the Actions Artifact even if completeness validation fails.
    if failed:
        print(f"ERROR: {len(failed)} failed stores. Examples: {failed[:3]}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
