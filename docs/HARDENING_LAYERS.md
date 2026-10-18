# SSA hardening layers (reversible, one commit each)

Each layer is behind an environment flag defaulting to **off** so production behaviour stays unchanged until an operator enables it. Roll back by reverting the layer’s commit or setting the flag to `0`/`false`.

| Layer | Commit (branch) | Flag(s) | Roll back |
| --- | --- | --- | --- |
| Fold #220 corpus completeness + reader (#213 superseded) | `merge: fold codex/ssa-9plus-remediation` | (always on when merged) | Revert merge commit |
| Test alignment for Account-1 acquisition | `test: align reader mocks and dispatcher` | — | Revert commit |
| Harvest lock clear + heartbeat | `layer: harvest lock hygiene` | `SSA_CLEAR_HARVEST_LOCKS_ON_START`, `SSA_HARVEST_LOCK_HEARTBEAT` | Revert commit; flags off |

Auditors: enable flags in staging, run the cited tests, then run backfill CLIs with `--dry-run` before any production data pass.
