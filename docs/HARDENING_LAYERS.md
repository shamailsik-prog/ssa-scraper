# SSA hardening layers (reversible)

Each layer is behind an environment flag defaulting to **off** so production behaviour stays unchanged until an operator enables it. Roll back by reverting the layer’s commit or setting the flag to `0`/`false`.

| Layer | Flag(s) | Roll back |
| --- | --- | --- |
| Harvest lock clear + heartbeat | `SSA_CLEAR_HARVEST_LOCKS_ON_START`, `SSA_HARVEST_LOCK_HEARTBEAT` | Revert commit; flags off |
| Order-sheet case titles + backfill CLI | `SSA_ORDER_SHEET_TITLE` | Revert commit; flag off |
| PLS encoding/court/PLD/short-text guards + backfill CLIs | `SSA_ENCODING_GUARD`, `SSA_COURT_HEADER_GUARD`, `SSA_PLD_CITATION_RELABEL`, `SSA_SHORT_TEXT_GUARD` | Revert commit; flags off |
| Encoding: strip Urdu headnote `????`, re-fetch only if body damaged | `SSA_ENCODING_GUARD` (refined) | Flag off; revert encoding commit |

Auditors: enable flags in staging, run the cited tests, then run backfill CLIs with `--dry-run` before any production data pass.
