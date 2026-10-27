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

The default reads visible Senate-sourced rows, fetches the PDF (or recorded bytes if the fetch fails), and prints JSON. It does not write. A Gazette PDF that contains several acts is scored against the segment whose act number or short title matches the pipe label or the stored name. Run this dry-run, with `--skip-fetch` when the check should use recorded bytes only, and write the JSON `counts` down before any `--apply`. `--apply` stays off unless it is passed. `--skip-fetch` stays off unless it is passed.

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
| `MATCH` | PDF short title agrees with the stored act, including a scanned title that differs only by OCR confusions (`i`/`l`/`1`, `c`/`e`, digit lookalikes, `m`/`rn` in a longer word). An act-number gap is ignored only when three things are all true: the significant words match in both directions under those OCR confusions (not a subset or a 0.55 overlap), both titles carry the same year, and the act numbers share that year and are either one apart (roman 30 against 31) or the same X-run with its final X read as III (roman 30 against 23, roman 20 against 13). That both-directions check also treats an `n`/`r` slip in a word of nine or more letters as the same word (`scientific` / `sciertific`, including `sclertific`), so a garbled PCSIR title with an agreeing year stays `MATCH`. An 8-letter pair such as `national` / `rational` does not. `Amendment` and ordinal words such as `Second` stay significant, so a principal Act against its Amendment stays `MISMATCH`. `year_as_section_number` is still reported and does not change `MATCH` when that wording already agrees. Renaming those section numbers is a separate sweep. |
| `MISMATCH` | Forced by `pdf_title_disagrees`, by `name_pdf_mismatch`, by `act_number_disagrees` unless the one-numeral or X-run exception above applies, or by `section_absent_from_pdf` together with an incoherent stored name. A missing year (`Finance Act` against `Finance Act, 2016`), a different year (`Finance Act, 2015` against `2016`), a different act-number year, or any other act-number gap on an otherwise identical title still forces `MISMATCH`. `name_pdf_mismatch` is the Senate form of `name_pdf_mismatch:law.gok.pk`: on a PDF that prints one act, the stored name is a different instrument from the short-title line, or no short title was read and the stored year disagrees with `Act No. … of YYYY`. Section text that is present in that PDF does not keep the row `MATCH`. `year_as_section_number` does not force `MISMATCH`. |
| `PARTIAL` | The stored name or a section is corrupt and the short title does not already agree: preamble or definitions fragment, newline title, wrong year, year parsed as a section number with no agreeing title, or a gazette running header such as `President's Orders and Regulations`. |
| `FETCH_FAIL` | No live PDF and no recorded PDF bytes. |
| `SKIPPED_SHELL` | Already quarantined, fragment name, pipe label, no sections. Not fetched. |

`deferred_quarantined` counts other already-quarantined rows skipped unless `--include-quarantined` is set.

## Sole-act name and year

law.gok.pk reads the instrument out of the PDF filename. Senate upload names are numeric (`1656581759_353.pdf`), so this sweep reads the gazette instead. A PDF with one act is `name_pdf_mismatch` when either of these is true:

- The stored name is a coherent act title and the short-title line (`This Act may be called …`) names a different instrument. `pdf_title_disagrees` is set as well.
- No short title was read, and the year in the stored name (or `year_enacted` when the name has no year) disagrees with `Act No. … of YYYY`. `gazette_act_year_disagrees` is set as well.

The stored sections being the PDF's own text does not clear the signal. Quarantine reason stays `senate_integrity:url_act_mismatch`. `--apply` is unchanged: it inserts that reason and does not rewrite the name or the sections. `needs_section_refetch` stays false.

These stay off this signal:

- A bundled gazette, two different `Act No.` years or numbers. That sweep is `python -m scraper.tasks.bundled_gazette_acts`. See `docs/BUNDLED_GAZETTE_ACTS.md`.
- An OCR-same short title, including a garbled PCSIR title whose year agrees. A one-numeral or X-run act-number misread on that title stays `MATCH`.
- A year stored as a section number. Renaming it is the year-section sweep.
- GoK filename bleed, including the Bhimber short-title OCR case, which stays on `python -m scraper.tasks.gok_multi_act_bleed`.
- OCR section mash, and a `DD.MM.YYYY` heading stored as a section number.

An amendment whose significant words are a superset of the stored title (`Credit Bureaus Act` inside `Credit Bureaus (Amendment) Act`) is not `name_pdf_mismatch`. It stays on the existing act-number path. Do not treat a particular amendment-bill UUID as a victim of this check without reading that PDF.

### Re-prove MAIN 12977c9f without --apply

Stored name `Companies Ordinance, 1984`, year 1984, source `https://senate.gov.pk/uploads/documents/1656581759_353.pdf`. The gazette is Act No. XI of 2015, short title Credit Bureaus Act, 2015. Sections 2–42 are that Act's text. Ops quarantine of this row is outside this change.

```bash
python -m scraper.tasks.senate_statute_integrity --statute-id 12977c9f-9d72-4f85-b5ae-14997b22859f
```

Do not pass `--apply`. Expect `dry_run` true, `applied` null, `verdict` `MISMATCH`, `reason` `senate_integrity:url_act_mismatch`, and `signals` containing `name_pdf_mismatch`, `pdf_title_disagrees`, and `gazette_act_year_disagrees`. `needs_reextract` is false.

## Apply

```bash
python -m scraper.tasks.senate_statute_integrity --apply
```

Record a fresh dry-run for MAIN and FREE-SITES first (`python -m scraper.tasks.senate_statute_integrity --skip-fetch` when using recorded bytes). Do that again after any compare change, before `--apply`. Only visible rows are written:

- `MISMATCH` inserts `document_quarantine` with reason `senate_integrity:url_act_mismatch`. `needs_section_refetch` stays false, because re-extracting would copy the wrong act into this row.
- `MATCH` is left unchanged. An OCR-near same-act title is `MATCH`, so `--apply` does not quarantine it and does not set `needs_section_refetch`. A one-numeral act-number misread (roman 30 against 31) or an X-run whose final X was read as III (roman 30 against 23, roman 20 against 13) on that title is also left unchanged, including when a section number is a year. A different instrument stays `MISMATCH`, including a principal Act against its Amendment, Amendment against Second Amendment, a year-less title against a titled year, ordinal amendments (Eighteenth against Nineteenth), Finance Act, 2015 against 2016, and a wrong PDF such as Arms against Esso Vesting, Banking Companies against Benami, or Islamabad local government against Factories.
- Repairable `PARTIAL` sets `needs_section_refetch` and does not quarantine. The row stays servable. Nothing in this sweep rewrites the name or the section text; the flag marks the row for a later re-extract.
- A gazette header with no coherent act title is quarantined as `senate_integrity:partial_identity` and is not flagged for re-extract.
- `FETCH_FAIL`, `MATCH`, and shells are left unchanged.

Quarantine hides a mismatch from search and corpus reads. The statute row, sections, and source URL remain as evidence.

## Undo

```bash
python -m scraper.tasks.senate_statute_integrity --undo <run_id>
```

`run_id` is in the `--apply` JSON. Undo deletes quarantine rows from that run whose reason starts with `senate_integrity:`, and restores `needs_section_refetch` to the value saved before the run. Other quarantine reasons that share the run id are left in place. Undo does not delete statute or section rows.
