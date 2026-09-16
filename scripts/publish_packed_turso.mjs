import Database from 'better-sqlite3';
import {createClient} from '@libsql/client';
const file=process.argv[2];
if(!file||!process.env.TURSO_DATABASE_URL||!process.env.TURSO_AUTH_TOKEN) throw new Error('packed database path and Turso credentials required');
const src=new Database(file,{readonly:true}); const db=createClient({url:process.env.TURSO_DATABASE_URL,authToken:process.env.TURSO_AUTH_TOKEN});
const tables=['metadata','store_directory','store_bundles','search_buckets','auxiliary_bundles'];
const existing=await db.execute("select count(*) count from sqlite_schema where type='table' and name in ('store_directory','store_bundles','search_buckets')");
if(Number(existing.rows[0].count)!==0) throw new Error('packed target is not empty');
for(const {sql} of src.prepare("select sql from sqlite_schema where type='table' and sql is not null and name not like 'sqlite_%'").all()) await db.execute(sql);
for(const table of tables){
  const cols=src.prepare(`pragma table_info(${table})`).all().map(x=>x.name); let n=0;
  for(const row of src.prepare(`select * from ${table}`).iterate()){
    await db.execute({sql:`insert into ${table}(${cols.join(',')}) values(${cols.map(()=>'?').join(',')})`,args:cols.map(c=>row[c])});
    if(++n%10000===0) console.log(`published ${table}: ${n.toLocaleString()}`);
  }
  console.log(`published ${table}: ${n.toLocaleString()}`);
}
for(const table of tables){const local=src.prepare(`select count(*) n from ${table}`).get().n;const remote=Number((await db.execute(`select count(*) n from ${table}`)).rows[0].n);if(local!==remote)throw new Error(`${table} count mismatch ${local} != ${remote}`);}
console.log('packed publish verified'); src.close(); db.close();
