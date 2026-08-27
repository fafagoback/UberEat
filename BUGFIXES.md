# Pipeline safety fixes (2026-08-27)

The failure in run 33035330847 was caused by an eight-character MD5 prefix
collision, not duplicate restaurants. Snapshots now compare complete store URL
sets, validate schemas and batch IDs, and use collision-resistant archive names.

- Crawlers preserve full store IDs and source menu item IDs. Explicitly empty API
  catalogs are recorded separately from incomplete responses. Failed discovery
  pages and truncated pagination fail the worker instead of counting as completed.
- Both worker pipelines require full assigned-store coverage. Dynamic worker
  counts are passed downstream, and duplicate/mixed discovery chunks are rejected.
- D1 imports stage data before publishing. Structured counts for every exported
  detail table must exactly match the local batch. Regional runs do not replace
  the Taiwan batch. Both workflows and Worker deployment share a production lock.
- The migration captures the existing read surface once in a legacy baseline so
  deployment does not blank the site. These historical batches are **not** newly
  certified complete; only new batches receive verified publication markers.
- Cuisine/hour duplicates are reduced to one identical record and unique indexes
  make retrying imports idempotent. Existing historical snapshots are retained.
- Same-name source variants receive separate IDs; unambiguous names retain legacy
  IDs for continuity. Ambiguous historical variants cannot be separated reliably
  and are not reassigned retroactively.
- Dataset text is no longer injected into inline JavaScript handlers; navigation
  URLs are allowlisted. Stale search requests cannot overwrite newer results.
- Pagination parameters are bounded, SQLite connections are reused and closed,
  and CI runs Python and JavaScript regression tests on pushes and pull requests.

Verification: `python -m unittest discover -s tests -v` and
`node --test tests/frontend.test.cjs`.

GitHub's rerun operation retains the original commit. To run these fixes, dispatch
`taiwan_store_crawler.yml` on the updated `main`, with `max_pages_per_point=0`.
An incomplete upstream response must remain a failed run, not a green partial
snapshot. Its menu artifacts are retained for diagnosis and recovery.
