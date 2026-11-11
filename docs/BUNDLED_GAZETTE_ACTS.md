# Bundled gazette act bleed

Some gazette PDFs print more than one Act, each opened by an `ACT No.` line. The section splitter treated that file as one Act. Where section numbers collided, the earlier Act's text was kept and the target Act's section was dropped. A later Act's repeal could also land on the target row, and a gazette footer such as `under Article 59` could be stored as section 59.

Confirmed row:

| Corpus | Id prefix | Stored name | Source |
| --- | --- | --- | --- |
| FREE-SITES | `f200fa53` | The Service Tribunals Act, 1973 | `https://senate.gov.pk/uploads/documents/1660540467_508.pdf\|LXX of 1973` |

That PDF is three Acts: LXVII of 1973 (Life Insurance (Nationalisation) (Amendment)), LXX of 1973 (Service Tribunals), LXXI of 1973. Stored sections 2 and 3 are the LXVII text. Section 4 of the Service Tribunals Act is missing. Section 59 is the footer. Section 9 runs into LXXI.

New ingest isolates the segment whose act number matches the pipe label, or whose short title matches the target title, and splits only that segment. A line-start `Act No.` is a boundary only when `An Act` or `1. Short title` follows within four lines. An `Act No.` glued into a section tail is also a boundary when `An Act` is the next text, even if that next act's own section 1 is missing. The cut drops the later act from the earlier act's sections. A stored section that still runs across that header is `bundled_gazette_act_bleed`. The year may be the next line (`ACT No. LXXI` then `1973`), and a one-character garble such as `r97J` still counts: that year is the year the neighbouring headings share, or the pipe label's year when none of them print a readable year. A citation tail (`.-`, `In the`, `, in section`), including `Act No. LXX of 1973.- In the …`, is not a boundary, so the sections after that quotation stay with the amending Act. `may be called` and `shall be called` both supply the short title, including when the title breaks onto the next line. If the label and the title name two different segments, or neither matches, the extract stores no sections and promotion quarantines the staging row with `bundled_gazette_unresolved`. It does not set `needs_section_refetch`.

A scanned bundle does not have to print a clean `ACT No. … of YYYY`. senate.gov.pk `1661312733_937.pdf` (Pakistan Rangers (Amendment) Act, 1975, then the North-West Frontier Province Urban Planning Act, 1975, then the Public Service Commission amendment) is three acts, and the text layer matches none of the clean form:

| What was printed | What the scan shows | Why it still splits |
| --- | --- | --- |
| `ACT` | `AC_l` or `ACl` | T read as l, with an optional underscore where the glyph broke |
| `ACT` | `{-CT` | The leading A replaced by one to four punctuation marks. A bare `CT No.` does not match |
| `of` | `on` | f read as n, on the same line (`ACT No. XXXV on 1975`) |
| `of` | `or` | f read as r, on the same line (`XXXTV or 1975`) |
| `of` | absent | The text layer left `or` on the previous line, so the year sits against the numeral (`XXXTV 1975`) |
| `IV` | `TV` | T stands for I. `XXXTV` is XXXIV. The repair applies only to this heading's numeral |
| `An Act` | `.4 Act` | `An` read as a dot plus one digit. A bare `4 Act` is not a preamble |

`Acr No.` is accepted when the numeral is already a roman or an integer (`Acr No. XXII`, including a year printed `197.5`). `Acr No. xt or 2015` is not one of these slips: `xt` is not that numeral. A citation tail (`.-`, `In the`, `, in section`) is still not a boundary.

The same cut runs for Senate, the National Assembly, law.gok.pk, and any other text that has two or more `ACT No.` lines. A law.gok.pk PDF that has no second `ACT No.` line still uses the existing title-heading cut, which drops a later act and does not pull a middle act out of an earlier one.

## Already stored

This sweep reads stored sections against the PDF. It does not rewrite `statute.name` or section text. `--apply` only inserts `document_quarantine` for a visible mismatch.

