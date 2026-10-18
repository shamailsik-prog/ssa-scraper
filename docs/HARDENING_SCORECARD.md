# SSA hardening scorecard (independent audit draft)

**Branch:** `cursor/gold-standard-hardening-58c5`  
**Supersedes:** folds #220 (`codex/ssa-9plus-remediation`); reader work from #213 is included in that fold.  
**Production:** scrapers remain stopped; no deploy, no secrets, no server changes in this PR.

## Scorecard (self-assessment — enable flags in staging to verify)

| Area | Before (audit baseline) | After (this PR) | Evidence |
| --- | --- | --- | --- |
| Stale harvest locks | Slot/query locks could survive deploy up to 1h TTL | Optional clear-on-start + heartbeat refresh | `scraper/pls_harvest_locks.py`, `tests/test_pls_harvest_locks.py` |
| Harvest completeness | Core rules on main + #220 scheduling | #220 Account-1 acquisition, coverage challenges, gap harvest rules | Merged #220 tests |
| Titles | Cited-case / empty titles | Order-sheet title module + backfill CLI (flagged) | `tests/test_order_sheet_title*.py` |
| Encoding | `????` Urdu loss | Mojibake detect + redecode helper + quarantine flag | `tests/test_pls_quality_guards.py` |
| court_name | Defaults vs header | Header court parse + mismatch guard (flagged) | `judgment_header_court.py` |
| PLD citations | Missing court token | Relabel at reconcile + backfill CLI | `pld_citation_relabel.py` |
| Short / duplicate text | Thin rows in corpus | Short-text quarantine flag (500 chars) | `pls_quality_guards.py` |
| Spot check | 30m turn lock, 3 judgments | Unchanged on main; #220 verifier slot | `tests/test_spot_check.py` |
| Dashboard | Limited source control | Pause/resume + enable/disable on Sources tab | `dashboard.html` |
| Statutes | Partial / quarantined | #220 tier-2 + requalify path; full core acts backlog | See #220 body |
| Deploy rebuild | `install.sh --deploy-only` builds | Unchanged; compose build on service-impacting diff | `cloud/install.sh` |
| Reader | Fragile upgrades | Version history, circuit breaker, privacy sink (#220 + guard) | `tests/test_unreported_reader.py` |
| Drive archive | 1.3k failed / mismatches | Read-only audit CLI | `archive_audit.py` |
| Secrets | Policy | No new logging; existing `guard_scan.sh` | CI |

## Rollback index

See [HARDENING_LAYERS.md](./HARDENING_LAYERS.md) — one git revert per layer.

## Remaining (not claimed done)

- Full corpus viewer (filters, paragraph sidebar, citation graph in UI) — incremental follow-up
- Encoding **re-fetch** queue wiring (guard only; backfill re-fetch script not run)
- Statute full-text completion for core acts (seed/backfill runbooks)
- Populate new droplet Drive archive table (operator migration)
- Enable hardening flags in production after audit

## Tests

CI: `pytest -q` (full suite). Layer-focused: `tests/test_pls_harvest_locks.py`, `tests/test_order_sheet_title*.py`, `tests/test_pls_quality_guards.py`, `tests/test_archive_audit.py`, `tests/test_unreported_reader.py`, `tests/test_dispatcher.py`.
