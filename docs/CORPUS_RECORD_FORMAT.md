# Corpus record format (version 1)

Every scraper in this project hands its documents on in **one format**. The PakistanLawSite scraper and
the free-sites harvester both use it, so the ingestion stage (OCR, indexing, learning) never needs to know
which scraper collected a document.

The code that builds and checks the format is `scraper/corpus_record.py`.

## One judgment = one folder

```
Citations/<reporter>/<year>/<citation>/       reported judgments (PLD, SCMR, YLR ...)
Unreported/<court>/<proceeding>/<year>/<id>/  unreported judgments, by kind of proceeding
Unreported/<court>/<year>/<citation>/         court-website judgments written before that

    original.pdf        the genuine PDF from the source, byte for byte
      or
    rendered_copy.pdf   a PDF made from the text when the source had no PDF (labelled as such)
    judgment.txt        the full text
    metadata.json       the record described below
```

Exactly one of `original.pdf` / `rendered_copy.pdf` is present. The record's `files.document` names which.

## metadata.json for a judgment

| Field | Meaning |
|---|---|
| `schema_version` | `1`. A file without it was written before version 1 (see *Older files*). |
| `record_kind` | `"judgment"` |
| `canonical_citation` | The citation the corpus files it under, e.g. `PLD 2024 SC 300` |
| `case_title`, `petitioner`, `respondent` | Parties as printed |
| `court` | Court name |
| `judge_names`, `bench_size`, `bench_type` | Bench (`single`, `division`, `full`, `larger`) |
| `decision_date`, `year` | Date of decision (`YYYY-MM-DD`) and year (1947–2100) |
| `reported`, `reporter`, `volume`, `page_number` | Whether it is a reported judgment, and where it is reported |
| `docket_number` | Case number, e.g. `C.P. 1/2024` |
| `unreported`, `proceeding_type` | `true` for a judgment from a court's website that no law report cites (its `canonical_citation` is then `UNREPORTED <court> <case no> (<date>)`), and the kind of proceeding: `bail`, `constitutional_petition`, `civil_suit`, `civil_appeal`, `criminal_appeal`, ... (`scraper/unreported.py`). Optional: older files lack them. |
| `language` | `en` or `ur` |
| `source_name`, `source_url`, `access_method` | Where it came from; `public` or `login_session` |
| `full_text_sha256_canonical` | Fingerprint of the text, so duplicates and changes are detectable |
| `has_original_pdf`, `document_label` | Whether the PDF is the genuine original or a rendered copy |
| `needs_ocr` | `true` when the text is too thin to be the real text (a scanned PDF) |
| `collected_at` | When the corpus accepted the judgment |
| `files` | `{"document": "original.pdf" or "rendered_copy.pdf", "text": "judgment.txt"}` |
| `judgment_id` | The record's id in the scraper's own database |

## OCR is not the scraper's job

A scraper saves the original PDF and whatever text it carries, then moves on. When that text is too thin
(fewer than 200 characters), the record says `"needs_ocr": true` and **ingestion** reads the scanned pages
later. No scraper stops to OCR. PakistanLawSite publishes typed text, so its records should never need it.

## Statutes

A statute section is recorded per version with `record_kind: "statute_section"`: statute name, short name,
jurisdiction, type, year enacted (any year, the Penal Code is 1860), repeal status, section number and title,
chapter, part, version number, effective dates, text fingerprint `text_sha256`, source and `needs_ocr`.
The builder is `statute_section_record()`; statute records travel to Drive with the batch transfer.

## Checking a record

`validate_record(record)` returns a list of plain-sentence problems; an empty list means the record is
sound. Ingestion runs it on every record it reads and sets aside any record that fails, instead of
learning from bad data.

## Older files

Judgments already on Google Drive keep the `metadata.json` they were written with (Drive files are written
once and never overwritten). Those files have no `schema_version`: treat them as **version 0**. Version 1
kept every version 0 field with the same name and meaning, so a reader can use them directly and simply
treat the version 1 additions as unknown.
