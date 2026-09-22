"""Make packed store IDs stable against the currently published Turso database."""
from __future__ import annotations
import argparse, hashlib, json, sqlite3
from pathlib import Path
import msgpack
import zstandard as zstd
from prototype_pack_db import SCHEMA, packed

def decode(row, dec):
    raw = dec.decompress(row[0])
    if hashlib.sha256(raw).hexdigest() != row[1]:
        raise ValueError("packed checksum mismatch")
    return msgpack.unpackb(raw, raw=False)

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("mapping")
    ap.add_argument("--output", required=True)
    args = ap.parse_args()
    mapping = {str(k): int(v) for k, v in json.loads(Path(args.mapping).read_text(encoding="utf-8")).items()}
    src = sqlite3.connect(args.source); src.row_factory = sqlite3.Row
    out = Path(args.output); out.unlink(missing_ok=True)
    dst = sqlite3.connect(out); dst.executescript(SCHEMA)
    for definition in ("latitude REAL", "longitude REAL", "address TEXT", "order_url TEXT", "first_seen TEXT", "last_seen TEXT", "status TEXT", "is_open INTEGER"):
        dst.execute(f"alter table store_directory add column {definition}")
    local_to_stable = {}; next_id = max(mapping.values(), default=-1) + 1
    for row in src.execute("select * from store_directory order by store_id"):
        old_id = int(row["store_id"]); stable_id = mapping.get(str(row["store_uuid"]))
        if stable_id is None: stable_id, next_id = next_id, next_id + 1
        local_to_stable[old_id] = stable_id
        values = list(row); values[0] = stable_id
        dst.execute(f"insert into store_directory values({','.join('?' for _ in values)})", values)
    for row in src.execute("select store_id,chunk_no,codec,raw_bytes,checksum,payload from store_bundles order by store_id,chunk_no"):
        dst.execute("insert into store_bundles values(?,?,?,?,?,?)", (local_to_stable[int(row[0])], *row[1:]))
    dec = zstd.ZstdDecompressor(); comp = zstd.ZstdCompressor(level=10)
    for row in src.execute("select bucket_id,codec,raw_bytes,checksum,payload from search_buckets order by bucket_id"):
        data = decode((row[4], row[3]), dec)
        remapped = {term: [((local_to_stable[int(ref) >> 20] << 20) | (int(ref) & ((1 << 20) - 1))) for ref in refs] for term, refs in data.items()}
        raw, blob, digest = packed(remapped, comp)
        dst.execute("insert into search_buckets values(?,?,?,?,?)", (row[0], "msgpack+zstd", len(raw), digest, blob))
    for row in src.execute("select name,codec,raw_bytes,checksum,payload from auxiliary_bundles"):
        dst.execute("insert into auxiliary_bundles values(?,?,?,?,?)", tuple(row))
    for row in src.execute("select key,value from metadata"):
        dst.execute("insert into metadata values(?,?)", tuple(row))
    dst.commit(); dst.execute("pragma journal_mode=wal"); dst.execute("pragma wal_checkpoint(truncate)")
    if dst.execute("pragma integrity_check").fetchone()[0] != "ok": raise RuntimeError("rekeyed packed DB failed integrity check")
    dst.close(); src.close()
    print(json.dumps({"stores": len(local_to_stable), "new_store_ids": max(0, next_id - max(mapping.values(), default=-1) - 1), "bytes": out.stat().st_size}))

if __name__ == "__main__": main()
