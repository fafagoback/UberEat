"""Publish a validated local CURRENT/EVENTS SQLite database to Turso."""
from __future__ import annotations
import argparse, os, sqlite3

def chunks(rows, size=250):
    for i in range(0, len(rows), size): yield rows[i:i+size]

def main():
    p=argparse.ArgumentParser(); p.add_argument('--database',required=True); a=p.parse_args()
    url=os.environ.get('TURSO_DATABASE_URL','').replace('libsql://','https://')
    token=os.environ.get('TURSO_AUTH_TOKEN')
    if not url or not token: raise SystemExit('Turso credentials missing')
    import libsql_client
    src=sqlite3.connect(a.database); src.row_factory=sqlite3.Row
    with libsql_client.create_client_sync(url=url,auth_token=token) as db:
        schema=[r[0] for r in src.execute("SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'product_search%' AND type IN ('table','index') ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END")]
        for sql in schema:
            try: db.execute(sql)
            except Exception as e:
                if 'already exists' not in str(e).lower(): raise
        specs={
          'stores':('store_uuid',), 'products':('store_uuid','product_uuid'),
          'crawl_batches':('batch_id',), 'metadata':('key',)
        }
        for table,keys in specs.items():
            cols=[r[1] for r in src.execute(f'PRAGMA table_info({table})')]
            update=[c for c in cols if c not in keys and c!='first_seen']
            sql=f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join(['?']*len(cols))}) ON CONFLICT({','.join(keys)}) DO UPDATE SET "+','.join(f'{c}=excluded.{c}' for c in update)
            rows=[tuple(r[c] for c in cols) for r in src.execute(f'SELECT * FROM {table}')]
            for part in chunks(rows): db.batch([libsql_client.Statement(sql,list(row)) for row in part])
            print(f'published {table}: {len(rows):,}')
        # Events use a deterministic natural duplicate guard for cache replays.
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedupe ON events(event_time,store_uuid,coalesce(product_uuid,''),event_type)")
        cols=['event_time','store_uuid','product_uuid','event_type','old_state','new_state']
        sql="INSERT OR IGNORE INTO events(event_time,store_uuid,product_uuid,event_type,old_state,new_state) VALUES(?,?,?,?,?,?)"
        rows=[tuple(r[c] for c in cols) for r in src.execute('SELECT * FROM events')]
        for part in chunks(rows): db.batch([libsql_client.Statement(sql,list(row)) for row in part])
        db.execute("DELETE FROM events WHERE event_time < datetime('now','-60 days')")
        # Turso/libSQL FTS availability varies; API search falls back to indexed LIKE.
        result=db.execute("SELECT (SELECT count(*) FROM stores),(SELECT count(*) FROM products),(SELECT count(*) FROM events)")
        print('remote_counts',result.rows[0])

if __name__=='__main__': main()
