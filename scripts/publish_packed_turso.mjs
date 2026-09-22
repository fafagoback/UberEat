import Database from 'better-sqlite3';
import {createClient} from '@libsql/client';
import {createHash} from 'node:crypto';

const file=process.argv[2];
if(!file||!process.env.TURSO_DATABASE_URL||!process.env.TURSO_AUTH_TOKEN) throw new Error('packed database path and Turso credentials required');
const src=new Database(file,{readonly:true});
const db=createClient({url:process.env.TURSO_DATABASE_URL,authToken:process.env.TURSO_AUTH_TOKEN});
const tables=['metadata','store_directory','store_bundles','search_buckets','auxiliary_bundles'];
const maxRows=Number(process.env.TURSO_BATCH_ROWS||100);
const maxBytes=Number(process.env.TURSO_BATCH_BYTES||4*1024*1024);
const started=Date.now();
const bytes=v=>v==null?0:Buffer.isBuffer(v)||v instanceof Uint8Array?v.byteLength:Buffer.byteLength(String(v));
const human=n=>{const u=['B','KB','MB','GB'];let i=0;while(n>=1024&&i<u.length-1){n/=1024;i++;}return `${n.toFixed(i?1:0)} ${u[i]}`;};
const elapsed=s=>{s=Math.max(0,Math.round(s));return `${Math.floor(s/3600)}h ${Math.floor(s%3600/60)}m ${s%60}s`;};

// Bind checkpoints to the exact packed input. Packed payloads already have checksums,
// so hashing identities and checksums avoids rereading every multi-GB blob.
const hash=createHash('sha256');
for(const table of tables){
  const cols=src.prepare(`pragma table_info(${table})`).all().map(x=>x.name);
  // build_seconds in metadata is intentionally excluded: rebuilding identical
  // source data must produce the same resumable identity.
  const ids=table==='metadata'?['key']:cols.filter(c=>c!=='payload');
  hash.update(`${table}\0`);
  for(const row of src.prepare(`select ${ids.join(',')} from ${table} order by rowid`).iterate())
    for(const col of ids) hash.update(`${col}=${String(row[col])}\0`);
}
const sourceId=hash.digest('hex');

for(const {sql} of src.prepare("select sql from sqlite_schema where type='table' and sql is not null and name not like 'sqlite_%'").all()){
  try{await db.execute(sql);}catch(error){if(!String(error.message||error).toLowerCase().includes('already exists'))throw error;}
}
await db.execute(`create table if not exists _packed_publish_progress(
 source_id text not null,table_name text not null,last_rowid integer not null,
 rows_done integer not null,bytes_done integer not null,updated_at text not null,
 primary key(source_id,table_name))`);

const totals={};let totalRows=0,totalBytes=0;
for(const table of tables){
  const cols=src.prepare(`pragma table_info(${table})`).all().map(x=>x.name);
  const sizeSql=cols.map(c=>`coalesce(length(cast(${c} as blob)),0)`).join('+');
  let rowCount=0,byteCount=0,batchRows=0,batchBytes=0;const batchEnds=[];
  for(const row of src.prepare(`select rowid,${sizeSql} size from ${table} order by rowid`).iterate()){
    if(batchRows&&(batchRows===maxRows||batchBytes+Number(row.size)>maxBytes)){
      batchEnds.push(rowCount);batchRows=0;batchBytes=0;
    }
    rowCount++;byteCount+=Number(row.size);batchRows++;batchBytes+=Number(row.size);
  }
  if(batchRows)batchEnds.push(rowCount);
  totals[table]={rows:rowCount,bytes:byteCount,batches:batchEnds.length,batchEnds};totalRows+=rowCount;totalBytes+=byteCount;
}
console.log(`source ${sourceId.slice(0,12)}: ${totalRows.toLocaleString()} rows, ${human(totalBytes)}; batch limits ${maxRows} rows/${human(maxBytes)}`);

