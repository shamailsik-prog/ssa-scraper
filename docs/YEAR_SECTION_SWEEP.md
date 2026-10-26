# Year-as-section sweep

Auditor 2026-10-26. Some stored `statute_section.section_number` values are years (`1860`, `2001`, `1976`, `1882`, `1898`), not headings. The parser split on a year inside the prose (`(Act XLV of 1860)`, `2001) dt. 16.12.2001`, `1930), hereinafter`) and saved the tail under that year. The rows are legacy: new splits no longer accept those headings.

This sweep is separate from Senate statute integrity and from any law.gok.pk multi-act bleed repair. It does not touch PLS reporter codes.

## What “repaired” means

For each affected statute, in section order (`sort_key`, then `created_at`):

- A section number matching `^(1[89]|20)\d{2}$` is a year-section.
- If a real (non-year) section precedes it, the year-section’s current text is appended to that real section as a **new** version. The previous version row stays.
- The year-section row is removed only after that copy. Its text is the new version, not discarded.
- A year-section with no previous real section is left unchanged (`skipped_no_previous`).
- `document_quarantine` is not inserted, updated, or deleted. A hidden statute stays hidden. Nothing is auto-released or unquarantined.

Re-extraction is not used. The statute row does not store the source PDF, and splitting again can drop the same tail.

## Commands

Dry-run (default, no writes). Run this first and read `by_host`:

```bash
python -m scraper.tasks.year_section_sweep
```

Write merges only after that dry-run looks right:

```bash
python -m scraper.tasks.year_section_sweep --apply
```

`--apply` is never the default. There is no flag that unquarantines.

`by_host` keys are the `source_url` hostname with a leading `www.` removed (`law.gok.pk`, `pabalochistan.gov.pk`, `senate.gov.pk`, `pakp.gov.pk`, and the rest). Each host has `statutes`, `year_sections`, `merged`, `skipped_no_previous`, and `skipped_empty`.

## Before any release or unquarantine

Do not release this repair, and do not unquarantine any statute, until a person has live-verified **10** repaired statutes.

For each of the 10, using ids from the dry-run `samples` list (spread them across hosts, not one source):

1. Open the statute in the corpus read API or the operator UI.
2. Confirm the year is gone as a section number (`1860`, `2001`, `1976`, and the same class).
3. Confirm the previous real section’s current text still contains the citation tail (`of 1860)`, `dt.`, or `hereinafter`).
4. If the statute was quarantined, confirm it is still quarantined. Unquarantine is a separate human decision after this check, never part of the sweep.

Stop if any of the 10 is missing the tail or still shows the year as a section. Do not run a second destructive pass; the merge keeps the prior version on the real section.

## Splitter tests

Rejection of year headings (citation context, and a number that does not continue the act’s section sequence) is covered by:

```bash
python -m pytest tests/test_year_section_splitter.py tests/test_year_section_sweep.py
```

The sequence rule matches the existing assembly-PDF jump: a 1800–2099 heading is kept only when some non-year section number in the same act is within 120, or the act is only a tight run of such headings. `of 1860)`, a heading that ends with `)`, and `dt.` are never headings, even when a nearby section would put the year inside that gap.
