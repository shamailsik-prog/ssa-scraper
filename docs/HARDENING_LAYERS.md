# SSA hardening layers (reversible)

Judgment-content and PLS quality layers default to **on** (same policy as B3 statute safety). Disable only by setting the flag to `0` or `false` in the environment. Roll back by reverting the layer’s commit or opting out via env.

| Layer | Flag(s) | Roll back |
| --- | --- | --- |
| Order-sheet case titles + backfill CLI | `SSA_ORDER_SHEET_TITLE` | Revert commit; `SSA_ORDER_SHEET_TITLE=0` |
| PLS encoding/court/PLD/short-text guards + backfill CLIs | `SSA_ENCODING_GUARD`, `SSA_COURT_HEADER_GUARD`, `SSA_PLD_CITATION_RELABEL`, `SSA_SHORT_TEXT_GUARD` | Revert commit; set affected flags to `0` |
| Encoding: strip Urdu headnote `????`, re-fetch only if body damaged | `SSA_ENCODING_GUARD` (refined) | `SSA_ENCODING_GUARD=0`; revert encoding commit |
| Urdu headnote flag column + strip CLI | `quality_flag_urdu_headnote_mojibake` on `judgment` | `pls_admin strip-urdu-headnote-mojibake` (dry-run default; `--apply` to write) |

**Deferred (out of scope for B4):** harvest lock clear on worker start (`SSA_CLEAR_HARVEST_LOCKS_ON_START`) and harvest lock heartbeat (`SSA_HARVEST_LOCK_HEARTBEAT`) — tracked separately; not shipped on this branch (no runtime code).

**Backfill / maintenance CLIs** (`pls_admin backfill-metadata`, `backfill-order-sheet-titles`, `backfill-pld-citations`, `strip-urdu-headnote-mojibake`, `requalify-quarantine`): default is **dry-run** (no writes). Pass **`--apply`** to commit changes. Manual operator runs only; not part of deploy.

Auditors: confirm flags in staging (defaults on), run the cited tests, then run backfill CLIs without `--apply` before any production data pass.
