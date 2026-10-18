# SSA hardening scorecard (2026-10-18)

**Production:** scrapers remain stopped. No deploys, no secrets, no server changes in these PRs.

**Independent audits this cycle**
- Forensic ranking of `codex/ssa-9plus-remediation` vs `main`: provisional **5.8/10**, not production-certified. Critical: false reconciliation of distinct judicial orders.
- Live DB audit (read-only): **90,039** judgments (partial pass), **12,618** statutes (**FAIL** — 10,303 have no sections).

This scorecard is **self-assessment of open layered PRs**, not a recertification. Flags default **off**. Nothing here is proven on production data until an operator enables flags in staging and runs the dry-run CLIs.

## Whole-system score

| | Score | Notes |
| --- | --- | --- |
| Auditor carry-forward | **5.8 / 10** | Unchanged as a certified number. Critical merge risk is **patched in #228 but off by default**. Statute corpus is still a fail. |
| If #228 guard is enabled in staging and regression tests stay green | ~6.4 | Identity decision can refuse opposite outcomes; destructive merge still exists for allowed pairs. |
| Path to 9.0+ | Not claimed | Requires: flag-on verification of #228, statute completeness for core acts, independent adversarial re-audit, green `pytest -q` on CI, no unresolved high-severity defects. |

## Judgment findings (auditor 2026-10-18)

| Finding | Count | Layer / PR | Status |
| --- | --- | --- | --- |
| False reported/unreported merges | — | #228 dispositive guard + SQL docket match | Code + tests; **flag off** |
| No title (mostly SHC) | 6,048 | #230 order-sheet titles | Open; flag off |
| Text under 500 chars | 134 | #231 `SSA_SHORT_TEXT_GUARD` | Open; harvest now forwards `source_name` |
| Malformed PLD citations | 187 | #231 `SSA_PLD_CITATION_RELABEL` | Open |
| `????` runs | 6,708 | #231 + #235 (headnote vs body; no blind re-fetch) | Open; stacked |
| SC label / HC header | 20 | #231 `SSA_COURT_HEADER_GUARD` | Open |
| `Case Description` chrome | 115 | Strip exists at promote; **backfill CLI not shipped this round** | Residual |
| No decision date | 381 | **Not shipped this round** | Residual |
| Archive mismatch/failed | ~8,600 | #232 read-only audit CLI | Open |
| Duplicate citations / empty texts | 0 | Keep it that way | Hold |

## Statute findings (FAIL)

| Finding | Count | Layer / PR | Status |
| --- | --- | --- | --- |
| No sections (stubs from instrument mentions) | 10,303 / 12,618 | #234 `SSA_STATUTE_NO_MENTION_STUBS` + dry-run `statute_stub_audit` | Open; **does not delete existing stubs** |
| Act text only in PDFs | — | #236 `SSA_STATUTE_PDF_FOLLOW` | Open; stacked on #234 |
| Fragment / garbage names | ~1/3 of names; JPEG bytes | #237 `SSA_STATUTE_NAME_QUALITY` | Open; stacked on #234 |
| No year | 588 | Report only in stub audit | Residual |
| Duplicate names | 79 | Report only | Residual |
| Core acts incomplete | sample 18/20 stubs | Stub audit lists Constitution, PPC, CrPC, CPC, Customs, Income Tax, Sales Tax, Qanun-e-Shahadat, NAB | Residual until PDF follow + PakistanCode harvest with flags on |

## Tests

- Layer subsets were run locally and are cited on each PR.
- Shared-DB flakes (`uq_court_name`, `StaleDataError`): mitigation in `cursor/test-db-isolation-58c5` (court seed `ON CONFLICT`, no pool dispose at teardown). **Full `pytest -q` is not claimed green in this cycle.**

## Rollback

Each layer: flag off or revert that PR. See `docs/HARDENING_LAYERS.md` on the individual branches.
