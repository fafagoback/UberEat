import Database from 'better-sqlite3';
import {createClient} from '@libsql/client';
import {statSync} from 'node:fs';

// In-place release updates remain disabled until atomic release addressing exists.
const DAILY_DELTA_LIMIT = 262144000;
const file = process.argv[2];
if (!file || process.env.TURSO_PUBLISH_MODE !== 'bootstrap')
  throw new Error(`Only isolated bootstrap is enabled; daily delta remains capped at ${DAILY_DELTA_LIMIT}`);
const url = process.env.TURSO_DATABASE_URL || '';
if (!/^libsql:\/\/ubereats-packed-v[2-9][0-9]*-[a-z0-9-]+\./.test(url))
  throw new Error('Refusing to write an unversioned or production database');
if (statSync(file).size >= 4_750_000_000) throw new Error('Packed storage guard exceeded');
const src = new Database(file, {readonly:true});
const meta = Object.fromEntries(src.prepare('select key,value from metadata').all().map(r => [r.key, JSON.parse(r.value)]));
if (meta.definition_version !== '2026-09-search-v3' || !meta.latest_batch || !meta.source_revision)
  throw new Error('Missing or incompatible packed release metadata');
const tables = ['store_directory','store_bundles','search_buckets','auxiliary_bundles','metadata'];
for (const t of tables) if (!src.prepare('select 1 from sqlite_schema where type=? and name=?').get('table',t))
  throw new Error(`Not a packed database: ${t}`);
const db = createClient({url, authToken:process.env.TURSO_AUTH_TOKEN});
const existing = await db.execute("select count(*) n from sqlite_schema where type='table' and name not like 'sqlite_%'");
if (Number(existing.rows[0].n) !== 0) throw new Error('Bootstrap target must be empty; never reset or overwrite it');
for (const row of src.prepare("select sql from sqlite_schema where type='table' and name not like 'sqlite_%'").iterate())
  await db.execute(row.sql);
const size = v => v == null ? 0 : (v instanceof Uint8Array ? v.byteLength : Buffer.byteLength(String(v)));
try {
  for (const table of tables) {
    const cols=src.prepare(`pragma table_info(${table})`).all().map(r=>r.name);
    const sql=`insert into ${table}(${cols.join(',')}) values(${cols.map(()=>'?').join(',')})`;
    let batch=[], bytes=0, count=0;
    for (const row of src.prepare(`select * from ${table} order by rowid`).iterate()) {
      const args=cols.map(c=>row[c]); const n=args.reduce((a,v)=>a+size(v),0);
      if (n > 3_000_000) throw new Error('One packed row exceeds upload request budget');
      if (batch.length && (bytes+n > 3_000_000 || batch.length >= 50)) {
        await db.batch(batch,'write'); batch=[]; bytes=0;
      }
      batch.push({sql,args}); bytes+=n; count++;
    }
    if (batch.length) await db.batch(batch,'write');
    console.log(`${table}: ${count} rows uploaded`);
  }
} finally { src.close(); db.close(); }
console.log('Isolated bootstrap complete. Production config has not changed.');
