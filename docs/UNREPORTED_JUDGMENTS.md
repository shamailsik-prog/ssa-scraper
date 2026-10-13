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
- **Interpretation**: the reading of the law the court adopts, in a sentence or two.
- **Turns on**: what the decision rests on.
- **Subjects**: one to three headings taken only from the fixed list `SUBJECTS` in
  `scraper/tasks/unreported_reader.py` (owner's decision, 13 October 2026), so one topic never splits across
  near-identical headings. Any other heading is dropped and recorded; a judgment with none is filed under Other. To add
  a heading, add it to the list. And the **outcome**.

Guard against invention: a law or point whose quoted words are not found in the judgment is dropped
(kept aside in `dropped`, never shown as a finding). Interpretation and "turns on" are one-line summaries.
All of it is a **machine reading**: the viewer labels it so, shows it above the full judgment, and it is
never a substitute for reading the judgment.

**Switching it on.** Set `READER_MODEL` (or `TREATMENT_MODEL`) and an API key (`OPENAI_API_KEY`, or
`READER_BASE_URL` + `READER_API_KEY` for any OpenAI-compatible endpoint) in `/opt/ssa-scraper/.env`, then
restart, or run the deploy-cloud workflow with `reader_model` set (it writes `READER_MODEL` and restarts; `off`
stops it, even where TREATMENT_MODEL is set). One reading run at a time (advisory lock); an answer without a
reading counts as a failure and is retried after `READER_RETRY_HOURS`. Without them the agent does nothing and the dashboard lists `READER_MODEL` under "Not configured".
Only public judgments are sent (the unreported section is court websites); PakistanLawSite text never is.

**Cost.** Up to `READER_MAX_CHARS` (24,000) characters of each judgment are sent (the opening and the
closing part of a long one), about 6,000 tokens. Twelve thousand judgments are roughly 75 million input
tokens; the price depends on the model chosen. At 20 a run every 10 minutes the agent reads about 2,900
judgments a day.

**Viewer.** Corpus viewer → judgments → Section *unreported*: the **Subject** filter lists the headings the
agent has assigned, with counts; opening a judgment shows the reading above the text.
