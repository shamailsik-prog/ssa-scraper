# Bundled gazette act bleed

Some gazette PDFs print more than one Act, each opened by an `ACT No.` line. The section splitter treated that file as one Act. Where section numbers collided, the earlier Act's text was kept and the target Act's section was dropped. A later Act's repeal could also land on the target row, and a gazette footer such as `under Article 59` could be stored as section 59.

Confirmed row:

| Corpus | Id prefix | Stored name | Source |
| --- | --- | --- | --- |
| FREE-SITES | `f200fa53` | The Service Tribunals Act, 1973 | `https://senate.gov.pk/uploads/documents/1660540467_508.pdf\|LXX of 1973` |

That PDF is three Acts: LXVII of 1973 (Life Insurance (Nationalisation) (Amendment)), LXX of 1973 (Service Tribunals), LXXI of 1973. Stored sections 2 and 3 are the LXVII text. Section 4 of the Service Tribunals Act is missing. Section 59 is the footer. Section 9 runs into LXXI.

New ingest isolates the segment whose act number matches the pipe label, or whose short title matches the target title, and splits only that segment. A line-start `Act No.` is a boundary only when `An Act` or `1. Short title` follows within four lines. The year may be the next line (`ACT No. LXXI` then `1973`), and a one-character garble such as `r97J` still counts: that year is the year the neighbouring headings share, or the pipe label's year when none of them print a readable year. A citation tail (`.-`, `In the`, `, in section`), including `Act No. LXX of 1973.- In the …`, is not a boundary, so the sections after that quotation stay with the amending Act. `may be called` and `shall be called` both supply the short title, including when the title breaks onto the next line. If the label and the title name two different segments, or neither matches, the extract stores no sections and promotion quarantines the staging row with `bundled_gazette_unresolved`. It does not set `needs_section_refetch`.

The same cut runs for Senate, the National Assembly, law.gok.pk, and any other text that has two or more `ACT No.` lines. A law.gok.pk PDF that has no second `ACT No.` line still uses the existing title-heading cut, which drops a later act and does not pull a middle act out of an earlier one.

## Already stored

This sweep reads stored sections against the PDF. It does not rewrite `statute.name` or section text. `--apply` only inserts `document_quarantine` for a visible mismatch.

Population: `senate.gov.pk`, `pcp.gov.pk` when `source_name` is Senate or GazetteOfPakistan, `law.gok.pk`, and `na.gov.pk`. Rows with no sections are counted and not fetched.

| Reason | When |
| --- | --- |
| `bundled_gazette_act_bleed` | An 80-character stretch of a stored section is found in one act segment, and another stored section is found in a different segment. |
| `bundled_gazette_act_wrong_segment` | Every located section sits in one segment, and that segment is not the pipe label or the stored title. |

A PDF with one `ACT No.` line is `NOT_BUNDLED`. A bundled PDF whose located sections all sit in the target segment is `MATCH`.

## Dry-run first

```bash
python -m scraper.tasks.bundled_gazette_acts
```

The default prints JSON and does not write. `--apply` stays off unless it is passed.

Re-prove FREE-SITES `f200fa53` before any write. Use the full UUID, not the prefix:

```bash
python -m scraper.tasks.bundled_gazette_acts --statute-id <uuid-for-f200fa53>
```

Expect `verdict` `MISMATCH`, `reason` `bundled_gazette_act_bleed`, and `signals` naming more than one `segment:<number>:<year>` (67 and 71 beside 70). `applied` is null. Do not add `--apply` on that command.

```bash
python -m scraper.tasks.bundled_gazette_acts --limit 25
python -m scraper.tasks.bundled_gazette_acts --skip-fetch
python -m scraper.tasks.bundled_gazette_acts --include-quarantined
```

`--skip-fetch` uses preserved public PDF bytes only. A missing file is `FETCH_FAIL` and is not a quarantine candidate. `--include-quarantined` classifies rows that already have a quarantine row. `--apply` does not replace that row.

Write down `population`, `counts`, and the mismatch `statute_id` values before any `--apply`.

## Apply

```bash
python -m scraper.tasks.bundled_gazette_acts --apply
```

Only a visible `MISMATCH` gets a new `document_quarantine` row. Section text stays as evidence. `needs_section_refetch` stays false, because a later promote onto the same row would replace numbers that appear again and would leave numbers that exist only because of the other act (section 59 on `f200fa53`). Running `--apply` twice does not insert a second row.

Quarantine hides the statute from search and corpus reads.

## What not to do with f200fa53

Do not treat `--apply` as a re-extract. After this code is deployed, a fresh extract with `act_title` `The Service Tribunals Act, 1973` or source URL `|LXX of 1973` keeps the LXX segment: Service Tribunals sections, not the Life Insurance amendment and not the LXXI repeal. Promoting that extract onto `f200fa53` still leaves a stored section whose number is absent from LXX, including the Article 59 footer. This sweep does not delete those leftover sections. Quarantine the row after the dry-run above, and leave it quarantined until a separate edit removes the leftovers.

## 86215821

MAIN `86215821` (AJK Conciliation Courts Act, 1962, flagged 2026-10-26) is the earlier title-heading bleed on law.gok.pk. It is already in `docs/GOK_MULTI_ACT_BLEED.md`. This sweep does not classify it unless the PDF text has two `ACT No.` lines. Re-prove it with the GoK command, still without `--apply` until that JSON is reviewed:

```bash
python -m scraper.tasks.gok_multi_act_bleed --statute-id <uuid-for-86215821>
```

Expect `multi_act_pdf_bleed:law.gok.pk`. Do not use `bundled_gazette_acts --apply` to change that row.

## Undo

```bash
python -m scraper.tasks.bundled_gazette_acts --undo <run_id>
```

`run_id` is in the `--apply` JSON. Undo deletes quarantine rows from that run whose reason starts with `bundled_gazette_act_`. Other reasons are left in place. Undo does not delete statute or section rows.
