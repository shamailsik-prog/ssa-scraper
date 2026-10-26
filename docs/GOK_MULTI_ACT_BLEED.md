# law.gok.pk multi-act PDF bleed sweep

Some `law.gok.pk` statute rows were built from a revised-volume PDF that contains more than one act. Sections of the later act were stored on the earlier act's row. A different row can also keep a statute name that does not match the PDF filename (a Constitution 1973 name on an AJK Legislative Assembly ordinance, or on the Courts and Laws Code).

This sweep reads stored sections and the PDF filename. It does not download PDFs. It does not rewrite `statute.name` or section text. `--apply` only inserts `document_quarantine` rows for visible mismatches.

Scope is `law.gok.pk` (and `www.law.gok.pk`). Other hosts, including Senate, are not classified.

On 2026-10-26 the read-only droplet counts for this host were about 2,674 rows on MAIN (786 visible, all AJK) and about 2,609 on FREE-SITES (787 visible).

## Reasons

| Reason | When |
| --- | --- |
| `multi_act_pdf_bleed:law.gok.pk` | A second act absorbed into the row: a different instrument's title, a numbering drop back to section 1 or 2 that names another instrument or is a Short-title heading that does not name the stored act, or a run of headings about a body (Authority, Tribunal, and similar) that the stored act never names or defines. The stored act's own short title is not bleed, including when the called title differs only by a scan confusion (`i`/`l`/`1`, `c`/`e`) or one extra or missing letter (`Bhimbher`/`Bhimber`, `University`/`Universty`) and the two titles have the same number of significant words. A one- or two-letter margin stamp, with or without a closing parenthesis, glued in front of `the` (`ay) the ... University of Bhimber`) is still that act. A real extra word (`Election Commission` against `Elections`) is still another act. A date or schedule row stored as section `01`, and a revised-volume tail in front of that act, are not bleed. A wrapped line ending `this Act`, and `shall be called in question`, are not a new short title. `amended` in an earlier sentence does not hide the next act's called title. A `may be called` in the same sentence as `substituted` or `namely` is a citation. A Board, Council, or Authority the act itself constitutes (`Constitution of the Board` of the Cell, `Board of Directors` of the Organization, `Board of Governors` of the Foundation, `establish a Zakat Council`) is not a foreign body. `the commission of an offence` is not a Commission. |
| `name_pdf_mismatch:law.gok.pk` | The PDF filename is a different instrument from the stored name. Parenthetical act numbers such as `(Act II of 1962)` and a `\|II` source-url label are ignored. |

A year used as a section number (1800–2030), or a year/junk number that only sits before section 1, is reported and is not quarantined. Those patterns are too common on this host to quarantine in bulk (hundreds of year-like section numbers; about 22 rows per host are only a year before section 1).

## Dry-run first

Run a dry-run on each corpus and write down `population`, `counts`, and `reason_counts` before any `--apply`.

```bash
python -m scraper.tasks.gok_multi_act_bleed
```

The default prints JSON and does not write.

```bash
python -m scraper.tasks.gok_multi_act_bleed --limit 25
python -m scraper.tasks.gok_multi_act_bleed --statute-id <uuid>
```

Verdicts:

| Verdict | Meaning |
| --- | --- |
| `MATCH` | No bleed and no filename mismatch. A pipe label, an `(Act N of YYYY)` parenthetical, a year-fragment section, or a year before section 1 can still be present. |
| `MISMATCH` | Quarantine candidate. `reason` is one of the two reasons above. `signals` names the evidence. |

`already_quarantined` is true when a `document_quarantine` row already exists. Those rows stay in the report so a contained bleed is still visible. `--apply` does not change them.

Rows already contained on the droplets (do not expect a new write):

| Corpus | Id prefix | Stored name | Reason |
| --- | --- | --- | --- |
| MAIN | `86215821` | Conciliation Courts Act, 1962 | `multi_act_pdf_bleed:law.gok.pk` |
| FREE-SITES | `6321f7b0` | Conciliation Courts Act, 1962 | `multi_act_pdf_bleed:law.gok.pk` |
| MAIN | `e0db6058` | Constitution 1973; PDF is the AJK Legislative Assembly Elections Amendment Ordinance, 1980 | `name_pdf_mismatch:law.gok.pk` |
| FREE-SITES | `8a07a5ad` | Constitution 1973; PDF is the AJK Courts and Laws Code, 1949 | `name_pdf_mismatch:law.gok.pk` |

