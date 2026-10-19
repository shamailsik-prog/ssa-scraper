# SSA hardening layers (reversible)

| Layer | Flag | Roll back |
| --- | --- | --- |
| Test DB isolation (truncate+seed lock, court seed ON CONFLICT) | — (pytest only) | Revert commit |

Does not change production behaviour. Addresses flaky `uq_court_name` / `StaleDataError` on the shared test database.