let globalRows=0,globalBytes=0;
for(const table of tables){
  const cols=src.prepare(`pragma table_info(${table})`).all().map(x=>x.name);
  const insertSql=`insert or replace into ${table}(${cols.join(',')}) values(${cols.map(()=>'?').join(',')})`;
  const minRowid=Number(src.prepare(`select coalesce(min(rowid),1) n from ${table}`).get().n);
  let saved=(await db.execute({sql:'select last_rowid,rows_done,bytes_done from _packed_publish_progress where source_id=? and table_name=?',args:[sourceId,table]})).rows[0];
  if(saved&&Number(saved.rows_done)!==Number(saved.last_rowid)-minRowid+1){
    console.log(`${table}: discarding inconsistent checkpoint (rowid ${saved.last_rowid}, rows ${saved.rows_done})`);
    await db.execute({sql:'delete from _packed_publish_progress where source_id=? and table_name=?',args:[sourceId,table]});
    saved=undefined;
  }

  // A different packed source must be replayed from row 1. INSERT OR REPLACE
  // updates the existing database in place; it never drops or recreates it.
  if(!saved){
    const remote=(await db.execute(`select count(*) n,coalesce(max(rowid),0) last_rowid from ${table}`)).rows[0];
    const count=Number(remote.n),last=Number(remote.last_rowid);
    if(count&&table!=='metadata'){
      const localLast=src.prepare(`select * from ${table} where rowid=?`).get(last);
      const remoteLast=(await db.execute(`select * from ${table} where rowid=${last}`)).rows[0];
      const comparable=cols.filter(c=>c!=='payload');
      const prefixMatches=Number(src.prepare(`select count(*) n from ${table} where rowid<=?`).get(last).n)===count
        && localLast&&remoteLast&&!comparable.some(c=>String(localLast[c])!==String(remoteLast[c]));
      if(prefixMatches){
        let doneBytes=0;
        for(const row of src.prepare(`select * from ${table} where rowid<=?`).iterate(last)) doneBytes+=cols.reduce((n,c)=>n+bytes(row[c]),0);
        await db.execute({sql:'insert into _packed_publish_progress values(?,?,?,?,?,?)',args:[sourceId,table,last,count,doneBytes,new Date().toISOString()]});
        saved={last_rowid:last,rows_done:count,bytes_done:doneBytes};
        console.log(`${table}: adopted matching existing prefix ${count.toLocaleString()}/${totals[table].rows.toLocaleString()} rows`);
      }else{
        console.log(`${table}: existing rows belong to an older snapshot; updating in place from row 1`);
      }
    }
    // Metadata is tiny and can contain non-deterministic build timings. Replacing
    // it is safer and cheaper than trying to adopt an old prefix.
  }

  let last=saved?Number(saved.last_rowid):minRowid-1,done=Number(saved?.rows_done||0),doneBytes=Number(saved?.bytes_done||0),batch=0;
  const resumedBatches=totals[table].batchEnds.filter(end=>end<=done).length;
  globalRows+=done;globalBytes+=doneBytes;
  if(done)console.log(`${table}: resuming at ${done.toLocaleString()}/${totals[table].rows.toLocaleString()} rows`);
  while(done<totals[table].rows){
    const candidates=src.prepare(`select rowid __rowid,* from ${table} where rowid>? order by rowid limit ?`).all(last,maxRows);
    if(!candidates.length)throw new Error(`${table}: source ended before expected row count`);
    const chunk=[];let chunkBytes=0;
    for(const row of candidates){
      const size=cols.reduce((n,c)=>n+bytes(row[c]),0);
      if(chunk.length&&chunkBytes+size>maxBytes)break;
      chunk.push(row);chunkBytes+=size;
    }
    const nextLast=chunk.at(-1).__rowid,nextDone=done+chunk.length,nextBytes=doneBytes+chunkBytes;
    const statements=chunk.map(row=>({sql:insertSql,args:cols.map(c=>row[c])}));
    statements.push({sql:'insert or replace into _packed_publish_progress values(?,?,?,?,?,?)',args:[sourceId,table,nextLast,nextDone,nextBytes,new Date().toISOString()]});
    await db.batch(statements,'write'); // rows and checkpoint commit atomically
    last=nextLast;done=nextDone;doneBytes=nextBytes;batch++;globalRows+=chunk.length;globalBytes+=chunkBytes;
    const seconds=(Date.now()-started)/1000,rate=globalBytes/Math.max(seconds,0.001),remaining=totalBytes-globalBytes;
    const percent=totalBytes?globalBytes/totalBytes*100:globalRows/totalRows*100;
    console.log(`${table} batch ${resumedBatches+batch}/${totals[table].batches}: ${done.toLocaleString()}/${totals[table].rows.toLocaleString()} rows | total ${percent.toFixed(2)}% (${human(globalBytes)}/${human(totalBytes)}) | ${human(rate)}/s | ETA ${elapsed(remaining/rate)}`);
  }

  // Remove only rows left behind by an older, larger packed snapshot. The
  // database and all rows represented by the new cumulative snapshot remain.
  if(table==='metadata'||table==='auxiliary_bundles'){
    const key=table==='metadata'?'key':'name';
    const keys=src.prepare(`select ${key} value from ${table}`).all().map(row=>row.value);
    if(keys.length) await db.execute({sql:`delete from ${table} where ${key} not in (${keys.map(()=>'?').join(',')})`,args:keys});
  }else if(table==='store_directory'){
    const maxId=Number(src.prepare('select coalesce(max(store_id),-1) n from store_directory').get().n);
    await db.execute({sql:'delete from store_directory where store_id>?',args:[maxId]});
  }else if(table==='search_buckets'){
    const maxId=Number(src.prepare('select coalesce(max(bucket_id),-1) n from search_buckets').get().n);
    await db.execute({sql:'delete from search_buckets where bucket_id>?',args:[maxId]});
  }
}

// A store can shrink from multiple chunks to fewer chunks without changing
// the maximum table rowid, so remove only obsolete chunks for that store.
await db.execute(`delete from store_bundles
  where not exists (
    select 1 from store_directory d
    where d.store_id=store_bundles.store_id
      and store_bundles.chunk_no<d.chunk_count
  )`);

for(const table of tables){
  const remote=Number((await db.execute(`select count(*) n from ${table}`)).rows[0].n);
  if(totals[table].rows!==remote)throw new Error(`${table} count mismatch ${totals[table].rows} != ${remote}`);
}
console.log(`packed publish verified in ${elapsed((Date.now()-started)/1000)}`);
src.close();db.close();
