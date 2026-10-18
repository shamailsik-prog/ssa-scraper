# SSA hardening layers (reversible)

| Layer | Flag | Roll back |
| --- | --- | --- |
| Do not insert sectionless statute stubs from instrument mentions | `SSA_STATUTE_NO_MENTION_STUBS` | Flag off restores legacy stub inserts; revert commit |
| Follow linked statute PDFs and prefer extracted act text over listing chrome | `SSA_STATUTE_PDF_FOLLOW` | Flag off; revert commit |

Auditor baseline (2026-10-18): 10,303 of 12,618 statutes have no sections. A statute counts as complete only when it has at least one section with body text. Dry-run: `python -m scraper.tasks.statute_stub_audit`.
