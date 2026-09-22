from argparse import Namespace
import sqlite3
import subprocess
import sys
from pathlib import Path
import pytest
from test_shard_rebuild import write_multi_store_snapshot
from src.rebuild_database import rebuild
from scripts.prototype_pack_db import build, verify
from scripts.verify_packed_local import verify as verify_release


def test_rebuild_pack_merge_and_reject_corrupt_payload(tmp_path):
    raw = tmp_path / 'raw'
    write_multi_store_snapshot(raw, '20260922063749', {})
    for shard in range(2):
        database = tmp_path / f'normalized_{shard}.db'
        rebuild(Namespace(source_dir=str(raw), repo_id=None, database=str(database), replace=False,
                          missing_threshold=3, minimum_stores=1, max_snapshots=None,
                          shard_id=shard, total_shards=2, skip_fts=True, strict=True, revision='fixture'))
        target = tmp_path / f'packed_{shard}.db'
        build(str(database), str(target), bucket_count=32)
        verify(str(database), str(target))
    output = tmp_path / 'merged.db'
    cmd = [sys.executable, 'scripts/merge_packed_shards.py', '--shards-dir', str(tmp_path),
           '--output', str(output), '--expected-shards', '2']
    subprocess.run(cmd, check=True, capture_output=True)
    report = verify_release(output)
    assert report['stores'] == 4
    assert report['products'] == 4
    assert report['metadata']['unique_batches'] == 1
    with sqlite3.connect(output) as c:
        c.execute("update search_buckets set checksum='bad' where bucket_id=0")
    with pytest.raises(ValueError, match='checksum'):
        verify_release(output)
    with sqlite3.connect(tmp_path / 'packed_1.db') as c:
        c.execute("update metadata set value='\"different\"' where key='source_revision'")
    c.close()
    cmd[cmd.index('--output') + 1] = str(tmp_path / 'rejected.db')
    failed = subprocess.run(cmd, capture_output=True)
    assert failed.returncode != 0
    assert b'mixed shard release' in failed.stderr
