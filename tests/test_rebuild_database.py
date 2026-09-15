import io
import json
import sqlite3
import tarfile
from argparse import Namespace

from src.rebuild_database import rebuild
from src.sync_database_from_hf import sync

S = "11111111-1111-4111-8111-111111111111"
P = "22222222-2222-4222-8222-222222222222"


def write_snapshot(root, batch, price):
    path = root / batch / f"taiwan_menus_{batch}.tar.gz"
    path.parent.mkdir(parents=True)
    doc = {
        "store_uuid": S,
        "name": "COSTCO",
        "hasMenu": {"hasMenuSection": [{"name": "食品", "hasMenuItem": [{
            "identifier": P, "name": "牛肉", "offers": {"price": str(price)}
        }]}]},
    }
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in (("manifest.json", {"batch_id": batch, "store_count": 1}), (f"Json/{S}.json", doc)):
            raw = json.dumps(payload).encode()
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))


def test_rebuild_replays_every_snapshot_and_skips_manifest(tmp_path):
    prices = [100, 1, 100, 1, 80]
    for day, price in enumerate(prices, 1):
        write_snapshot(tmp_path / "snapshots", f"202609{day:02d}120000", price)
    output = tmp_path / "rebuilt.db"
    summary = rebuild(Namespace(
        source_dir=str(tmp_path / "snapshots"), repo_id=None, database=str(output),
        replace=False, missing_threshold=3, minimum_stores=1, max_snapshots=None,
    ))
    assert summary["snapshots"] == 5
    assert summary["stores"] == 1
    assert summary["products"] == 1
    conn = sqlite3.connect(output)
    assert conn.execute("select first_seen from products").fetchone()[0].startswith("2026-09-01")
    price_events = conn.execute("select event_time from events where event_type='PRICE_CHANGED'").fetchall()
    assert len(price_events) == 1
    assert price_events[0][0].startswith("2026-09-05")
    assert conn.execute("select count(*) from stores where store_uuid like 'fallback:%'").fetchone()[0] == 0


def test_sync_catches_up_every_snapshot_after_latest_batch(tmp_path):
    source = tmp_path / "snapshots"
    for day, price in enumerate([100, 100, 100], 1):
        write_snapshot(source, f"202609{day:02d}120000", price)
    output = tmp_path / "serving.db"
    base = Namespace(source_dir=str(source), repo_id=None, database=str(output), replace=False,
                     missing_threshold=3, minimum_stores=1, max_snapshots=None)
    rebuild(base)
    write_snapshot(source, "20260904120000", 80)
    write_snapshot(source, "20260905120000", 70)
    result = sync(Namespace(source_dir=str(source), repo_id=None, database=str(output),
                            missing_threshold=3, minimum_stores=1))
    assert result["processed"] == ["20260904120000", "20260905120000"]
    conn = sqlite3.connect(output)
    assert conn.execute("select value from metadata where key='latest_batch'").fetchone()[0] == "20260905120000"
    assert conn.execute("select count(*) from crawl_batches").fetchone()[0] == 5


def test_rebuild_skips_archive_with_corrupt_store_json(tmp_path):
    source = tmp_path / "snapshots"
    valid_batch = "20260901120000"
    corrupt_batch = "20260902120000"
    write_snapshot(source, valid_batch, 100)
    corrupt = source / corrupt_batch / f"taiwan_menus_{corrupt_batch}.tar.gz"
    corrupt.parent.mkdir(parents=True)
    with tarfile.open(corrupt, "w:gz") as archive:
        manifest = json.dumps({"batch_id": corrupt_batch, "store_count": 1}).encode()
        info = tarfile.TarInfo("manifest.json"); info.size = len(manifest)
        archive.addfile(info, io.BytesIO(manifest))
        payload = b"{not valid json"
        info = tarfile.TarInfo(f"Json/{S}.json"); info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    output = tmp_path / "serving.db"
    summary = rebuild(Namespace(
        source_dir=str(source), repo_id=None, database=str(output), replace=False,
        missing_threshold=3, minimum_stores=1, max_snapshots=None,
    ))
    assert summary["snapshots"] == 1
    assert summary["skipped"][0]["batch_id"] == corrupt_batch
