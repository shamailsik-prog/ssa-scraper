# SSA hardening layers (reversible, one commit each)

Each layer is behind an environment flag defaulting to **off** so production behaviour stays unchanged until an operator enables it. Roll back by reverting the layer’s commit or setting the flag to `0`/`false`.

| Layer | Commit (branch) | Flag(s) | Roll back |
| --- | --- | --- | --- |
| Fold #220 corpus completeness + reader (#213 superseded) | `merge: fold codex/ssa-9plus-remediation` | (always on when merged) | Revert merge commit |
| Test alignment for Account-1 acquisition | `test: align reader mocks and dispatcher` | — | Revert commit |
| Harvest lock clear + heartbeat | `layer: harvest lock hygiene` | `SSA_CLEAR_HARVEST_LOCKS_ON_START`, `SSA_HARVEST_LOCK_HEARTBEAT` | Revert commit; flags off |
| Order-sheet case titles + backfill CLI | `layer: order-sheet titles` | `SSA_ORDER_SHEET_TITLE` | Revert commit; flag off; titles unchanged on new promotes |
| PLS encoding/court/PLD/short-text guards | `layer: PLS quality guards` | `SSA_ENCODING_GUARD`, `SSA_COURT_HEADER_GUARD`, `SSA_PLD_CITATION_RELABEL`, `SSA_SHORT_TEXT_GUARD` | Revert commit; flags off |
| PLD citation backfill CLI | (same PR commit or next) | — | `pls_admin backfill-pld-citations --dry-run` |
| Archive/Drive audit summary | `layer: archive audit CLI` | — | `python -m scraper.tasks.archive_audit` |
| Dashboard source on/off | `layer: dashboard source toggles` | — | Revert commit |
| Fold #220 completeness / reader | merge commit | (feature set in #220) | Revert merge commit |

Auditors: enable flags in staging, run the cited tests, then run backfill CLIs with `--dry-run` before any production data pass.
