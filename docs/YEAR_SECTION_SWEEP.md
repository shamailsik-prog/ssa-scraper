# Year-as-section sweep

Auditor 2026-10-26. Some stored `statute_section.section_number` values are years (`1860`, `2001`, `1976`, `1882`, `1898`), not headings. The parser split on a year inside the prose (`(Act XLV of 1860)`, `2001) dt. 16.12.2001`, `1930), hereinafter`) and saved the tail under that year. The rows are legacy: new splits no longer accept those headings.

This sweep is separate from Senate statute integrity and from any law.gok.pk multi-act bleed repair. It does not touch PLS reporter codes.

## What “repaired” means

For each affected statute, in section order (`sort_key`, then `created_at`):

- A section number matching `^(1[89]|20)\d{2}$` is a year-section.
- It is merged only when the splitter would reject it: the text is a citation (`of 1860)`, a heading that ends with `)`, `dt.`, or the previous section was cut after `(` / `of`), or the number does not continue this act’s section sequence (more than 120 away from a non-year section, unless the act is only a tight run of such headings).
- `skipped_in_sequence` means the year-shaped number continues the act (1798, 1799, 1800, 1801). Those rows are left in place. `--apply` does not merge or delete them.
- A merged year-section’s current text is appended to the previous kept section as a **new** version. The previous version row stays.
- Before the year row is removed, `instrument_section_relation.target_statute_section_id` values that pointed at it are re-pointed at the merge target, and the target section is set back to `pending` on `embedding_queue`.
- The removed year text is written to `corpus_metadata` under `year_section_sweep_log:<run_id>` (`year_section_sweep_latest_run_id` names the latest run). That log is the record of what `--apply` removed.
- A bogus year-section with no previous real section is left unchanged (`skipped_no_previous`).
- `document_quarantine` is not inserted, updated, or deleted. A hidden statute stays hidden. Nothing is auto-released or unquarantined.

Re-extraction is not used. The statute row does not store the source PDF, and splitting again can drop the same tail.

## Commands

Dry-run (default, no writes). Run it against **both** databases and read `by_host` on each before any `--apply`:

1. MAIN
2. FREE-SITES

A dry-run from one database does not describe the other. `samples` are round-robin by host so one source cannot fill the list.

```bash
python -m scraper.tasks.year_section_sweep
```

Write merges on that same database only after its own dry-run looks right:

```bash
python -m scraper.tasks.year_section_sweep --apply
```

`--apply` is never the default. There is no flag that unquarantines.

`by_host` keys are the `source_url` hostname with a leading `www.` removed (`law.gok.pk`, `pabalochistan.gov.pk`, `senate.gov.pk`, `pakp.gov.pk`, `pas.gov.pk`, and the rest). Each host has `statutes`, `year_sections`, `merged`, `skipped_no_previous`, `skipped_empty`, and `skipped_in_sequence`.

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
