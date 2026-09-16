import Database from 'better-sqlite3';
import {createClient} from '@libsql/client';

const file=process.argv[2];
if(!file||!process.env.TURSO_DATABASE_URL||!process.env.TURSO_AUTH_TOKEN)
  throw new Error('packed database path and Turso credentials required');

const src=new Database(file,{readonly:true});
const db=createClient({url:process.env.TURSO_DATABASE_URL,authToken:process.env.TURSO_AUTH_TOKEN});
const tables=['metadata','store_directory','store_bundles','search_buckets','auxiliary_bundles'];

for(const table of tables){
  const local=Number(src.prepare(`select count(*) n from ${table}`).get().n);
  const remote=Number((await db.execute(`select count(*) n from ${table}`)).rows[0].n);
  if(local!==remote) throw new Error(`${table} count mismatch ${local} != ${remote}`);
  console.log(`${table}: ${remote.toLocaleString()} rows verified`);
}

const localMetadata=src.prepare('select key,value from metadata order by key').all();
const remoteMetadata=(await db.execute('select key,value from metadata order by key')).rows
  .map(row=>({key:String(row.key),value:String(row.value)}));
if(JSON.stringify(localMetadata)!==JSON.stringify(remoteMetadata))
  throw new Error('metadata mismatch after database upload');

console.log('uploaded packed database verified');
src.close();
db.close();
