"""Merge independently verified packed shard databases without expanding rows."""
from __future__ import annotations
import argparse, hashlib, json, sqlite3
from collections import defaultdict
from pathlib import Path
import msgpack, zstandard as zstd
from prototype_pack_db import SCHEMA, packed


def decode(row, dec):
    raw=dec.decompress(row[0])
    if hashlib.sha256(raw).hexdigest()!=row[1]: raise ValueError("packed shard checksum mismatch")
    return msgpack.unpackb(raw,raw=False)


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--shards-dir',required=True); ap.add_argument('--output',required=True); ap.add_argument('--expected-shards',type=int,default=15); a=ap.parse_args()
    files=sorted(Path(a.shards_dir).rglob('packed_*.db'))
    if len(files)!=a.expected_shards: raise SystemExit(f'expected {a.expected_shards} packed shards, found {len(files)}')
    out=Path(a.output); out.unlink(missing_ok=True); dst=sqlite3.connect(out); dst.executescript(SCHEMA)
    dec=zstd.ZstdDecompressor(); comp=zstd.ZstdCompressor(level=10); offsets=[]; next_store=0; bucket_count=None; sources=[]
    batch_agg=defaultdict(lambda:[None,0,0,0,0]); metadata={}; totals=defaultdict(int); max_raw=max_blob=0
    for path in files:
        src=sqlite3.connect(path); sources.append(src); info={r[0]:json.loads(r[1]) for r in src.execute('select key,value from metadata')}
        if bucket_count is None: bucket_count=info['buckets']
        elif bucket_count!=info['buckets']: raise ValueError('bucket count mismatch')
        offset=next_store; offsets.append(offset)
        for row in src.execute('select store_id,store_uuid,name,city,locality,rating,review_count,chunk_count from store_directory order by store_id'):
            dst.execute('insert into store_directory values(?,?,?,?,?,?,?,?)',(row[0]+offset,*row[1:]))
            next_store=max(next_store,row[0]+offset+1)
        for row in src.execute('select store_id,chunk_no,codec,raw_bytes,checksum,payload from store_bundles order by store_id,chunk_no'):
            dst.execute('insert into store_bundles values(?,?,?,?,?,?)',(row[0]+offset,*row[1:])); max_raw=max(max_raw,row[3]); max_blob=max(max_blob,len(row[5]))
        auxrow=src.execute("select payload,checksum from auxiliary_bundles where name='normalized_auxiliary'").fetchone(); aux=decode(auxrow,dec)
        for row in aux['crawl_batches']['rows']:
            agg=batch_agg[row[0]]; agg[0]=row[1]; agg[1]+=row[2]; agg[2]+=row[3]; agg[3]+=row[4]; agg[4]+=row[5]
        for row in aux['metadata']['rows']: metadata.setdefault(row[0],row[1])
        for t,v in info['source_tables'].items(): totals[t]+=v['count']
    for bid in range(bucket_count):
        terms=defaultdict(list)
        for src,offset in zip(sources,offsets):
            row=src.execute('select payload,checksum from search_buckets where bucket_id=?',(bid,)).fetchone(); data=decode(row,dec)
            for term,refs in data.items(): terms[term].extend((((ref>>20)+offset)<<20)|(ref&((1<<20)-1)) for ref in refs)
        raw,blob,digest=packed(dict(terms),comp); dst.execute('insert into search_buckets values(?,?,?,?,?)',(bid,'msgpack+zstd',len(raw),digest,blob))
        if bid%128==0: print(f'merged search buckets {bid:,}/{bucket_count:,}',flush=True)
    auxiliary={'crawl_batches':{'columns':['batch_id','processed_at','stores_seen','products_seen','fallback_stores','fallback_products'],'rows':[[k,*v] for k,v in sorted(batch_agg.items())]},'metadata':{'columns':['key','value'],'rows':[[k,v] for k,v in sorted(metadata.items())]}}
    raw,blob,digest=packed(auxiliary,comp); dst.execute('insert into auxiliary_bundles values(?,?,?,?,?)',('normalized_auxiliary','msgpack+zstd',len(raw),digest,blob))
    chunks=dst.execute('select count(*) from store_bundles').fetchone()[0]
    info={'format':1,'buckets':bucket_count,'chunks':chunks,'products':totals['products'],'max_raw_bundle':max_raw,'max_compressed_bundle':max_blob,'source_counts':dict(totals),'shards':len(files)}
    for k,v in info.items(): dst.execute('insert into metadata values(?,?)',(k,json.dumps(v,ensure_ascii=False,separators=(',',':'))))
    dst.commit(); dst.execute('vacuum')
    # Turso's database-upload endpoint requires WAL mode. There are no pending
    # writes after VACUUM, but checkpoint explicitly so the uploaded main file
    # is complete and does not depend on a sidecar WAL file.
    mode=dst.execute('pragma journal_mode=wal').fetchone()[0]
    if mode.lower()!='wal': raise RuntimeError(f'failed to enable WAL mode: {mode}')
    dst.execute('pragma wal_checkpoint(truncate)')
    integrity=dst.execute('pragma integrity_check').fetchone()[0]
    if integrity!='ok': raise RuntimeError(f'packed database integrity check failed: {integrity}')
    dst.close()
    for src in sources: src.close()
    print(json.dumps({**info,'stores':next_store,'database_bytes':out.stat().st_size,'packed_rows':next_store+chunks+bucket_count+1+len(info)},indent=2))

if __name__=='__main__': main()
