# SSA hardening layers (reversible)

| Layer | Flag | Roll back |
| --- | --- | --- |
| Reported/unreported dispositive reconciliation guard | `SSA_REPORTED_UNREPORTED_DISPOSITIVE_GUARD` | Set flag off or revert the guard commit |

When the flag is off, reconciliation keeps the pre-guard similarity-only behaviour. When on, automatic merges require compatible outcome and monetary signals.
