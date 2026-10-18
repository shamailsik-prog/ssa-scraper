# Unreported judgments

A judgment from a court's own website that no law report (PLD, SCMR, CLC, YLR ...) cites is an
**unreported judgment**. These used to be held for review ("no citation supported by source")
because every judgment in the corpus is filed under its citation. They now enter the corpus in their
own section.

## How one enters

- **Identity.** `UNREPORTED <court> <case number> (<decision date>)`, for example
  `UNREPORTED SHC C.P. No. D-5678 of 2023 (2024-05-01)`. The case number comes from the court's
  listing row when the site gives one (AJK, GB, FSC); otherwise from the heading of the judgment.
  Two orders in the same case on the same day get the text fingerprint added, so they never merge.
  With no case number at all, the identity is court, date and fingerprint.
- **Kind of proceeding** (`proceeding_type`), read from the case number: bail, habeas corpus,
  constitutional/writ petition, petition for leave to appeal, civil suit, civil/criminal/intra-court
  appeal, civil/criminal revision, review, criminal or tax reference, contempt, execution, transfer,
  civil/criminal miscellaneous, other.
- **Held instead** when the court is unknown or the text is under `UNREPORTED_MIN_TEXT_CHARS` (400):
  a cause list or an "adjourned" note is not a judgment.
- **PakistanLawSite is never affected.** It publishes only reported judgments, so one without a
  citation there is still a bad capture.
- **Release.** Each promotion run releases up to `UNREPORTED_RELEASE_BATCH` (500) held judgments and
  closes their review items. The backlog clears on its own within a few hours.
- **Later reported.** When the same judgment arrives again with a citation of its own, it takes that
  citation; the unreported identity stays as an alias.
- **Switch off.** `UNREPORTED_JUDGMENTS=false` restores the old behaviour.

## Where it shows

- **Viewer.** Dashboard → Corpus viewer → Kind *judgments* → Section *unreported*, and Proceeding
  (with counts). `GET /admin/corpus/unreported` gives the counts by proceeding and court.
- **Drive.** `Unreported/<court>/<proceeding>/<year>/<identity>/`.
- **Record format.** `unreported` and `proceeding_type` (docs/CORPUS_RECORD_FORMAT.md).

## The reading agent

A background task (`scraper/tasks/unreported_reader.py`, every `READER_SCHEDULE_SECONDS`, 600 s, up to
`READER_BATCH`, 20, judgments a run) reads each unreported judgment once and records:

- **Laws**: each statute and section the court applies or interprets, with the words of the judgment that show it.
- **Points**: each point decided and how, with the words of the judgment that show it.
- **Interpretation**: each legal interpretation is stored only with a supporting quotation copied from the judgment.
- **Turns on**: each dispositive proposition is stored only with a supporting quotation.
- **Subjects**: one to three headings taken only from the fixed list `SUBJECTS` in
  `scraper/tasks/unreported_reader.py` (taxonomy version `legal-subjects-1`). Invalid headings and valid headings
  beyond the three-heading ceiling are recorded in `dropped`; `Other` is used only when the model explicitly selects it.
- **Outcome**: accepted only when the disposition is supported by quoted words found in the judgment.

Guard against invention: laws, points, interpretation, dispositive reasoning and outcome are source-grounded
before they become the current reading. Rejected items remain auditable in `dropped`. Every reader attempt is written
to immutable `judgment_reading_version` history; `judgment_reading` is only the current validated serving snapshot.
A failed re-read therefore never destroys or hides the last good reading. All of it remains a **machine reading**:
the viewer labels it so and it is never a substitute for reading the judgment.

**Switching it on.** Set `READER_MODEL` (or `TREATMENT_MODEL`) and an API key (`OPENAI_API_KEY`, or
`READER_BASE_URL` + `READER_API_KEY` for an OpenAI Chat-Completions-compatible endpoint that supports JSON-object response mode) in `/opt/ssa-scraper/.env`, then
restart, or run the deploy-cloud workflow with `reader_model` set (it writes `READER_MODEL` and restarts; `off`
stops it, even where TREATMENT_MODEL is set). Concurrent workers are safe: each judgment is protected by its own
PostgreSQL advisory lock, so different judgments can be read in parallel without duplicate model spend. Failures are
classified (authentication/configuration, rate-limit, transient provider/network, validation, database), each attempt
gets its own `retry_after_at`, and provider-wide failures open a Redis-backed circuit breaker. Without a configured
model/key the agent does nothing and the dashboard lists `READER_MODEL` under "Not configured". Only public judgments
are sent (the unreported section is court websites); PakistanLawSite text never is.

**Whole-judgment coverage.** `READER_MAX_CHARS` (24,000 by default) is now the maximum **per chunk**, not a
head/tail truncation limit. Longer judgments are read through overlapping chunks (`READER_CHUNK_OVERLAP_CHARS`,
1,200 by default), so middle paragraphs are not omitted. Chunk results are consolidated and then checked against the
full judgment text before publication.

**Cost and observability.** Every successful or failed attempt records provider/model, prompt and taxonomy version,
judgment-text hash, input/output token counts, total latency, chunk count, parse-repair use and error class. Actual
token usage therefore comes from the provider response instead of a fixed estimate. The 80/20 historical/fresh default
(`READER_FRESH_SHARE=0.2`) drains backlog while keeping newly promoted judgments from waiting behind the entire archive.

**Viewer.** Corpus viewer → judgments → Section *unreported*: the **Subject** filter lists the headings the
agent has assigned, with counts; opening a judgment shows the reading above the text.