Population: `senate.gov.pk`, `pcp.gov.pk` when `source_name` is Senate or GazetteOfPakistan, `law.gok.pk`, and `na.gov.pk`. Rows with no sections are counted and not fetched.

| Reason | When |
| --- | --- |
| `bundled_gazette_act_bleed` | An 80-character stretch of a stored section is found in one act segment, and another stretch (another window of that section, or another stored section) is found in a different segment. A mid-section `ACT No.` / `An Act` header is one of those boundaries. |
| `bundled_gazette_act_wrong_segment` | Every located section sits in one segment, and that segment is not the pipe label or the stored title. |

A PDF with one `ACT No.` line is `NOT_BUNDLED`. A bundled PDF whose located sections all sit in the target segment is `MATCH`.

A scanned ``XX`` is sometimes ``)O(``. ``)O(X of 1976`` is Act 30 and that repair is clean, as is ``L)O(XI`` for 81. ``)O(Vlt``, a leading ``1`` standing for ``L`` (``1XIV``), and the shard ``LX\,'`` repair to a number and stay unclean. A pipe label does not select those. ``xxvll`` on the line above ``ACT No. oF`` is also unclean, and it is selected only when it is the only heading that repairs to that number. ``)O(Vlt`` is never selected as that number, including when both forms are in the file. ``An Acl`` counts as the act-intro line. ``of`` may be ``ol``. The year may be split as ``I`` then ``975``, or printed with one dot (``197.5``). ``Acr No.`` plus a plain roman numeral is a heading; ``Acr No. xt`` is not. A line that is ``L`` or ``l`` before ``Short title`` (including ``Short tide``) is section 1. A section is cut at the next Act header, and a ``Price Rs.`` / ``[2940 Ex. Gaz.]`` line (including ``Prlce``) is not stored as a section. A Schedule that follows the last section stays in that section. The statute name prefers the short title in ``may be called the …`` over an ``Act No. … An Act`` heading. A name that is only that numeral heading is a hold (``numeral_heading_name``) and is not applied.

``The following Act``, ``President's Orders and Regulations``, and a ``|Share Print Acts of Parliament Filter by`` suffix are not act titles. A Federal gazette extract whose ``year_enacted`` is null or whose payload omits the key, a section 1 short title that names a different instrument from the ``|marker``, a section that still contains a second Act header, or a footer stored as a section is rejected at validation and is not saved. Preview compares the PDF bytes with the recorded sha. A mismatch, or a confirmed row with no authentic sha, is a hold with no sections and does not adopt the live file's short title.

If the URL marker's Act number is not a heading in the file, the extract has no sections. The current PDF is not replaced with a different source.

## Dry-run re-extract (writes nothing)

```bash
python -m scraper.tasks.bundled_gazette_acts --preview-reextract --skip-fetch
```

This reads the confirmed bundled-gazette rows and prints what a fresh extract would contain. It does not insert, update, or delete. ``--skip-fetch`` uses recorded PDF bytes only, and those bytes must match the pinned sha. Without ``--skip-fetch`` the command GETs the public PDF and still writes nothing. A live file whose sha is not the pin, a confirmed row with no authentic sha, or a marker that is not a clean heading in that PDF is reported with ``section_count`` 0. Do not add ``--apply``.

The eight rows first named by prefix are in `CONFIRMED_BLEED_STATUTE_IDS` and `CONFIRMED_PDF_SHA256` under their full UUIDs (MAIN and FREE-SITES twins): `eef57038-13dc-4b4d-b050-14cb7d490e44`, `eb8f8ae0-727a-464b-9786-db0247eca896`, `d7d3187e-9ebd-4782-aa9a-228b40d2dd96`, `f700f160-4bf3-41df-9ec5-b052b5627f77`, `17f283f3-b875-4c5d-b3ed-6a235d9ebd21`, `df206e70-1114-4676-8eec-c77fa051f268`, `65c5b7b3-674c-4726-8372-b068527cea90`, `768e086d-329e-42d9-ade3-01900badfb8e`. Preview matches those ids exactly. It does not match an 8-hex prefix.

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
