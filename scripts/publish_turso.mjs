import Database from 'better-sqlite3';
import { createClient } from '@libsql/client';

const file=process.argv[2];
if(!file||!process.env.TURSO_DATABASE_URL||!process.env.TURSO_AUTH_TOKEN) throw new Error('database path and Turso credentials required');
const src=new Database(file,{readonly:true});
const db=createClient({url:process.env.TURSO_DATABASE_URL,authToken:process.env.TURSO_AUTH_TOKEN});
const batchSize=Number(process.env.TURSO_BATCH_SIZE||1000);

for(const {sql} of src.prepare("SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'product_search%' AND type IN ('table','index') ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END").all()) await db.execute(sql);

for(const [table,keys] of Object.entries({stores:['store_uuid'],products:['store_uuid','product_uuid'],crawl_batches:['batch_id'],metadata:['key']})){
  const cols=src.prepare(`PRAGMA table_info(${table})`).all().map(x=>x.name);
  const updates=cols.filter(c=>!keys.includes(c)&&c!=='first_seen');
  const sql=`INSERT INTO ${table}(${cols.join(',')}) VALUES(${cols.map(()=>'?').join(',')}) ON CONFLICT(${keys.join(',')}) DO UPDATE SET ${updates.map(c=>`${c}=excluded.${c}`).join(',')}`;
  let pending=[],published=0;
  for(const row of src.prepare(`SELECT * FROM ${table}`).iterate()){
    pending.push({sql,args:cols.map(c=>row[c])});
    if(pending.length===batchSize){await db.batch(pending,'write');published+=pending.length;pending=[];if(published%100000===0)console.log(`publishing ${table}: ${published.toLocaleString()}`);}
  }
  if(pending.length){await db.batch(pending,'write');published+=pending.length;}
  console.log(`published ${table}: ${published.toLocaleString()}`);
}
await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedupe ON events(event_time,store_uuid,coalesce(product_uuid,''),event_type)");
const eventCols=['event_time','store_uuid','product_uuid','event_type','old_state','new_state'];let pending=[];
for(const row of src.prepare('SELECT * FROM events').iterate()){
  pending.push({sql:'INSERT OR IGNORE INTO events(event_time,store_uuid,product_uuid,event_type,old_state,new_state) VALUES(?,?,?,?,?,?)',args:eventCols.map(c=>row[c])});
  if(pending.length===batchSize){await db.batch(pending,'write');pending=[];}
}
if(pending.length)await db.batch(pending,'write');
await db.execute("DELETE FROM events WHERE event_time < datetime('now','-60 days')");
const result=await db.execute("SELECT (SELECT count(*) FROM stores) stores,(SELECT count(*) FROM products) products,(SELECT count(*) FROM events) events");
console.log('remote_counts',result.rows[0]);src.close();db.close();
