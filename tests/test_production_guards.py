import csv
import json
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from src.rebuild_database import selected_hf_snapshot_paths


def write_scan(path: Path, count: int) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["id", "latitude", "longitude", "county", "radius_km"])
        writer.writeheader()
        for index in range(count):
            writer.writerow({"id": index + 1, "latitude": 25, "longitude": 121,
                             "county": f"縣市{index % 22}", "radius_km": 3})


def test_coordinator_rejects_truncated_nationwide_scan(tmp_path):
    scan = tmp_path / "scan.csv"
    write_scan(scan, 1)
    result = subprocess.run([
        sys.executable, "src/taiwan_crawler_coordinator.py", "--scan-file", str(scan),
        "--output-dir", str(tmp_path / "tasks"),
    ], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode != 0
    assert "拒絕假全台批次" in result.stderr
    assert not (tmp_path / "tasks").exists()


def test_pinned_rebuild_uses_only_manifest_archives(tmp_path):
    manifest = tmp_path / "source-manifest.json"
    manifest.write_text(json.dumps({
        "revision": "immutable",
        "raw_count": 1,
        "archives": [{
            "path": "TaiwanMenuSnapshots/20260923063709/taiwan_menus_20260923063709.tar.gz",
            "bytes": 123,
            "sha256": "a" * 64,
        }],
    }), encoding="utf-8")
    args = Namespace(repo_id="ignored", revision="immutable", source_manifest=str(manifest))
    assert selected_hf_snapshot_paths(args, None) == [
        ("20260923063709", "TaiwanMenuSnapshots/20260923063709/taiwan_menus_20260923063709.tar.gz")
    ]


def test_pinned_rebuild_rejects_revision_mismatch(tmp_path):
    manifest = tmp_path / "source-manifest.json"
    manifest.write_text(json.dumps({"revision": "other", "raw_count": 0, "archives": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="revision"):
        selected_hf_snapshot_paths(
            Namespace(repo_id="ignored", revision="immutable", source_manifest=str(manifest)), None
        )
