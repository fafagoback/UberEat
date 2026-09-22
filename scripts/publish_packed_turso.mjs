import Database from 'better-sqlite3';
import {createClient} from '@libsql/client';
import {createHash} from 'node:crypto';

const file=process.argv[2];
if(!file||!process.env.TURSO_DATABASE_URL||!process.env.TURSO_AUTH_TOKEN)
  throw new Error('packed database path and Turso credentials required');
const src=new Database(file,{readonly:true});
const db=createClient({url:process.env.TURSO_DATABASE_URL,authToken:process.env.TURSO_AUTH_TOKEN});
const tables=[
  {name:'metadata',keys:['key'],remote:'key,value'},
  {name:'store_directory',keys:['store_id'],remote:'*'},
  {name:'store_bundles',keys:['store_id','chunk_no'],remote:'store_id,chunk_no,checksum'},
  {name:'search_buckets',keys:['bucket_id'],remote:'bucket_id,checksum'},
  {name:'auxiliary_bundles',keys:['name'],remote:'name,checksum'},
];
const bytes=v=>v==null?0:Buffer.isBuffer(v)||v instanceof Uint8Array?v.byteLength:Buffer.byteLength(String(v));
const keyOf=(row,keys)=>keys.map(k=>String(row[k])).join('\0');
const digest=(row,cols)=>createHash('sha256').update(cols.map(c=>`${c}=${String(row[c])}`).join('\0')).digest('hex');
const started=Date.now();

for(const {sql} of src.prepare("select sql from sqlite_schema where type='table' and sql is not null and name not like 'sqlite_%'").all()){
  try{await db.execute(sql)}catch(error){
    if(!String(error.message||error).toLowerCase().includes('already exists')) throw error;
  }
}

const local={};
for(const spec of tables){
  const cols=src.prepare(`pragma table_info(${spec.name})`).all().map(x=>x.name);
  const rows=src.prepare(`select * from ${spec.name}`).all();
  local[spec.name]={spec,cols,rows,byKey:new Map(rows.map(row=>[keyOf(row,spec.keys),row]))};
}

const remote={};
for(const spec of tables){
  const selected=spec.remote==='*'?local[spec.name].cols.join(','):spec.remote;
  const result=await db.execute(`select ${selected} from ${spec.name}`);
  const rows=result.rows;
  const compareCols=spec.name==='store_directory'?local[spec.name].cols:
    (spec.name==='metadata'?['value']:['checksum']);
  remote[spec.name]={
    byKey:new Map(rows.map(row=>[keyOf(row,spec.keys),{fingerprint:digest(row,compareCols),row}])),
    keys:new Set(rows.map(row=>keyOf(row,spec.keys))),
  };
}

let deltaBytes=0;
const changed={};
for(const spec of tables){
  const l=local[spec.name], r=remote[spec.name];
  const compareCols=spec.name==='store_directory'?l.cols:
    (spec.name==='metadata'?['value']:['checksum']);
  changed[spec.name]=l.rows.filter(row=>{
    const old=r.byKey.get(keyOf(row,spec.keys));
    const different=!old||digest(row,compareCols)!==old.fingerprint;
    if(different) deltaBytes+=l.cols.reduce((n,c)=>n+bytes(row[c]),0);
    return different;
  });
}
const stale={};
for(const spec of tables) stale[spec.name]=[...remote[spec.name].keys].filter(k=>!local[spec.name].byKey.has(k));

const batchSize=Number(process.env.TURSO_BATCH_ROWS||100);
for(const spec of tables){
  const l=local[spec.name];
  const updates=l.cols.filter(c=>!spec.keys.includes(c));
  const insert=`insert into ${spec.name}(${l.cols.join(',')}) values(${l.cols.map(()=>'?').join(',')}) on conflict(${spec.keys.join(',')}) do update set ${updates.map(c=>`${c}=excluded.${c}`).join(',')}`;
  for(let i=0;i<changed[spec.name].length;i+=batchSize){
    const statements=changed[spec.name].slice(i,i+batchSize).map(row=>({sql:insert,args:l.cols.map(c=>row[c])}));
    if(statements.length) await db.batch(statements,'write');
  }
  const deleteSql=`delete from ${spec.name} where ${spec.keys.map(k=>`${k}=?`).join(' and ')}`;
  const deletes=stale[spec.name].map(key=>({sql:deleteSql,args:key.split('\0')}));
  for(let i=0;i<deletes.length;i+=batchSize) if(deletes.length) await db.batch(deletes.slice(i,i+batchSize),'write');
  console.log(`${spec.name}: changed=${changed[spec.name].length} stale=${stale[spec.name].length}`);
}

for(const spec of tables){
  const count=Number((await db.execute(`select count(*) n from ${spec.name}`)).rows[0].n);
  if(count!==local[spec.name].rows.length)
    throw new Error(`${spec.name} count mismatch ${count} != ${local[spec.name].rows.length}`);
}
console.log(`packed delta updated: ${deltaBytes} bytes (${changed.store_directory.length} stores, ${changed.store_bundles.length} bundles, ${changed.search_buckets.length} buckets) in ${Math.round((Date.now()-started)/1000)}s`);
src.close();db.close();
