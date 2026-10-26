# law.gok.pk multi-act PDF bleed sweep

Some `law.gok.pk` statute rows were built from a revised-volume PDF that contains more than one act. Sections of the later act were stored on the earlier act's row. A different row can also keep a statute name that does not match the PDF filename (a Constitution 1973 name on an AJK Legislative Assembly ordinance, or on the Courts and Laws Code).

This sweep reads stored sections and the PDF filename. It does not download PDFs. It does not rewrite `statute.name` or section text. `--apply` only inserts `document_quarantine` rows for visible mismatches.

Scope is `law.gok.pk` (and `www.law.gok.pk`). Other hosts, including Senate, are not classified.

On 2026-10-26 the read-only droplet counts for this host were about 2,674 rows on MAIN (786 visible, all AJK) and about 2,609 on FREE-SITES (787 visible).

## Reasons

| Reason | When |
| --- | --- |
| `multi_act_pdf_bleed:law.gok.pk` | A second act title in the stored text, a document-order restart of section numbering, or a run of headings about a body (Authority, Tribunal, and similar) that the stored act never names or defines. |
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

Visible restart candidates to look for in the dry-run (`numbering_restart`, not already quarantined):

| Corpus | Id prefix | Name |
| --- | --- | --- |
| MAIN | `d16bce19` | Ulma-o-Mushaikh 1997 |
| MAIN | `32bfe59b` | Extra-Territorial Offences |
| MAIN | `d56d0c99` | Law Officers |
| MAIN | `eaacc59b` | Regulation of Services (junk section 2591, then a real restart) |
| FREE-SITES | `a4e7d610`, `06d76131`, `41b4d195` | Twins of the MAIN restart rows |

Closer rows to read in the same dry-run output before deciding they are the same defect. They were still visible on 2026-10-26 and are not assumed quarantined:

- MAIN `89de4e53` LG Amend, `8b292efe` Penal Code Amend 1993, `f257a105` Ehtesab Amend 1998, `b21e2057` Finance 2023
- FREE-SITES `7b76f260`, `590ea26d`, `cb509ba3` University AJK 1985, `985e6d1a` Finance 1998, `b3e2ee30`, `d0569a54`

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
