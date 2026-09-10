# SIKANDER AI — THE SCRAPER — Plain-English Reference

This Markdown reference preserves the operator concepts from the uploaded `SIKANDER_AI_Scraper_Plain_English_11Sep2026.pdf` so Claude Cloud can read them directly from the repository even when the original PDF is not mounted in its workspace.

The original PDF remains the authoritative presentation/reference document; this file is a textual operator reference, not a replacement for the source PDF.

## 1. The scraper in one page

The corpus service is a separate application in its own repository and cloud project. Its job is to build and keep current a database of Pakistani judgments and legislation that the firm owns and that SIKANDER AI reads through a read-only database connection. It runs on a schedule and ordinarily needs human intervention only for quality review and for re-enabling a source that has been halted.

Operational stages:

1. Scheduler wakes a source at its configured interval.
2. The source is fetched by an authenticated login session for PakistanLawSite or a public fetch/browser workflow for public portals.
3. Raw HTML/PDF/text is stored before parsing.
4. Parser/extraction produces citations, parties, bench, date, text, headnotes, statutes and cases cited with a confidence score.
5. Trusted-source records above threshold are promoted automatically; low-confidence/review-source records go to quarantine.
6. Nightly enrichment handles embeddings, treatment classification, statute version history and citation cross-references.
7. Canonical data is stored in the firm-owned PostgreSQL corpus; the corpus service is the writer and SIKANDER AI is read-only.
8. Optional archive mirrors can copy promoted material into external storage such as Google Drive.

## 2. PakistanLawSite login/session model

PakistanLawSite uses the firm's own paid access and is treated as `login_session` access. The service uses Playwright browser profiles and a human interactive-login workflow when verification is present.

A human clerk opens Interactive Login from the dashboard, enters the credentials, completes any verification shown by the site and closes/finishes the login. The service persists the resulting browser storage state (cookies + localStorage) encrypted. Normal scraping then runs headless inside that saved session.

The service never solves CAPTCHA or robot verification automatically.

A normal disconnect is a continuity event: preserve anything already received, wait 30 seconds, reconnect on the same slot and resume the same query/result-page cursor. Only if that continuity recovery fails may an already-authorized alternate continuity slot be used. The job does not restart from page one.

An explicit block is different. HTTP 403, account suspended, access denied, automated-access warnings, unusual activity or equivalent block signals HALT the source. The system does not rotate accounts, proxies or other mechanisms to get around a block.

## 3. The corpus service copies and extracts; it does not learn firm law from the internet

The corpus service preserves legal source material and extracts structured data from it. It does not learn the firm's drafting style or legal preferences from public internet sources. Firm-specific learning belongs in the main SIKANDER AI application and its own knowledge services.

Human review in the corpus service confirms extraction quality—citation, court, headnote boundary, treatment label when uncertain—not a rewritten version of the law.

## 4. One working database

The canonical working store is PostgreSQL. It supports citation search, semantic search, court/date filters, authority treatment, statute versions and cross-references. Archive folders are secondary copies and are not queried by the main application.

The corpus service is the writer. SIKANDER AI receives only the read-only corpus role/connection.

## 5. Archive mirror

Optional archive copies may be written to several targets at once:

- Google Drive
- Dropbox
- OneDrive
- S3-compatible storage (AWS S3, Cloudflare R2, Backblaze, MinIO, etc.)
- SFTP
- SMB
- Local/mounted path

The service creates the folder tree automatically. It is write-once: a new version is a new file; existing archived material is not silently overwritten.

Representative structure:

```text
<root>/
  Citations/
    PLD/<year>/
    SCMR/<year>/
    CLC/<year>/
    YLR/<year>/
    MLD/<year>/
    PCrLJ/<year>/
    PTD/<year>/
    PTCL/<year>/
    Unreported/<court>/<year>/
  Statutes/<jurisdiction>/<Act name>/
  Instruments/<year>/
  _index/
```

The archive is not the working corpus. SIKANDER AI reads PostgreSQL.

## 6. How the bot reaches PakistanLawSite judgments

PakistanLawSite is treated as a systematic searcher rather than a generic site crawl because judgments are reached through search results.

### Step 0 — map the search form

The first mapping job records the fields, result layout, pagination, detail-page structure and any result limits. It does not treat the website as a fixed selector forever; it can remap when connector drift is detected.

### Tier 1 — citation enumeration

For subscribed reporters, years and applicable court tokens, enumerate reporter citations systematically. A volume is treated as closed after the configured consecutive-miss threshold (default 40). This tier creates the principal coverage map.

### Tier 2 — statute/section enumeration

Search each known Act/section/order/rule/article, such as `Section 302 PPC`, `s.497 CrPC`, `Order VII Rule 11 CPC` and `Article 199 Constitution`, to fill gaps and create cross-references.

### Tier 3 — vocabulary sweep

Run a large vocabulary of legal subjects, doctrines, procedural terms, party-name words, courts and judge names. Rank terms by yield and retire terms that repeatedly produce no new judgments for that year.

### Tier 4 — ongoing wave

After backlog coverage, repeatedly check the current reporter year, high-yield vocabulary terms and sections of Acts that changed since the previous run.

A judgment reached by multiple routes is stored once. Additional routes are recorded in provenance/coverage data rather than creating duplicate judgments.

## 7. Coverage and inspection dashboard

The dashboard is operational, not a lawyer-facing research interface. It should show:

- source connected/running/waiting/halted state;
- last and next run;
- records saved today;
- PakistanLawSite slot state;
- reporter/year coverage: found, missing, estimated;
- frontier pending/yield information;
- record inspection with provenance;
- quarantine/review queue;
- archive target status/test/run/rebuild controls;
- overall service health/errors/jobs;
- the read-only corpus connection information required by the main application.

## 8. Operating checklist

1. Configure environment/region, pacing and the required source settings before ingest.
2. Establish PakistanLawSite interactive login session(s) where authorized.
3. Set subscribed reporters and earliest permitted subscription year; do not invent these values.
4. Run/map each source and verify first jobs complete.
5. Review quarantine frequently during a new connector's initial period, then reduce review frequency once extraction quality is proven.
6. If a source is HALTED, read the reason before re-enabling it. Resolve account/source issues rather than bypassing a block.
7. Give SIKANDER AI only the read-only corpus connection.
8. Reconcile archive indexes against database counts periodically.
