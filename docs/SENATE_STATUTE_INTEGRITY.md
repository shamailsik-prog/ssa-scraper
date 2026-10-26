# Senate statute identity sweep

Some Senate-sourced statute rows disagree with the PDF currently served at `source_url`: the stored name, the stored section text, and the live PDF can be three different acts. A `source_url` of the form `{pdf_url}|{label}` is normal. The label is an act number (`XIII of 1985`) or a bill title used so shared PDF bytes do not collapse distinct acts. The label differing from `statute.name` is not, by itself, a mismatch.

Population:

- `source_url` host `senate.gov.pk` (any Senate upload)
- `source_url` host `pcp.gov.pk` when `source_name` is `Senate` or `GazetteOfPakistan`

On 2026-10-26 the read-only droplet counts were:

| Corpus | Senate-sourced rows | Already quarantined | Still visible |
| --- | --- | --- | --- |
| MAIN | 4,016 (3,931 `senate.gov.pk` + 85 `pcp.gov.pk` Gazette) | 3,622 | 394 |
| FREE-SITES | 3,824 `senate.gov.pk` | 3,337 | 487 |

Most quarantined rows are bill shells: a body-fragment name, a `|Bill title` label, and zero sections. The sweep counts those shells and does not fetch or relabel them.

## Dry-run first

Run a dry-run on each corpus and record the JSON `counts` (`MATCH`, `MISMATCH`, `PARTIAL`, `FETCH_FAIL`, `SKIPPED_SHELL`) plus `deferred_quarantined` and `population` before any `--apply`. Compare those counts with the table above: `population` should be near 4,016 on MAIN and 3,824 on FREE-SITES, and `SKIPPED_SHELL` plus `deferred_quarantined` should account for the already-quarantined majority. Apply only after those counts are written down.

```bash
python -m scraper.tasks.senate_statute_integrity
```

The default reads visible Senate-sourced rows, fetches the PDF (or recorded bytes if the fetch fails), and prints JSON. It does not write. A Gazette PDF that contains several acts is scored against the segment whose act number or short title matches the pipe label or the stored name. A row is `MISMATCH` only when no segment matches and the stored sections are not in the PDF.

Useful limits:

```bash
python -m scraper.tasks.senate_statute_integrity --limit 25
python -m scraper.tasks.senate_statute_integrity --statute-id <uuid>
python -m scraper.tasks.senate_statute_integrity --skip-fetch
python -m scraper.tasks.senate_statute_integrity --include-quarantined
```

`--skip-fetch` uses preserved PDF bytes only. `--include-quarantined` also classifies already-quarantined non-shell rows. Apply still does not relabel them.

Verdicts:

| Verdict | Meaning |
| --- | --- |
| `MATCH` | PDF short title agrees with the stored act, and spot-checked section text appears in the PDF. A scanned title that differs only by OCR confusions (`i`/`l`/`1`, `c`/`e`, digit lookalikes, `m`/`rn` in a longer word) still agrees. |
| `MISMATCH` | No Gazette segment matches the pipe label or stored name, and the stored sections are not in the PDF. An incoherent name plus missing sections is a mismatch. |
| `PARTIAL` | The matching PDF segment is the same instrument, but the stored name or a section is corrupt: preamble or definitions fragment, newline title, wrong year, year parsed as a section number, or a gazette running header such as `President's Orders and Regulations`. |
| `FETCH_FAIL` | No live PDF and no recorded PDF bytes. |
| `SKIPPED_SHELL` | Already quarantined, fragment name, pipe label, no sections. Not fetched. |

`deferred_quarantined` counts other already-quarantined rows skipped unless `--include-quarantined` is set.

## Apply

```bash
python -m scraper.tasks.senate_statute_integrity --apply
```

Record the dry-run counts for MAIN and FREE-SITES first. Only visible rows are written:

- `MISMATCH` inserts `document_quarantine` with reason `senate_integrity:url_act_mismatch`. `needs_section_refetch` stays false, because re-extracting would copy the wrong act into this row.
- `MATCH` is left unchanged. An OCR-near same-act title is `MATCH`, so `--apply` does not quarantine it and does not set `needs_section_refetch`. A different instrument stays `MISMATCH`, including ordinal amendments (Eighteenth against Nineteenth) and a wrong PDF such as Arms against Esso Vesting, Banking Companies against Benami, or Islamabad local government against Factories.
- Repairable `PARTIAL` sets `needs_section_refetch` and does not quarantine. The row stays servable. Nothing in this sweep rewrites the name or the section text; the flag marks the row for a later re-extract.
- A gazette header with no coherent act title is quarantined as `senate_integrity:partial_identity` and is not flagged for re-extract.
- `FETCH_FAIL`, `MATCH`, and shells are left unchanged.

Quarantine hides a mismatch from search and corpus reads. The statute row, sections, and source URL remain as evidence.

## Undo

```bash
python -m scraper.tasks.senate_statute_integrity --undo <run_id>
```

`run_id` is in the `--apply` JSON. Undo deletes quarantine rows from that run whose reason starts with `senate_integrity:`, and restores `needs_section_refetch` to the value saved before the run. Other quarantine reasons that share the run id are left in place. Undo does not delete statute or section rows.
