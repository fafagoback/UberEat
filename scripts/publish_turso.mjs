import Database from 'better-sqlite3';
import { createClient } from '@libsql/client';

const file=process.argv[2];
if(!file||!process.env.TURSO_DATABASE_URL||!process.env.TURSO_AUTH_TOKEN) throw new Error('database path and Turso credentials required');
const src=new Database(file);
const db=createClient({url:process.env.TURSO_DATABASE_URL,authToken:process.env.TURSO_AUTH_TOKEN});
const batchSize=Number(process.env.TURSO_BATCH_SIZE||1000);

if(process.env.TURSO_REQUIRE_EMPTY==='1'){
  const existing=await db.execute("SELECT count(*) AS count FROM sqlite_schema WHERE type='table' AND name IN ('stores','products','events','crawl_batches','metadata')");
  if(Number(existing.rows[0].count)!==0) throw new Error('rebuild target is not empty; refusing to mix old and rebuilt data');
}

for(const {sql} of src.prepare("SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'product_search%' AND name NOT LIKE 'pending_%' AND type='table'").all()){
  try{await db.execute(sql);}catch(error){if(!String(error.message||error).includes('already exists'))throw error;}
}

// Migrate databases created before the rolling three-snapshot price fields and is_open.
for(const sql of [
  "ALTER TABLE stores ADD COLUMN is_open INTEGER NOT NULL DEFAULT 1",
  "ALTER TABLE products ADD COLUMN recent_prices TEXT NOT NULL DEFAULT '[]'",
  "ALTER TABLE products ADD COLUMN price_novel_vs_previous_3 INTEGER NOT NULL DEFAULT 0",
  "ALTER TABLE products ADD COLUMN reference_price REAL",
  "ALTER TABLE products ADD COLUMN discount_amount REAL NOT NULL DEFAULT 0",
  "ALTER TABLE products ADD COLUMN discount_pct REAL NOT NULL DEFAULT 0",
  "ALTER TABLE products ADD COLUMN is_price_deal INTEGER NOT NULL DEFAULT 0",
  "ALTER TABLE products ADD COLUMN is_open INTEGER NOT NULL DEFAULT 1"
]){
  try{await db.execute(sql);}catch(error){
    const message=String(error.message||error).toLowerCase();
    if(!message.includes('duplicate column')&&!message.includes('already exists'))throw error;
  }
}

for(const {sql} of src.prepare("SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'product_search%' AND type='index'").all()){
  try{await db.execute(sql);}catch(error){if(!String(error.message||error).includes('already exists'))throw error;}
}

let remoteLatest='';
try{
  const result=await db.execute("SELECT value FROM metadata WHERE key='latest_batch'");
  remoteLatest=String(result.rows[0]?.value||'');
}catch(error){
  if(!String(error.message||error).toLowerCase().includes('no such table')) throw error;
}

const deltaTables={
  stores:{keys:['store_uuid'],query:'SELECT s.* FROM stores s JOIN pending_store_changes p USING(store_uuid)'},
  products:{keys:['store_uuid','product_uuid'],query:'SELECT p.* FROM products p JOIN pending_product_changes d USING(store_uuid,product_uuid)'},
  crawl_batches:{keys:['batch_id'],query:'SELECT * FROM crawl_batches WHERE batch_id > ?'},
};
for(const [table,{keys,query}] of Object.entries(deltaTables)){
  const cols=src.prepare(`PRAGMA table_info(${table})`).all().map(x=>x.name);
  const updates=cols.filter(c=>!keys.includes(c)&&c!=='first_seen');
  const suffix=` ON CONFLICT(${keys.join(',')}) DO UPDATE SET ${updates.map(c=>`${c}=excluded.${c}`).join(',')}`;
  let pending=[],published=0;
  const flush=async()=>{const values=pending.map(()=>`(${cols.map(()=>'?').join(',')})`).join(',');const args=pending.flatMap(row=>cols.map(c=>row[c]));await db.execute({sql:`INSERT INTO ${table}(${cols.join(',')}) VALUES ${values}${suffix}`,args});published+=pending.length;pending=[];};
  const rows=src.prepare(query);
  const iterator=table==='crawl_batches'?rows.iterate(remoteLatest):rows.iterate();
  for(const row of iterator){
    pending.push(row);
    if(pending.length===batchSize){await flush();if(published%100000===0)console.log(`publishing ${table}: ${published.toLocaleString()}`);}
  }
  if(pending.length)await flush();
  console.log(`published ${table}: ${published.toLocaleString()}`);
}
await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedupe ON events(event_time,store_uuid,coalesce(product_uuid,''),event_type)");
const eventCols=['event_time','store_uuid','product_uuid','event_type','old_state','new_state'];let pending=[];
const eventCutoff=remoteLatest ? `${remoteLatest.slice(0,4)}-${remoteLatest.slice(4,6)}-${remoteLatest.slice(6,8)}T${remoteLatest.slice(8,10)}:${remoteLatest.slice(10,12)}:${remoteLatest.slice(12,14)}+08:00` : '';
for(const row of src.prepare('SELECT * FROM events WHERE event_time > ? ORDER BY id').iterate(eventCutoff)){
  pending.push(row);
  if(pending.length===batchSize){const values=pending.map(()=>'(?,?,?,?,?,?)').join(',');await db.execute({sql:`INSERT OR IGNORE INTO events(${eventCols.join(',')}) VALUES ${values}`,args:pending.flatMap(r=>eventCols.map(c=>r[c]))});pending=[];}
}
if(pending.length){const values=pending.map(()=>'(?,?,?,?,?,?)').join(',');await db.execute({sql:`INSERT OR IGNORE INTO events(${eventCols.join(',')}) VALUES ${values}`,args:pending.flatMap(r=>eventCols.map(c=>r[c]))});}
for(const row of src.prepare('SELECT * FROM metadata').iterate()){
  await db.execute({sql:'INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',args:[row.key,row.value]});
}
const result=await db.execute("SELECT (SELECT count(*) FROM stores) stores,(SELECT count(*) FROM products) products,(SELECT count(*) FROM events) events");
console.log('remote_counts',result.rows[0]);
src.exec('DELETE FROM pending_store_changes; DELETE FROM pending_product_changes;');
src.close();db.close();
