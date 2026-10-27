# Amendment-date section inventory

Auditor 2026-10-27 04:15 PKT. On free-sites, statute `1032ddb0` ("The Bank of Azad Jammu & Kashmir Act, 2005 (ACT VI OF 2005)") comes from a law.gok.pk PDF. Footnote amendment dates in `DD.MM.YYYY` form were parsed as section numbers and took the place of the real sections:

- s.11 = `11.10.2018. 278 Volume XI (2001-2010) proportion to the shares…` (106 chars)
- s.17 = `17.02.2007. 2 In subsection (1) for the words…`
- s.23 = `23.03.2019. 277 Volume XI (2001-2010) discovered to have not been qualified…`

Real ss.11, 17, and 23 are not in those rows. A year-like section `2018` on the same statute is the separate 1800–2099 class (a bare year line). This inventory does not merge or delete it. Missing ss.18, 20, 25, and 26 are not restored by rewriting the date rows; the real bodies are not stored there.

Day and month numbers are small, so they collide with real section numbers. The year-as-section sweep does not catch them. This is also not PCSIR, bundled-gazette bleed, Bhimber, or OCR section-mash.

The splitter now refuses a line that starts with `DD.MM.(19|20)YY` as a heading. The note stays on the previous real section when there is one. A leading note, before any real section, is not pasted onto the next section.

## Legacy counts (not growing)

Visible statutes whose current section text starts with `DD.MM.(19|20)YY`, as reported with this query shape:

```sql
-- current statute_section_version.section_text
-- ^[[:space:]]*[0-9]{1,2}[.][0-9]{1,2}[.](19|20)[0-9]{2}
```

| Corpus | Rows | Statutes | law.gok.pk rows | senate rows |
| --- | --- | --- | --- | --- |
| main | 23 | 17 | 21 | 2 |
| free-sites | 26 | 19 | 24 | 2 |

`1032ddb0` is one of the free-sites law.gok.pk statutes.

## Commands

Report only. Run it against each database and read `rows`, `statutes`, and `by_host`. A report from one database does not describe the other.

```bash
python -m scraper.tasks.amendment_date_section
```

`--apply` is opt-in and still writes nothing. It does not rewrite section text, does not insert `document_quarantine`, and does not set `needs_section_refetch`.

```bash
python -m scraper.tasks.amendment_date_section --apply
```

`writes` and `section_rewrites` stay `0`. `query` is the POSIX pattern above. `samples` are round-robin by host. Quarantined statutes are left out of the count.

## Re-extract

Do not rewrite these rows in place. The date row replaced the real section, so merging or editing it cannot bring ss.11, 17, or 23 back.

After this splitter fix is deployed, re-extract the affected statutes from their source PDFs (law.gok.pk and the two senate rows). The new split keeps a real `11. Capital…` heading and does not open a section on `11.10.2018.`. Confirm the re-extract before dropping the old date rows.

A line that is only `2018.` stays on `python -m scraper.tasks.year_section_sweep`.

## Tests

```bash
python -m pytest tests/test_amendment_date_section_splitter.py tests/test_amendment_date_section_inventory.py
```
