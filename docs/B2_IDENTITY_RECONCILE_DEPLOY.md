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

Use the project’s normal migration runner (`run_migrations` / deploy script). Never downgrade 019 while `judgment_identity_link` or `judgment_identity_review` contain rows.

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