Same-instrument rows stay `MATCH` when the stored name matches the PDF filename. A 2026-10-26 dry-run on `a810c958` flagged many of these as `multi_act_pdf_bleed`; that was a false positive. Do not quarantine them for a numbering drop or a cited title alone:

| Corpus | Id prefix | Name | Why it is the same instrument |
| --- | --- | --- | --- |
| MAIN | `d16bce19` | Ulma-o-Mushaikh 1997 | Name matches the PDF. A numbering drop without another act's title. |
| MAIN | `32bfe59b` | Extra-Territorial Offences 1962 | Volume tail, then this act's own short title. |
| MAIN | `d56d0c99` | Law Officers 2014 | A date (`01.07.2014`) parsed as section `01`. |
| FREE-SITES | `a4e7d610`, `06d76131`, `41b4d195` | Twins of Ulma, Extra-Territorial, and Law Officers | Same shapes. |

The same class, name matching filename, includes the one-page Presidential Election Act, 1974 text, Finance 2012 and 1998 (schedule rows such as `01. Middle Classes`), Wildlife (its own Board; `the commission of an offence` is not a Commission), Ehtesab Bureau 2001, Essential Articles 2016 (`shall be called in question`), Payment of Wages 1967, Zakat and Ushr 1985 (the Zakat Council), Rahamat-Ul-Aalameen, Finance Supplementary 1974, Islamic Tazirati Quaneen Nafaz Amendment 1995, Press Foundation 2003 (the Board of Governors), Liberation Cell 2014 (the Cell's Board), Power Development Organization 2014 (the Board of Directors), Criminal Law 3rd Amendment 2020, Interim Constitution 1st Amendment 1975, and Bhimbher University 2023 (a called title `University of Bhimber` against a stored `Bhimbher Universty` or `Unlversity`, including when a margin stamp OCRs as `ay)` in front of `the`). MAIN `f70e23ad` and FREE-SITES `9aa67f0c`.

The 7-page Presidential Election Act, 1974 volume (MAIN `18c06bc5`, FREE-SITES `b6de056f`) continues into the Legislative Assembly Privileges Act, 1974. That is a real second act. It stays `MISMATCH` with `second_act_title`. A Contract Act, 1872 name on the Regulation of Services PDF (FREE-SITES `fcfc4374`) stays `name_pdf_mismatch`.

`eaacc59b` Regulation of Services (junk section `2591`, then lower numbers) is not bleed unless the restarted text opens a different instrument. A bare drop after `2591` stays `MATCH`.

Name mismatches that must still quarantine: Cabinet Order against `AZAD-KASHMIR-OLD-LIABILITIES`; an Elections Act against `Election-Comission` (those tokens do not intersect); Constitution 1973 against the Courts and Laws Code or the Legislative Assembly ordinance.

Amendment rows that only cite another act stay `MATCH` as well: MAIN `89de4e53` LG Amend, `8b292efe` Penal Code Amend 1993, `f257a105` Ehtesab Amend 1998, `b21e2057` Finance 2023, and FREE-SITES `7b76f260`, `590ea26d`, `cb509ba3` University AJK 1985, `985e6d1a` Finance 1998, `b3e2ee30`, `d0569a54`. Quarantine one of these only when the stored text opens a different instrument or a foreign body run, as Conciliation Courts does with Mirpur.

Mirpur Town Development Authority's own statute rows are a one-way control: the bleed runs into Conciliation Courts, not back into Mirpur. A Mirpur row whose name and definitions establish the Authority and the Tribunal should stay `MATCH`.

## Apply

```bash
python -m scraper.tasks.gok_multi_act_bleed --apply
```

Record the dry-run counts for MAIN and FREE-SITES first. `--apply` inserts a quarantine row only when the statute is visible and the verdict is `MISMATCH`. An existing quarantine row is left as it is, including rows already held for `multi_act_pdf_bleed:law.gok.pk` or `name_pdf_mismatch:law.gok.pk`. Running `--apply` twice does not insert a second row and does not change the first run id.

Quarantine hides the statute from search and corpus reads. The statute row, its sections, and `source_url` stay as evidence. `needs_section_refetch` is not set.

New AJK ingest cuts a law.gok.pk PDF at a later act heading so a second act is not stored on the first act's row. That does not repair rows already in the corpus.

## Undo

```bash
python -m scraper.tasks.gok_multi_act_bleed --undo <run_id>
```

`run_id` is in the `--apply` JSON. Undo deletes quarantine rows from that run whose reason starts with `multi_act_pdf_bleed:law.gok.pk` or `name_pdf_mismatch:law.gok.pk`. Other reasons are left in place. Undo does not delete statute or section rows.
