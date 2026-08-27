"""Install publication metadata before deploying the publication-aware API."""
import os
from d1_publication import PUBLICATION_DDL, query_remote

if __name__ == "__main__":
    for statement in PUBLICATION_DDL.split(";"):
        if statement.strip():
            query_remote("ubereats_monitor", statement + ";", dict(os.environ))
    # Preserve the pre-migration read surface without claiming old snapshots were
    # verified. Capture once, before any new importer starts writing staging rows.
    tables = query_remote("ubereats_monitor", "SELECT name FROM sqlite_master WHERE type='table' AND name='products';", dict(os.environ))
    if tables:
        query_remote("ubereats_monitor", "INSERT OR IGNORE INTO publication_legacy_baseline SELECT DISTINCT crawled_time FROM products WHERE NOT EXISTS (SELECT 1 FROM publication_migrations WHERE name='legacy-baseline');", dict(os.environ))
    query_remote("ubereats_monitor", "INSERT OR IGNORE INTO publication_migrations VALUES ('legacy-baseline');", dict(os.environ))
