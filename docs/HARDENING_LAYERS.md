# SSA hardening layers (reversible)

Judgment-content and PLS quality layers default to **on** (same policy as B3 statute safety). Disable only by setting the flag to `0` or `false` in the environment. Roll back by reverting the layer’s commit or opting out via env.

| Layer | Flag(s) | Roll back |
| --- | --- | --- |
| Order-sheet case titles + backfill CLI | `SSA_ORDER_SHEET_TITLE` | Revert commit; `SSA_ORDER_SHEET_TITLE=0` |
| PLS encoding/court/PLD/short-text guards + backfill CLIs | `SSA_ENCODING_GUARD`, `SSA_COURT_HEADER_GUARD`, `SSA_PLD_CITATION_RELABEL`, `SSA_SHORT_TEXT_GUARD`, `SSA_PLS_COURT_RESOLUTION` | Revert commit; set affected flags to `0` |
| Encoding: strip Urdu headnote `????`, re-fetch only if body damaged | `SSA_ENCODING_GUARD` (refined) | `SSA_ENCODING_GUARD=0`; revert encoding commit |
| Urdu headnote flag column + strip/flag CLIs | `quality_flag_urdu_headnote_mojibake`, `headnote_stripped_full_text` on `judgment` | `backfill-flag-urdu-headnote-mojibake` then `strip-urdu-headnote-mojibake` (`--apply` writes overlay only; `full_text` hash unchanged) |
| Search harvest Postgres advisory lock (per slot) | `SSA_HARVEST_PG_LOCK` | Revert commit; `SSA_HARVEST_PG_LOCK=0` |
| Document save validation + document_quarantine CLI | `SSA_DOCUMENT_SAVE_VALIDATION`, migration **027** (`document_quarantine` side table), `python -m scraper.tasks.quarantine_bad_documents` | Revert commit; `SSA_DOCUMENT_SAVE_VALIDATION=0`; `--undo RUN_ID` on quarantine CLI |
| Senate statute identity sweep | `python -m scraper.tasks.senate_statute_integrity` (dry-run). `--apply` quarantines visible mismatches only | `--undo RUN_ID` removes that run's quarantine rows and restores `needs_section_refetch`. See `docs/SENATE_STATUTE_INTEGRITY.md` |

**Deferred (out of scope for B4):** harvest lock clear on worker start (`SSA_CLEAR_HARVEST_LOCKS_ON_START`) and harvest lock heartbeat (`SSA_HARVEST_LOCK_HEARTBEAT`) — tracked separately; not shipped on this branch (no runtime code).

**Backfill / maintenance CLIs** (`pls_admin backfill-metadata`, `backfill-order-sheet-titles`, `backfill-pld-citations`, `backfill-judgment-courts`, `strip-urdu-headnote-mojibake`, `requalify-quarantine`): default is **dry-run** (no writes). Pass **`--apply`** to commit changes; `backfill-judgment-courts --undo RUN_ID` restores prior `court_name` and `court_id` (no deletes). `--consistency-report` / `judgment-court-consistency-report` list `court_id` vs `court_name` mismatches. Apply repoints `court_id` when `SSA_JUDGMENT_COURT_ID_BACKFILL=1`. Manual operator runs only; not part of deploy.

**Migrations 025–026 (B4):** each upgrade/downgrade sets `SET LOCAL lock_timeout = '2s'`. If DDL hits `lock_timeout`, retry the migration after harvest drain (same pattern as `docs/B2_IDENTITY_RECONCILE_DEPLOY.md` §2).

Auditors: confirm flags in staging (defaults on), run the cited tests, then run backfill CLIs without `--apply` before any production data pass.
