import {createClient} from '@libsql/client';

if(!process.env.TURSO_DATABASE_URL || !process.env.TURSO_AUTH_TOKEN)
  throw new Error('Turso credentials required');
const db=createClient({url:process.env.TURSO_DATABASE_URL,authToken:process.env.TURSO_AUTH_TOKEN});
const result=await db.execute('select store_id,store_uuid from store_directory order by store_id');
const mapping={};
for(const row of result.rows) mapping[String(row.store_uuid)]=Number(row.store_id);
process.stdout.write(JSON.stringify(mapping));
db.close();
