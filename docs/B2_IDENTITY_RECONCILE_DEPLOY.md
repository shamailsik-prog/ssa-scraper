# B2 identity reconcile — manual post-deploy runbook

Do **not** run these steps on production until harvest/scrape jobs are paused and you have a maintenance window. Migrations take `ACCESS EXCLUSIVE` locks on `judgment`.

## 1. Pause harvest

Stop or drain PakistanLawSite harvest and related login-session scrapers so new judgments are not written during migrations.

## 2. Apply migrations (in order)

For **each** migration below, set a lock timeout on the session before running `upgrade` (DDL needs `ACCESS EXCLUSIVE` on `judgment`):

```sql
SET lock_timeout = '30s';
```

Then apply, in order:

1. `018_judgment_case_text_hash` (if not already applied on the environment)
2. `019_judgment_identity_reconcile`
3. `020_judgment_case_hash_unreported_scope`
4. `021_repair_case_hash_unreported_scope`
5. `022_statute_section_refetch_flags` (B3 quality/refetch flags on `statute`; no row deletion)
6. `023_statute_name_lower_index` (functional index on `lower(name)`)
7. `024_statute_canonical_name_key` (indexed `canonical_name_key` + backfill)

Use the project’s normal migration runner (`run_migrations` / deploy script). Never downgrade 019 while `judgment_identity_link` or `judgment_identity_review` contain rows.

**B3 statute migrations (022–024):**

- **022 downgrade** is blocked while any `statute` row still has `needs_section_refetch`, `quality_flag_no_year`, or `quality_flag_duplicate_name` set. Clear those flags (or complete refetch/review) before downgrade.
- **023** uses `CREATE INDEX` on `lower(name)` inside the migration transaction. That takes a **ShareLock** on `statute` and can block concurrent inserts until the build finishes. For large production tables, prefer running the index build **outside** a long transaction with `CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_statute_name_lower ON statute (lower(name));` (then record `023` in `schema_migrations` manually if you bypass the runner).
- **024** adds `canonical_name_key`, backfills **only rows where the key is NULL** in batches (safe to re-run), then builds `ix_statute_canonical_name_key` with **`CREATE INDEX CONCURRENTLY`** after the migration transaction commits. The `ALTER TABLE … ADD COLUMN` still takes a brief **ACCESS EXCLUSIVE** lock; batched `UPDATE`s use row-level locks (large tables: expect multi-second read/write contention during backfill). Plan a maintenance window for ~90k+ statute rows; do not run the old one-row-at-a-time backfill pattern on production.

## 3. Run reconcile (manual Celery task)

The reconcile task is **not** on Celery beat. After migrations succeed, run once (or on demand):

- Task name: `scraper.tasks.judgment_identity_reconcile.reconcile_judgment_identity_pairs`
- Queue: maintenance (same routing as other promotion/reconcile tasks)

Example (adjust broker/app as in your deployment):

```bash
celery -A scraper.tasks.celery_app call scraper.tasks.judgment_identity_reconcile.reconcile_judgment_identity_pairs
```

## 4. Resume harvest

Re-enable harvest after reconcile completes and you have verified link/review counts.
