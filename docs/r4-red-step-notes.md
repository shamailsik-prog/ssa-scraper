# R4 red-step notes (baseline `6790129`)

Demonstration: compared behavior of new tests against `6790129` implementations of `pls_backfill.py` / `pls_text_encoding.py`.

| Test | On `6790129` | On R4 fix |
|------|----------------|-----------|
| `test_pld_backfill_collides_with_non_pls_canonical_holder` | `IntegrityError` on `uq_judgment_canonical_citation` | `collisions==1`, `updated==0` |
| `test_flag_strip_three_cycles_no_reflag` | cycle 2 `flagged>=1` after no-op strip cleared flag | cycles 2–3 `flagged==0` |
| `test_pld_backfill_canonical_only_row_collides_with_expanded_target` | collision not detected (`updated>=1` or crash) | `collisions>=1`, `updated==0` |

Green: `pytest tests/test_pls_backfill_b4.py::test_pld_backfill_collides_with_non_pls_canonical_holder tests/test_pls_backfill_b4.py::test_flag_strip_three_cycles_no_reflag` → 2 passed on fixed branch.
