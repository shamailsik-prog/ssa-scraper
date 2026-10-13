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

## Next: the reading agent (planned)

A background agent reads each unreported judgment and records, with the paragraph it relies on:
the law and sections applied, the legal point decided, the interpretation adopted, and the ground
the decision turns on. Its tags are machine readings, marked as such in the viewer and always shown
beside the judgment text; they are never a substitute for reading the judgment.
