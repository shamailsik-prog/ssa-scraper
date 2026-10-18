# SSA hardening layers (reversible)

| Layer | Flag(s) | Roll back |
| --- | --- | --- |
| PLS encoding/court/PLD/short-text guards + backfill CLIs | `SSA_ENCODING_GUARD`, `SSA_COURT_HEADER_GUARD`, `SSA_PLD_CITATION_RELABEL`, `SSA_SHORT_TEXT_GUARD` | Revert commit; flags off |
| Encoding: strip Urdu headnote `????`, re-fetch only if body damaged | `SSA_ENCODING_GUARD` (refined) | Flag off; revert this commit |
