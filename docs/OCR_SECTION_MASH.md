# OCR section-mash sweep

Some visible statutes built from a scanned notification PDF (no text layer) keep OCR page or line numbers as `section_number`. The real `N. Title.—` headings stay inside a low-numbered row, and most of the act is missing.

FREE-SITES `8d5b87b6`, The Balochistan Institute of Child Health Services Quetta Act, Act No. XXI of 2025 (pabalochistan.gov.pk, created 2026-10-12), is the reference case. It is stored as sections 3, 8, 10 and 11. Stored s.11 holds the text of ss.28–30. That row is a mismatch until a re-extract stores the real section numbers. The sweep must keep classifying that shape as a mismatch.

This sweep reads stored sections. When a statute has very few sections it also reads a preserved PDF, if one is on disk, and asks only whether the text layer is missing. It does not download PDFs. It does not OCR. It does not rewrite `section_number` or section text. A bare page number, `Page N of M`, `- N -`, or a line that is only `N.` is not a section number.

Senate-sourced rows are skipped. MAIN `648084fb` (PCSIR 1973) and FREE-SITES `97bcaece` (IST Bahawalpur 2018) are the Senate OCR-garble partial class and stay on `python -m scraper.tasks.senate_statute_integrity`. This sweep does not classify them.

MAIN `09cac1a4` (AJK Industrial Statistics 1975, seven sections whose headings match the stored numbers) is match-likely and is not a suspect.

On 2026-10-26 an auditor's rough count was about 22 visible rows on MAIN and about 21 on FREE-SITES with five or fewer sections whose text still carries a heading numbered 20 or higher, out of the visible rows with five or fewer sections.

## Dry-run first

Run a dry-run on each corpus and write down `population`, `skipped_senate`, `suspects`, `counts`, and `reason_counts` before any `--apply`. Do not pass `--apply` until Monitor has reviewed that dry-run.

```bash
python -m scraper.tasks.ocr_section_mash
```

The default prints JSON and does not write.

```bash
python -m scraper.tasks.ocr_section_mash --limit 25
python -m scraper.tasks.ocr_section_mash --statute-id <uuid>
```

`population` is visible statutes that are not Senate-sourced. `rows` lists suspects only.

| Verdict | Meaning |
| --- | --- |
| `MATCH` | Stored headings are not far above the stored section numbers, and the row is not a scanned few-section suspect. |
| `MISMATCH` | A stored section's operative text carries two or more `N.` headings at least 8 above that section's number. Quoted lines are not counted. An amending verb (`inserted`, `substituted`, or `added`) skips headings only until that quoted or unquoted insert ends; `namely` by itself does not open a skip. Headings after a schedule, annex, or appendix marker are not counted, including a numbering run that starts again at 1. A section whose own heading was stripped before storage is still a match when nothing else remains. The same verdict is used when those operative headings outnumber the stored sections by 8 or more. Quarantine candidate. |
| `SUSPECT` | Five or fewer stored sections, and a preserved PDF has no text layer. The headings do not show a mash. Re-extract candidate, not a quarantine. |

## Reasons

| Reason | When |
| --- | --- |
| `ocr_section_mash:headings_above_stored` | Heading mismatch described above. Also the promotion gate reason when the same shape is extracted again. |
| `ocr_section_mash:few_sections_scanned_source` | Very few sections and the preserved PDF is scanned or has no text layer. Unknown text-layer status does not use this reason. |

`already_quarantined` is true when a `document_quarantine` row already exists. `--apply` does not change that row.

## Apply

```bash
python -m scraper.tasks.ocr_section_mash --apply
```

Record the dry-run counts for MAIN and FREE-SITES first. Monitor reviews those counts before this command is used.

- `MISMATCH` inserts `document_quarantine` with reason `ocr_section_mash:headings_above_stored`. `needs_section_refetch` stays false. Re-extracting with the same page numbers would store the same mash.
- `SUSPECT` sets `needs_section_refetch` and does not quarantine. The row stays servable. The flag is only a marker. This sweep does not choose new section numbers from page or line numbers.
- `MATCH` and Senate rows are left unchanged.

Quarantine hides a mismatch from search and corpus reads. The statute row, its sections, and `source_url` stay as evidence.

New extracts of the same shape are refused by the statute save gate (`document_validation:ocr_section_mash:headings_above_stored`) before promotion. Senate sources are not refused by this gate.

## Undo

```bash
python -m scraper.tasks.ocr_section_mash --undo <run_id>
```

`run_id` is in the `--apply` JSON. Undo deletes quarantine rows from that run whose reason starts with `ocr_section_mash:`, and restores `needs_section_refetch` to the value saved before the run. Other reasons are left in place. Undo does not delete statute or section rows.
