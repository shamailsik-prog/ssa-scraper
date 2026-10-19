# Statute safety flags (B3)

Three rules protect the statute corpus. **Each is on by default** when the variable is unset. Set the env var to `0` or `false` only to turn a rule off (legacy behaviour).

| Variable | Default | When on |
|----------|---------|---------|
| `SSA_STATUTE_NO_MENTION_STUBS` | **on** | Instrument mentions resolve to existing statutes only; no new sectionless stub rows. |
| `SSA_STATUTE_PDF_FOLLOW` | **on** | Statute HTML ingest follows the first linked PDF when extracted text is longer than the listing page. |
| `SSA_STATUTE_NAME_QUALITY` | **on** | Promotion quarantines fragment/garbage names, missing `year_enacted`, and duplicate names (never guess year or merge). |

Listing-only captures are quarantined when `field_evidence.listing_only` is set (independent of the flags above).

Production category counts (read-only): `docs/statute_production_audit.sql`.

Dry-run stub/completeness report: `python -m scraper.tasks.statute_stub_audit`.
