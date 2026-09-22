import Database from 'better-sqlite3';
import {createClient} from '@libsql/client';
import {createHash} from 'node:crypto';
import {decompress} from 'fzstd';
const src=new Database(process.argv[2],{readonly:true});
const db=createClient({url:process.env.TURSO_DATABASE_URL,authToken:process.env.TURSO_AUTH_TOKEN});
const specs=[['metadata',['key']],['store_directory',['store_id']],['store_bundles',['store_id','chunk_no']],['search_buckets',['bucket_id']],['auxiliary_bundles',['name']]];
try {
  for (const [table,keys] of specs) {
    const count=src.prepare(`select count(*) n from ${table}`).get().n;
    const remote=Number((await db.execute(`select count(*) n from ${table}`)).rows[0].n);
    if(count!==remote) throw new Error(`${table} count mismatch`);
    let verified=0;
    // Key batches bound both the response size and client memory.
    const check=async rows=>{
      const where=rows.map(()=>`(${keys.map(k=>`${k}=?`).join(' and ')})`).join(' or ');
      const result=await db.execute({sql:`select * from ${table} where ${where}`,args:rows.flatMap(r=>keys.map(k=>r[k]))});
      const key=r=>keys.map(k=>String(r[k])).join('\0');
      const byKey=new Map(result.rows.map(r=>[key(r),r]));
      for(const local of rows){
        const other=byKey.get(key(local));
        if(!other) throw new Error(`${table}: missing row`);
        for(const [column,value] of Object.entries(local)){
          const actual=other[column];
          const equal=Buffer.isBuffer(value)?Buffer.from(actual).equals(value):value===actual || (typeof actual==='bigint' && String(value)===String(actual));
          if(!equal) throw new Error(`${table}: ${column} content mismatch`);
        }
        if(local.payload){
          const raw=decompress(new Uint8Array(other.payload));
          if(raw.byteLength!==Number(other.raw_bytes) || createHash('sha256').update(raw).digest('hex')!==other.checksum)
            throw new Error(`${table}: remote decompression/checksum mismatch`);
        }
        verified++;
      }
    };
    let rows=[];
    for(const row of src.prepare(`select * from ${table} order by rowid`).iterate()){
      rows.push(row); if(rows.length===10){await check(rows);rows=[];}
    }
    if(rows.length) await check(rows);
    console.log(`${table}: ${verified} complete rows verified`);
  }
} finally {src.close();db.close();}
