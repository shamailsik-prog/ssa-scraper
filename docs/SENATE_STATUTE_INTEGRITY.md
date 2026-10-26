# Senate statute identity sweep

Some Senate-sourced statute rows disagree with the PDF currently served at `source_url`: the stored name, the stored section text, and the live PDF can be three different acts. A `source_url` of the form `{pdf_url}|{label}` is normal. The label is an act number (`XIII of 1985`) or a bill title used so shared PDF bytes do not collapse distinct acts. The label differing from `statute.name` is not, by itself, a mismatch.

Population:

- `source_url` host `senate.gov.pk` (any Senate upload)
- `source_url` host `pcp.gov.pk` when `source_name` is `Senate` or `GazetteOfPakistan`

On 2026-10-26 the droplet held about 4,016 such rows on MAIN (3,931 senate.gov.pk + 85 Gazette) and 3,824 senate.gov.pk rows on FREE-SITES. Most are already in `document_quarantine`. The common quarantined shell is a body-fragment name, a `|Bill title` label, and zero sections. The sweep counts those shells and does not fetch or relabel them.

## Dry-run (default)

```bash
python -m scraper.tasks.senate_statute_integrity
```

Reads visible Senate-sourced rows, fetches the PDF (or recorded bytes if the fetch fails), and prints JSON. It does not write.

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
| `MATCH` | PDF short title agrees with the stored act, and spot-checked section text appears in the PDF. |
| `MISMATCH` | The PDF is a different act, or the `\|XIII of 1985` label disagrees with `ACT No.` in the PDF. |
| `PARTIAL` | Identity is corrupt but the PDF is still the same instrument: preamble or definitions fragment, newline/truncated title, wrong year, year parsed as a section number, or a gazette running header such as `President's Orders and Regulations`. |
| `FETCH_FAIL` | No live PDF and no recorded PDF bytes. |
| `SKIPPED_SHELL` | Already quarantined, fragment name, pipe label, no sections. Not fetched. |

`deferred_quarantined` counts other already-quarantined rows skipped unless `--include-quarantined` is set.

## Apply

```bash
python -m scraper.tasks.senate_statute_integrity --apply
```

Only visible rows are written:

- `MISMATCH` inserts `document_quarantine` with reason `senate_integrity:url_act_mismatch`. `needs_section_refetch` stays false, because re-extracting would copy the wrong act into this row.
- Repairable `PARTIAL` inserts `document_quarantine` with reason `senate_integrity:partial_needs_reextract` and sets `needs_section_refetch`. That is the re-extract flag. The sweep does not replace the name or section text.
- A gazette header with no coherent act title is quarantined as `senate_integrity:partial_identity` and is not flagged for re-extract.
- `FETCH_FAIL`, `MATCH`, and shells are left unchanged.

Quarantine hides the row from search and corpus reads. The statute row, sections, and source URL remain as evidence.

## Undo

```bash
python -m scraper.tasks.senate_statute_integrity --undo <run_id>
```

`run_id` is in the `--apply` JSON. Undo deletes quarantine rows from that run and restores `needs_section_refetch` to the value saved before the run. It does not delete statute or section rows.
