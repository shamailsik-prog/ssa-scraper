# Moore's Indian Appeals (MIA)

Moore's Indian Appeals is the Privy Council's East Indies series, 1836–1872, volumes 1–14.
It is not on PakistanLawSite. `KNOWN_REPORTERS` is that site's search-harvest fallback, so
MIA is not in that tuple. Printed names collapse to the letters-only code `MIA`:

Moore IA, Moo Ind App, M.I.A., Moore's Indian Appeals.

A judgment from this path is `reporter=MIA`, with volume, start page, parties, decision
date when the margin date parses, `court=Privy Council`, the OCR text, the archive.org
item URL, and `ocr_source`. The dedup key is `(MIA, volume, page)`.

## Dry-run one volume

Nothing is scheduled and nothing is written to the database. This downloads one volume's
`_djvu.txt` (one request, or a second if the catalog filename 404s and metadata names the
file). Requests wait at least one second. It does not download the other volumes.

```bash
python -m scraper.tasks.archive_moore_ia --volume 6 --dry-run
```

On the volume 6 grea OCR (`reportsofcaseshe06grea_djvu.txt`, about 1.2 MB, not stored in
this repo) that command splits about 20 captions and pages 16 of them. Nusserwanjee
Pestonjee is `MIA:6:134`, decided 1855-06-19, on appeal from the Sudder Dewanny Adawlut
at Bombay. Running heads in that scan are noisy, so a caption with no Table of Cases
match stays unpaged rather than inventing a page.

Offline, with a file you already have:

```bash
python -m scraper.tasks.archive_moore_ia --volume 6 --ocr-path ./reportsofcaseshe06grea_djvu.txt --dry-run
```

`--list-volumes` prints the item ids. The grea series is primary. DLI and the Google scan
fill volumes 9, 11 and 14. Moore PC and Moore PC NS items (`reportscaseshea00commgoog`,
`reportscaseshea00moorgoog`, `reportscaseshea01commgoog`) are decoys and are not in the catalog.

## Volume 13

Volume 13 (1869–1871) is not on archive.org. `--volume 13` stops with that fact and does
not fetch. Finding a scan is a follow-up (TODO), not a blocker for the other thirteen volumes.

## BAILII

BAILII is not an adapter. It resolved 3 of 22 corpus cites, PDF-only, behind Anubis.
Those three URLs are stored as `alt_source_url` and are not fetched:

- 8 MIA 379 — https://www.bailii.org/uk/cases/UKPC/1861/1861_19.html
- 9 MIA 307 — https://www.bailii.org/uk/cases/UKPC/1862/1862_21.html
- 10 MIA 203 — https://www.bailii.org/uk/cases/UKPC/1865/1865_33.html
