# Deploy smoke for #67 residual reconcile reduction

Thin operator smoke for citation/statute residuals after relation reconcile:

- Judgment citation graph residuals (`judgment_citation_relation.resolution_status='unresolved'`).
- Instrument statute-section residuals (`instrument_section_relation.target_statute_section_id IS NULL`).

This smoke is fail-closed and does not require secrets.

## 1) Inventory: reconcile jobs + unresolved metrics

| Area | Reconcile entry point | Existing run counters | Unresolved residual count used in smoke |
|---|---|---|---|
| Judgment citation edges | `scraper.tasks.treatment.reconcile_judgment_citation_relations` | `scanned`, `processed`, `failed`, `linked`, `ambiguous`, `unresolved`, edge deltas | `judgment_citation_relation.resolution_status='unresolved'` |
| Instrument relation + section edges | `scraper.tasks.promotion.reconcile_instrument_relations` | `scanned`, `processed`, `failed`, edge deltas, section-edge deltas | `instrument_section_relation.target_statute_section_id IS NULL` |

## 2) Pre-flight

```bash
cd /opt/ssa-scraper
docker compose ps
curl -fsS http://127.0.0.1:8000/health
```

## 3) Dry-run residual report (read-only)

```bash
python -c "import json; from scraper.database import run_async; from scraper.tasks.promotion import reconcile_citation_statute_residual_smoke; print(json.dumps(run_async(reconcile_citation_statute_residual_smoke(run_reconcile=False)), indent=2))"
```

Expected:

- `"mode": "dry_run"`
- `"before"` equals `"after"`
- all `*_reduced` deltas are `0`
- optional compact unresolved triage buckets:
  - `include_unresolved_breakdown=True`
  - `unresolved_breakdown_top_n=<small N>` (default `5`) for `by_source_name` and unresolved key buckets

## 4) Apply reconcile + before→after residual reduction

```bash
python -c "import json; from scraper.database import run_async; from scraper.tasks.promotion import reconcile_citation_statute_residual_smoke; print(json.dumps(run_async(reconcile_citation_statute_residual_smoke(run_reconcile=True, lookback_hours=168, instrument_limit=200, judgment_batch_size=200, fail_on_increase=True)), indent=2))"
```

Expected:

- `"runs"` contains both reconcile payloads:
  - `reconcile_instrument_relations`
  - `reconcile_judgment_citation_relations`
- unresolved residual counts in `"after"` are less than or equal to `"before"`.
- if any unresolved residual count increases, the command exits non-zero (fail-closed).

## 5) Optional focused rerun commands

If you need per-job reruns only:

```bash
python -c "from scraper.tasks.promotion import reconcile_instrument_relations; from scraper.database import run_async; print(run_async(reconcile_instrument_relations()))"
python -c "from scraper.tasks.treatment import reconcile_judgment_citation_relations; from scraper.database import run_async; print(run_async(reconcile_judgment_citation_relations()))"
```
