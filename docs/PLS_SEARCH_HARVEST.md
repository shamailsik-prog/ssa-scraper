# PakistanLawSite search-driven gap harvest

The citation-grid harvester walks `#archivedpatientGrid` with absolute row offsets. That path does not enumerate judgments reachable only through the CitationSearch query form. The search harvester plans systematic queries (reporter × year, then court and keyword splits when the site reports more than `PLS_SEARCH_RESULT_CAP` hits), runs one query at a time under the login-session lock, paginates every result page, and records per-query totals in `pls_search_harvest_query`.

## Environment

| Variable | Default | Purpose |
|----------|---------|---------|
| `PLS_SEARCH_HARVEST_ENABLED` | `0` | Set to `1` to add `pls-search-harvest` to Celery Beat (not enabled by default). |
| `PLS_SEARCH_HARVEST_SCHEDULE_SECONDS` | `3600` | Beat interval when enabled. |
| `PLS_SEARCH_RESULT_CAP` | `500` | Observed CitationSearch cap; larger totals trigger automatic query splits. |
| `PLS_SEARCH_HARVEST_MAX_PAGES_PER_QUERY` | `0` | Pages per runner invocation (`0` = all pages). |
| `PLS_BROWSER_RECYCLE_WINDOWS` | `12` | Recycle Playwright after this many pages/windows. |
| `PLS_BROWSER_RSS_LIMIT_MB` | `900` | Soft RSS limit; recycle browser. |
| `PLS_BROWSER_RSS_HARD_MB` | `1200` | Hard RSS limit; stop run cleanly. |
| `PLS_BLOCK_HEAVY_RESOURCES` | `true` | Block image/font/media in Playwright. |

Requires the same human-login session, search form map, and `ALLOW_LOGIN_SCRAPING` / `ENVIRONMENT=chambers` rules as the main PLS harvest.

## CLI (operator)

Seed the plan (idempotent):

```bash
python -m scraper.tasks.pls_admin search-harvest-plan
python -m scraper.tasks.pls_admin search-harvest-plan --dry-run
```

Gap report (JSON):

```bash
python -m scraper.tasks.pls_admin search-harvest-gap-report --limit 100
```

Run one query (full harvest uses the same `preserve_and_extract` path as the main connector):

```bash
python -m scraper.tasks.pls_admin search-harvest-run --priority-gaps
python -m scraper.tasks.pls_admin search-harvest-run --dry-run --max-pages 2 --query-key 'search:reporter=PLD|year=2024'
```

HTTP gap report (read-only, same as `/status`):

`GET /pls-search-harvest/gap-report.json`

## Live smoke (one query, page count)

1. Ensure an ACTIVE browser slot and a current `search_form_map` for `PakistanLawSite`.
2. Seed plan: `python -m scraper.tasks.pls_admin search-harvest-plan`
3. Dry-run one query with a page cap:

   ```bash
   python -m scraper.tasks.pls_admin search-harvest-run --dry-run --max-pages 1 --priority-gaps
   ```

4. Confirm JSON output includes `pages_enumerated`, `rows_seen`, and `site_total_results` when the site prints a total.
5. Remove `--dry-run` only after reviewing pacing and slot health.

## Site limits

- **Result cap:** `PLS_SEARCH_RESULT_CAP` (default 500) — when the results page shows a higher total, the parent query is marked `split` and child queries are enqueued (court options from the mapped form, then single-letter keyword prefixes).
- **Pagination:** All pages are followed via the mapped `next_selector` or extracted `next_page` URL until exhausted or `PLS_SEARCH_HARVEST_MAX_PAGES_PER_QUERY` is reached.
- **No CAPTCHA bypass:** Verification or login surfaces halt the run like the main PLS pipeline.

## Celery

Task: `scraper.tasks.pls_search_harvest.pls_search_harvest_tick` on the `login_session` queue. Beat entry `pls-search-harvest` is registered only when `PLS_SEARCH_HARVEST_ENABLED=1`.
