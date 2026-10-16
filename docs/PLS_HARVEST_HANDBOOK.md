# PakistanLawSite Complete-Harvest Handbook

**Owner:** Shamail Sikander | **System:** SSA scraper (repo `shamailsik-prog/ssa-scraper`) | **Version:** 2.0, 16 October 2026

**Who this is for:** Shamail (owner, lawyer, not a coder) and Claude Code (sole engineer). New Bot (ops) and Scraper Auditor (read-only audits) follow it too. Where this handbook and older notes disagree, this handbook wins. Where this handbook and Shamail's design disagree, Shamail's design wins.

---

## Executive summary

**The goal:** every judgment on PakistanLawSite (PLS) gets downloaded, verified, stored in the database (DB) and mirrored to Google Drive. That includes every reporter and every year. Statutes from PakistanCode and public court and assembly sites get the same treatment. "Done" is not a feeling. For each reporter × year it is a number we can check: **site total = stored + quarantined-with-a-reason.**

**Where we are (12 Oct 2026):** about 34,600 judgments, arriving at about 130–240 an hour. Statutes have been flat at 12,618 since 11 Oct 2026, 04:03, while PakistanCode shows ACTIVE. Nobody yet knows whether that lane is finished or stuck.

**What went wrong in the last three weeks.** Almost every stall had one of four causes:

1. **The scraper lost its place or misjudged its progress.** The cursor was never saved. A query was marked done after page 1. Known rows weren't recognised.
2. **The single login queue was blocked.** Grid walks and the case-ID walk held it, and harvest ticks expired before they ran.
3. **Deploys and maintenance fought the harvest.** Two deploy crons collided, a 0.33 h threshold killed a healthy run, and a duplicate maintenance task starved promotion.
4. **The monitors watched the wrong thing.** The watchdog said "not stalled" during real stalls, and the dashboard showed red while the harvest was healthy.

Parsing gaps caused the rest: spaced "P L D", missing reporters (PLC, CLD, GBLR), and PLD keys stored without the court, which dropped 45 real PLD 2026 judgments as false duplicates.

**How we fix it.** We follow Shamail's design without exceptions:

- Search reporter × year × page, newest first.
- Check the DB before opening any judgment.
- Keep a coverage ledger per reporter × year.
- Split capped searches by court, then by page range.
- Keep the two PLS accounts independent: Account 1 harvests; Account 2 verifies judgments and challenges coverage. Account 2 never acquires corpus data on behalf of Account 1.
- Snowball the citations found inside judgments as a cross-check.
- Use judge and keyword searches only for gaps left at the end.

We make progress durable (commit per row, deploys that drain rather than kill), give each job type its own queue, and replace single-signal monitors with checks that combine several facts.

**The next 10 changes**, in order, are in Section 10. The first is merging Cursor PR #197 (the one-page bug fix).

**How to read the rules.** Rules are numbered (e.g. R4.2). "Acceptance test" lines say how to prove a rule is met. Any figure marked *example* is illustrative, not measured.

---

## 1. Goal and definition of done

**R1.1 The unit of completeness is one reporter × year** (e.g. PLD 2019, PCrLJ 2018). Each one has exactly one row in the coverage ledger (Section 2).

**R1.2 Completeness proof.** A reporter × year is complete only when all of these hold:

- `site_total` is known, read from the site's own result count (summed across splits if the search was split).
- `stored + quarantined_explained = site_total`. A row counts as `quarantined_explained` only if it has a named quarantine reason (Section 5) and a stored source copy.
- Every result page in the ledger range has been read, with the last page confirmed.
- The snowball cross-check (R2.11) has found no citation for that reporter × year that is missing from the DB.

> Acceptance test: the daily coverage report (R6.6) shows `gap = site_total − stored − quarantined_explained = 0` and `status = done` for that row, and an Auditor spot check of 10 random citations from the site finds all 10 in the DB.

**R1.3 Whole-corpus done** means every reporter × year from the earliest year on the site to the current year is `done`, every promoted statute section has a Tier-2 discovery frontier, no due Tier-4 fresh wave remains, and Account 2 has performed a recent coverage challenge with zero misses. Current-year rows remain continuously reopenable because new judgments keep being published.

**R1.4 Stored means four things:** the text is in the DB, it passed the quality gates (Section 5), it is mirrored to Drive (Section 8), and it is embedded. A judgment that is in the DB but missing on Drive counts as stored-but-unmirrored, and the reconcile job (R8.3) repairs it.

**R1.5 The scope in priority order:**

1. PLS reporters: PLD, SCMR, CLC, PLC, PLC(CS), PCrLJ, YLR, MLD, PTD, CLD, GBLR, PLJ, and any other reporter in the site's dropdown.
2. PakistanCode statutes.
3. Public court sites (e.g. SHC) and assembly sites.

---

## 2. Enumeration strategy

### 2.1 Search shape

**R2.1 Primary search: reporter × year × page, newest first.** Start at the current year and walk backwards. Within a year, read every result page in order.

**R2.2 Build the search form from the live site, never from memory.** Claude keeps a small form map with field names and allowed dropdown values. A daily job refreshes it by reading the live search form. A reporter value is valid only if it appears exactly in the site's dropdown (e.g. `PCRLJ`, not `PCrLJ`). Nothing is typed into the Citation box unless the query is truly a citation lookup.

> Acceptance test: a unit test fails if any configured reporter value is missing from the latest saved dropdown snapshot. The form-map refresh logs the dropdown values and their differences from yesterday.

**R2.3 Run a canary before each slot starts a shift.** The canary is one known query with an expected minimum result count (e.g. *example*: PLD 2020 must return more than zero rows, including one known citation). If it fails, that slot is set to `blocked: form_or_session` and an alert is raised. Nothing is written to the ledger.

**R2.4 Check the DB before opening any judgment.** For each grid row, build its citation key (R5.1) and look it up. If it is known, record it as `seen` and move on without opening the judgment. Open only unknown rows. Matching uses the same normaliser as the parser, so the grid's "PLD 2024 Lah. 12" and the DB's "PLD 2024 Lahore 12" give the same key.

> Acceptance test: re-walking a fully stored page opens 0 judgments, and the log shows `known=N, opened=0`.

### 2.2 The coverage ledger state machine

**R2.5 Each ledger row records:**

- reporter, year, and optionally court and page-range (for splits)
- `site_total`, `pages_total`, `last_page_read`
- `stored`, `quarantined_explained`, `status`, `slot`
- `updated_at`, `reason`

**R2.6 The states:**

| State | Meaning | Who moves it |
|---|---|---|
| `pending` | Not started, or reopened | Planner, reopen migrations |
| `in_progress` | A slot is reading pages; `last_page_read` advances per page | Harvest tick |
| `needs_split` | The site capped the results, or the total looks truncated | Harvest tick |
| `done` | The completeness proof (R1.2) holds | Harvest tick, but only if R2.7 passes |
| `blocked` | Repeated errors, form mismatch, session failure; `reason` is required | Any job; cleared by a self-heal guard or Claude |

**R2.7 "Done" is allowed only if one of these holds:**

- **(a)** `site_total` is known, every page up to `pages_total` has been read, and `stored + quarantined_explained = site_total`; **or**
- **(b)** `site_total` is unknown, but reading continued until a page came back empty (zero rows) **and** that empty page was confirmed by one retry.

The one-page bug happened because "done" was set after page 1 with `site_total = None`. That must never happen again. PR #197 puts this rule in place. Its reopen migration resets CLC 2019, PLC 2019, PCrLJ 2018, MLD 2019 and any other row closed with an unknown total.

> Acceptance test: a unit test sets `site_total=None`, page 1 with 20 rows, page 2 not read, and asserts the status stays `in_progress`. A DB query finds 0 `done` rows where `site_total IS NULL` and no empty page was recorded.

**R2.8 Allowed transitions:**

- pending → in_progress
- in_progress → done, needs_split, or blocked
- needs_split → split into child rows, each starting as pending. The parent closes as done when all its children are done and their totals add up.
- blocked → pending, after the fault is cleared
- done → pending, only through a reopen (R2.12) or a snowball miss (R2.11)

Any other transition is a bug.

### 2.3 Result caps and unknown totals

**R2.9 Handling capped results.** Suspect a cap if the reported total equals a round ceiling, if the reported total is more than the rows you can actually page through, or if paging stops early while results remain.

1. First, split by **court** (SC, Lahore, Sindh, Peshawar, Balochistan, Islamabad, FSC, tribunals, and so on, as listed in the live dropdown).
2. If a court slice is still capped, split it by **page range** of the reporter (e.g. *example*: pages 1–500, 501–1000), or by a smaller date range if the site supports one.

Each slice gets its own ledger row. The parent's `site_total` is the sum of its children.

**R2.10 Handling unknown totals.** If the site shows no total, store `site_total = NULL` and read until the confirmed empty page (R2.7b). When finished, record `pages_total` and the observed row count as `observed_total`. The coverage report marks these rows "done (observed)" so they stand apart from "done (site-confirmed)".

### 2.4 Snowballing and reconciliation

**R2.11 Snowball cross-check.** Every stored judgment is scanned for the citations it cites. Each cited citation is normalised (R5.1) and checked against the DB. A citation that is not in the DB goes into a `snowball_queue` with its reporter × year. The snowball queue has two uses:

- **As a cross-check.** If a `done` reporter × year receives a snowball miss, that row reopens to `pending` with reason `snowball_miss`.
- **As a direct fetch list.** A slot looks up the missing citation directly, but only once that reporter × year has finished its normal enumeration.

> Acceptance test: the report shows the snowball miss count per reporter × year, and every miss is either fetched, explained (e.g. cites a reporter not on PLS), or pending.

**R2.12 Reopens.** The current year and the previous year are reopened weekly. Any year with a snowball miss is reopened as soon as the miss is found.

**R2.13 Final reconciliation sweep.** Run this after all ledger rows are `done`, then monthly:

1. Re-query each reporter × year total and compare it with the ledger.
2. Run judge and keyword fallback searches only for reporter × years that still show a gap.
3. Re-run snowball across the whole corpus.
4. Produce the signed-off completeness proof.

Judge and keyword searches are never part of normal enumeration.

---

## 3. Session and slot management

### 3.1 The two accounts

**R3.1 Fixed roles, no cross-over.** Account/slot 1 is the acquisition account. Account/slot 2 is
the independent verification account. Search harvest, citation-grid harvest and case-ID walks run
only on Account 1. Account 2 never takes over acquisition when Account 1 is unavailable. Likewise,
verification never falls back to Account 1.

> Acceptance test: scheduler, dispatcher, continuation helpers and manual task entry points refuse
> acquisition work on slot 2. Verification tests assert every verifier browser call uses slot 2.

**R3.2 Independent verification.** At each verifier cycle Account 2 performs both:

1. **Document verification:** randomly select promoted PLS judgments, open each live source and compare
   substantive text and identity with the corpus.
2. **Coverage challenge:** randomly select search queries already marked `done`, re-run them independently,
   and compare live result identities against corpus/staging. Any missing identity reopens the affected
   query to `pending`; no human has to notice the omission first.

A verifier run that cannot authenticate, cannot express the query, or cannot identify live result
keys records **no completeness verdict**. Silence is never treated as proof of completeness.

**R3.3 Session recovery preserves role separation.** Saved credentials may re-authenticate each account,
but recovery never changes the account's role. If Account 1 fails, harvesting pauses. If Account 2
fails, completeness becomes unverified until independent checking resumes. Neither condition authorises
borrowing the other account.

**R3.4 Rate limits and politeness.** Each account has its own pacing counters. Only one request is in
flight per account. Back off on HTTP 429/5xx and stop on CAPTCHA, suspension or explicit automated-access
warnings. No proxy, stealth or technical-control bypass is permitted.

**R3.5 Lockout risk.** PLS is a paid subscription with two accounts. Never open a second browser on one
account while its worker holds that session. Never use a third login location while the server holds
the account. Account separation is a correctness control as well as an operational control.

### 3.2 Keeping the login queue free

**R3.6 Separate queues for each job type.** The rule is one login-bound queue per slot, used only for harvest ticks. Everything else goes elsewhere:

| Queue | Jobs | Needs login? |
|---|---|---|
| `login_session` | Account-1 PLS harvest/search/case-ID acquisition | Yes — Account 1 only |
| `login_session` verifier task | Account-2 judgment checks + coverage challenges | Yes — Account 2 only |
| `login_recovery` | role-preserving account recovery | Yes |
| `maintenance` | promotion, reported↔unreported reconciliation, reports, archive reconcile | No |
| `scraper` | public sources | No |
| `embeddings` | embeddings | No |

Grid-walk-only jobs and the case-ID walk run inside the harvest tick's time budget. They never run as separate long jobs that hold the slot.

**R3.7 Harvest ticks never expire.** Celery `expires` must not be set to the schedule interval. That was the bug: `expires = 900 s` while ticks ran about 1,200 s, so queued ticks were revoked before they ran. Instead:

- Harvest ticks have no expiry.
- A **singleton lock per slot** stops duplicate ticks from piling up. The new tick exits at once if one is already running.

> Acceptance test: over 24 h, the count of revoked harvest tasks is 0, and the count of overlapping ticks per slot is 0.

**R3.8 Short ticks that commit per row.** A tick has a time budget (*example*: 10 minutes) well below the schedule interval. It saves the cursor and ledger after **every** row and page, and stops cleanly when the budget is used up. The next tick resumes from the saved cursor.

**R3.9 The case-ID walk (when re-enabled):**

- It must reach a real judgment page. A landing on the search form counts as a miss.
- **Circuit breaker:** if 10 of the last 20 attempts miss, the walk stops for 24 h and alerts.
- Its time per tick is capped at a share of the tick budget (*example*: 20%). The past failure was 857 s of login queue time per tick with 60/60 misses.

> Acceptance test: in staging, a run against known case IDs gets ≥ 90% hits. Forcing misses trips the breaker within 20 attempts.

---

## 4. Durability

**R4.1 Commit progress per row.** For each grid row, the tick saves the outcome (`seen`, `fetched`, `quarantined`, or `error`) and the new cursor position in the same DB transaction. If the process is killed between rows, at most one row is repeated.

**R4.2 Persist the cursor properly.** `cursor_json` must be **replaced** with a new object and saved, not changed in place. In the bug, the row position was changed in place, the ORM never noticed, nothing was saved, and every 20-minute tick re-walked about 65 known rows to stage 1. Ops added a cursor guard that resyncs every minute. That guard is a stopgap and should stay only until this fix is proven.

> Acceptance test: a unit test advances the cursor, reloads it from the DB in a new session, and asserts the new position. In production, ticks show `rows_walked` close to `rows_new + rows_on_new_pages`, not a steady ~65.

**R4.3 Idempotent restarts.** Every job can be re-run safely:

- Fetches upsert by citation key.
- Mirror uploads check the file hash first.
- Promotion checks whether the row is already promoted.
- Migrations can run twice.

**R4.4 Deploy lock.** There is exactly one deploy path: one cron or one manual command, never two. It takes a DB or Redis lock (`deploy_lock`) with an owner and a TTL. A second deployer that finds the lock exits and logs. This prevents a repeat of the two-cron collision.

**R4.5 Deploys drain, not kill:**

1. Set a `draining` flag.
2. Harvest ticks finish their current row, save the cursor, and exit.
3. Wait for running tasks up to a timeout (*example*: 15 min).
4. Restart the containers.
5. Clear the flag.

A deploy never interrupts a tick in the middle of a page.

**R4.6 Deploy and auto-restart thresholds must exceed normal tick time.** The deployer stopped a healthy harvest because its "stalled" threshold was 0.33 h. Its stall test must be the multi-signal stall (R6.1), not a single timer, and it must respect the singleton lock.

**R4.7 Guards must not duplicate scheduled jobs.** A scheduled task must exist only once in the Celery beat schedule. The "duplicate maintenance task starved promotion" incident came from this. Startup must fail if a task name is registered twice.

---

## 5. Parsing and quality

### 5.1 The citation key standard

**R5.1 Canonical key = `REPORTER|YEAR|COURT|PAGE`.**

- **Reporter:** one canonical spelling per reporter. Variants map to it: "P L D", "P.L.D.", "PLD" → `PLD`; "P Cr L J", "PCrLJ", "PCRLJ" → `PCRLJ`; "PLC (C.S.)", "PLC(CS)" → `PLC(CS)`. The parser must handle spaced and dotted forms for **every** reporter, including PLC, CLD and GBLR.
- **Year:** 4 digits.
- **Court:** required for reporters that number pages separately per court, such as PLD (e.g. `PLD|2024|LAHORE|12` and `PLD|2024|SC|12` are different judgments). Court names map to a fixed list: "Lah.", "Lahore" → `LAHORE`; "S.C.", "Supreme Court" → `SC`. Claude keeps a per-reporter flag, `court_in_key`, confirmed against how the reporter is printed. For reporters numbered across the whole volume (e.g. *example*: SCMR), court is stored but left out of the key.
- **Page:** an integer.

> Acceptance test: a test table with at least these forms passes: `P L D 2024 Lahore 12`, `PLD 2024 SC 12` (different keys), `2024 PLC 345`, `2024 P L C (C.S.) 10`, `2020 CLD 1`, `2019 GBLR 5`, `2018 PCrLJ 77`. Re-parsing the 45 dropped PLD 2026 judgments gives 45 distinct keys.

**R5.2 Duplicate rules:**

- Two rows are duplicates only if their **canonical keys** match.
- A judgment printed in several reporters is one judgment with several citations. Keep one judgment record and a `citations` list. Use the text fingerprint (normalised text hash) plus parties and date to link them, never to drop them.
- Never delete on suspicion of duplication. Mark the row `possible_duplicate` and keep it.

**R5.3 Extracting parties, court and date:**

- Parties: split on "versus", "vs.", "v." at the header level only. Strip the reporter's header lines (e.g. PLC headers that polluted the respondent field) before extracting. Respondent must not contain the citation, the court name or headnote text.
- Court: take it from the citation where the reporter gives it, otherwise from the header.
- Date: the decision date ("Date of hearing", "decided on"), stored as an ISO date, and kept separate from the hearing date.

> Acceptance test: on a sample of 50 PLC judgments, the respondent field has no reporter header strings (an automated regex check plus 10 manual Auditor checks).

### 5.2 Quarantine

**R5.4 Quarantine reasons.** Each quarantined row has exactly one main reason from a fixed list:

- `no_citation_parsed`
- `citation_not_supported_by_source`
- `short_text`
- `placeholder_text`
- `no_reporter`
- `party_parse_failed`
- `possible_duplicate`
- `fetch_error`

Free-text reasons are not allowed.

**R5.5 Automatic re-qualify.** Whenever the parser or a threshold changes, a `requalify` job re-runs the gates on all quarantined rows of the affected reasons. Rows that now pass are promoted automatically. This is how the rows quarantined over "P L D", PLC, CLD and GBLR come back without any human action.

**R5.6 Confidence thresholds per source.** Each source has its own cutoff in config:

- **PLS keeps 0.85.**
- **SHC public:** SHC public rows were mass-quarantined with confidence 0.4–0.8 against the 0.85 cutoff, because public court pages often show no reporter citation at all. For SHC (and similar public sources) the citation is optional. Their identity key is court + case number + decision date. Claude sets an SHC-specific threshold based on the observed distribution, then re-qualifies.

> Acceptance test: the SHC quarantine rate drops, and a 20-row Auditor sample of newly promoted SHC rows shows correct court, case number and date.

**R5.7 Short-text and placeholder detection.** Texts under 1,000 characters are flagged `short_text` (119 such texts exist now). A row is `placeholder_text` if it matches login-wall, "subscribe" or "not found" templates, or the search-form HTML. Both reasons trigger an automatic re-fetch, at most 2 tries, before the row stays quarantined.

**R5.8 Rows without a reporter** (225 now): re-run the citation parser on the full text and header. If a reporter is still missing, use the reporter of the search that found the row (it is known from the ledger row). Record which source the reporter came from.

**R5.9 Human-free re-parse backfills.** Every parser change ships with a backfill job that re-parses affected rows from stored raw HTML. It never re-fetches from PLS, so it does not touch the slots. The backfill runs in batches on the `parse` queue and logs how many rows changed.

---

## 6. Monitoring

**R6.1 Stalls are judged on several facts.** "Stalled" is true only when there is work to do **and** progress has stopped. The watchdog computes:

| Signal | Stall if… |
|---|---|
| New judgments stored | 0 in the last 60 min (*example* window) |
| Harvest tick completions per slot | None in 2× the schedule interval while that slot has `pending` or `in_progress` rows |
| Ledger advancement | `last_page_read` hasn't moved on any `in_progress` row in 2× the interval |
| Queue age | Oldest message in `harvest_*`, `promote`, or `mirror` is older than 2× its interval |
| Slot probe | `slot_ok = false` or `last_ok_at` older than 15 min |

The watchdog reports **which** signals fired. The old watchdog tracked only a no-judgment threshold and said stalled=False during real stalls. One fired signal is a warning. Two or more, or any slot probe failure, is a stall.

> Acceptance test: in staging, pause the harvest worker. Within 2× the interval the watchdog reports `stalled=True` with the tick and ledger signals named.

**R6.2 Dashboard lights reflect real paths.** One light per real pipeline stage: Slot A, Slot B, Harvest A, Harvest B, Parse, Promote, Mirror, Embed, Statutes, Public courts. Each light is driven by the R6.1 signals for that stage. Remove the lights tied to legacy job types (the red light that showed while the harvest was healthy).

**R6.3 Self-healing guards that are allowed** (they act and log, then alert only if the action fails twice):

- Re-login a slot (R3.3).
- Queue one non-expiring harvest tick after 30 flat minutes (the flat guard).
- Restart the harvest worker if a tick runs more than 35 min.
- Resync the cursor (the cursor guard, until R4.2 is proven).
- Kick promotion if its queue is stale (the promotion guard).
- Re-fetch short or placeholder text (R5.7).
- Re-qualify after a threshold change (R5.5).

**R6.4 Actions that require an alert, never self-heal:**

- Repeated login failure.
- A captcha, warning or "in use elsewhere" message on PLS.
- A canary failure.
- A form-map dropdown change.
- The case-ID breaker tripping.
- Any guard firing more than 3 times in 24 h (a sign of a real bug).
- Any DB row count going **down**.
- A Drive mirror gap growing over 24 h.
- Statutes flat for more than 24 h while the source is ACTIVE.

**R6.5 Guards are temporary.** Each guard is listed with the bug it covers. Once Claude's fix passes its acceptance test for 7 days, the guard is either removed or kept as a backstop that alerts when it fires.

**R6.6 Daily coverage report** (08:00 PKT, emailed to Shamail and saved to Drive):

- Totals: judgments, statutes, quarantined by reason, and the rate per hour over the last 24 h.
- Ledger summary by status, plus the 20 largest gaps (reporter × year, site_total, stored, quarantined_explained, gap).
- Rows completed yesterday, reopened yesterday, and blocked (with reasons).
- Slot health, guard firings, incidents.
- Drive mirror and embedding backlog.
- The statutes lane verdict (R7.4).

> Acceptance test: the report arrives daily for 7 days in a row, and its totals match a direct DB query exactly.

---

## 7. Statutes lane

**R7.1 Sources:** PakistanCode is the primary source. Federal and provincial assembly sites fill gaps and supply recent Acts and Ordinances. The statutes lane runs on the `public` queue and never uses PLS slots.

**R7.2 Statute identity key:** jurisdiction + short title + year + number (where printed). Amendments are linked to the parent Act and never stored as duplicates.

**R7.3 Statute ledger:** one row per source × listing (e.g. PakistanCode by alphabet letter, by year, or by category, whichever the live site offers). It records `listed_count`, `stored`, `status`, and uses the same state machine and done rule as Section 2.

**R7.4 The exhausted-vs-stuck test.** Statutes have been flat at 12,618 since 2026-10-11 04:03. Treat this as **unknown** until the test is run.

1. **Count the source.** Enumerate all PakistanCode listing pages without fetching statute bodies, and count the distinct statutes listed (`source_count`).
2. **Compare.** Compute `missing = source_listing − DB` by identity key.
3. **Check job activity.** Did statute ticks run in the last 24 h, and how many items did they process (fetched, skipped as known, errored)?
4. **Verdict:**
   - **Exhausted** if `missing = 0` (or every missing item has an explained reason) **and** ticks are running and skipping only known items.
   - **Stuck** if `missing > 0` and ticks are failing, not running, or processing 0 items, or if the listing request itself fails or returns the wrong page (as happened with the PLS form).
   - **Stuck (cursor)** if `missing > 0` and ticks run but keep re-reading the same listing slice.
5. **Assemblies:** repeat steps 1–4 per assembly site.

> Acceptance test: the report prints `statutes: source_count=X, stored=Y, missing=Z, verdict=…` daily. A "stuck" verdict raises an alert.

**R7.5 When exhausted**, re-check weekly for new Acts (newest-first listing) and run step 1 monthly.

---

## 8. Storage

**R8.1 The DB first, always.** The DB (Postgres) is the system of record. A judgment counts as stored when its row is committed with raw HTML, clean text, parsed fields, citation keys and quality status.

**R8.2 Drive is a mirror.** After commit, the `mirror` queue uploads to Google Drive in a fixed folder layout: `Judgments/<Reporter>/<Year>/<canonical-key>.pdf|txt` and `Statutes/<Jurisdiction>/<Year>/…`. The Drive file ID is stored on the DB row. Drive is never the place where a record originates.

**R8.3 Daily reconcile.** Compare DB rows that have a Drive ID with the files listed on Drive. Re-upload anything missing. Re-link orphans found on Drive to their DB rows by key. Report the counts.

> Acceptance test: `db_mirrored = drive_files` per reporter folder, and the mirror backlog stays under 24 h.

**R8.4 Embeddings.** Each promoted judgment and statute is chunked and embedded on the `embed` queue. The embedding records model name and version and the text hash, and is re-embedded only when the text hash changes. Quarantined rows are not embedded.

**R8.5 Backups.** Nightly DB dump with off-droplet retention (DigitalOcean backups or Spaces). Restore is tested monthly.

**R8.6 Never wipe the DB.** No `DROP`, `TRUNCATE`, mass `DELETE`, or volume removal (`docker compose down -v`) on production, by anyone, for any reason. Corrections are made with additive migrations, status changes, or soft deletes.

---

## 9. Roles and governance

**R9.1 Claude Code is the only code author.** All code changes are made on `claude/ssh-scraper-performance-ks50bv` (or branches merged into it). Each change ships with tests and, where relevant, a backfill or migration.

**R9.2 Cursor helps only when Shamail asks.** It works by opening PRs **into Claude's branch** (like PR #197). Claude reviews and merges. Cursor never pushes to the deploy branch directly.

**R9.3 New Bot is the ops operator, ops only.** It may:

- watch, restart services, run approved guards and jobs, and run deploys through the locked deploy path
- read logs and the DB
- re-login slots

It does **not** edit code, change config thresholds, run ad-hoc SQL writes, or log in to PLS from anywhere except the server.

**R9.4 Scraper Auditor does read-only audits.** It reads the DB, logs and the site, runs spot checks and completeness checks, and reports. It never writes anything.

**R9.5 The stop/resume protocol:**

- **Stop:**
  1. Set `draining`.
  2. Wait for ticks to save and exit.
  3. Pause beat schedules.
  4. Record the reason and time in the ops log.
- **Resume:**
  1. Run the slot probes and the canary.
  2. Clear `draining`.
  3. Unpause beat.
  4. Confirm one harvest tick per slot completes and the ledger advances.
  5. Record it in the ops log.
- Sessions that expired during the stop are handled by automatic re-login (R3.3).

**R9.6 Change control.** Any change to rate limits, slot shares, confidence thresholds or the definition of done needs Shamail's written OK in chat. Claude records the change and the reason in `docs/CHANGELOG-ops.md`.

---

## 10. Prioritised action plan: the next 10 changes

| # | Change | Owner | Acceptance test |
|---|---|---|---|
| 1 | **Merge Cursor PR #197** (done only if the total is known and fully read, or an empty page is confirmed; reopen migration) | Claude reviews and merges; New Bot deploys | 0 `done` rows with `site_total IS NULL` and no recorded empty page. CLC 2019, PLC 2019, PCrLJ 2018 and MLD 2019 are back to `pending` or `in_progress` and advance past page 1. |
| 2 | **Un-expire harvest ticks** and add a singleton lock per slot; move grid-walk and other non-harvest jobs off the login queue (R3.6–R3.7) | Claude | 24 h with 0 revoked harvest tasks and 0 overlapping ticks per slot. The flat guard fires 0 times. |
| 3 | **Persist the cursor**: replace `cursor_json`, never change it in place; commit per row (R4.1–R4.2) | Claude | Reload test passes. Production ticks stop re-walking about 65 known rows. Cursor guard resyncs find nothing to fix for 7 days, after which the guard is retired to alert-only. |
| 4 | **Fix known-row matching** in the results grid using the canonical key normaliser (R2.4, R5.1), including court-aware PLD keys and spaced reporters; re-qualify quarantine | Claude | Re-walking a stored page opens 0 judgments. The 45 PLD 2026 judgments are restored as distinct rows. Rows quarantined over "P L D", PLC, CLD and GBLR are re-qualified. |
| 5 | **Fix and re-enable the case-ID walk** with real-page detection, a miss circuit breaker and a time cap (R3.9) | Claude; New Bot enables it | Staging hits ≥ 90% on known IDs. The breaker trips within 20 forced misses. In production, the walk's share of login-queue time stays within its cap. |
| 6 | **Fix the watchdog and dashboard**: multi-signal stall detection and lights per real stage (R6.1–R6.2) | Claude | Pausing the worker in staging gives `stalled=True` with named signals. During normal running every light is green and no legacy light remains. |
| 7 | **Fix the PLC party parser**: strip header lines and validate the respondent field (R5.3); backfill from raw HTML | Claude | A regex check over all PLC rows finds no header strings in respondent. The Auditor confirms 10 of 10 samples. |
| 8 | **SHC thresholds**: per-source cutoff and a non-citation identity key (R5.6); re-qualify. Also handle short texts and missing reporters (R5.7–R5.8) | Claude; Shamail approves the threshold | The SHC quarantine rate drops and a 20-row Auditor sample is correct. The short-text count (119) and the no-reporter count (225) each go down or carry explained reasons. |
| 9 | **Statutes check**: build the PakistanCode listing count and the exhausted-vs-stuck verdict (R7.4) | Claude builds it; New Bot runs it | The report prints source_count, stored, missing and verdict. If stuck, a fix ticket is opened the same day. |
| 10 | **Daily coverage report** (R6.6), including the ledger gap table and snowball misses | Claude | Delivered for 7 days in a row, and its totals match DB queries exactly. |

**After the 10:** complete the form-map refresh and canary (R2.2–R2.3) if they are not already in place; set up the deploy lock and drain (R4.4–R4.5); set up court and page-range splitting (R2.9); run the snowball pipeline at full scale (R2.11); run the final reconciliation sweep (R2.13).

---

## 11. Appendix: incident table (last three weeks, to 12 Oct 2026)

| Date | Symptom | Root cause | Fix | Guard |
|---|---|---|---|---|
| Last 3 wks | Harvest stalled; searches returned nothing useful | '1' typed into the Citation box; reporter value `PCrLJ` vs the site's `PCRLJ` | Map the form from the live site; validate dropdown values (R2.2) | Canary query per slot (R2.3) |
| Last 3 wks | Rows quarantined; 45 PLD 2026 judgments dropped | Spaced "P L D" and PLC/CLD/GBLR not parsed; PLD keys stored without the court | Canonical key with court (R5.1); re-qualify (R5.5) | Parser test table; dedupe never deletes (R5.2) |
| Last 3 wks | Each 20-min tick re-walked about 65 known rows and staged 1 | `cursor_json` changed in place, so never saved | Replace and save the cursor; commit per row (R4.1–R4.2) | Ops cursor guard resyncs every minute |
| Last 3 wks | Already-stored judgments fetched again | Grid rows not matched to DB keys | Shared normaliser for the DB check (R2.4) | Log `known` vs `opened` per page |
| Last 3 wks | Healthy harvest stopped mid-grid | Two auto-deploy crons collided; 0.33 h stall threshold; no lock | One deploy path with a lock; drain, not kill (R4.4–R4.6) | Deploy lock |
| Last 3 wks | Harvest starved | Single login queue held by grid-walk jobs; tick expiry 900 s vs ~1,200 s runtime, so ticks revoked | Separate queues; non-expiring ticks; singleton lock (R3.6–R3.7) | Flat guard (non-expiring tick after 30 flat min); worker restart if a tick runs over 35 min |
| Last 3 wks | Case-ID walk 60/60 misses; 857 s of login queue per tick | Case-ID URLs landed on the search form | Disabled; to be fixed with real-page detection (R3.9) | Miss circuit breaker (planned) |
| Last 3 wks | CLC 2019, PLC 2019, PCrLJ 2018, MLD 2019 closed after page 1 | Marked done when `site_total_results` was None | PR #197 done rule plus reopen migration (R2.7) | Daily query: no `done` rows with an unknown total and no empty page |
| Last 3 wks | Logins expired | 16 h of downtime | Automatic server re-login with saved credentials (R3.3) | Slot probe every 5 min |
| Last 3 wks | Promotion starved | Duplicate maintenance task | Remove the duplicate; fail on a duplicate beat entry (R4.7) | Ops promotion guard |
| Last 3 wks | stalled=False during real stalls; red lights while healthy | Watchdog used only a no-judgment threshold; lights tracked a legacy job type | Multi-signal watchdog; lights per stage (R6.1–R6.2) | Alert rules (R6.4) |
| Last 3 wks | SHC rows mass-quarantined; polluted respondents; 119 short texts; 225 with no reporter | 0.85 cutoff vs 0.4–0.8 SHC confidence; PLC headers in party field; weak short-text and reporter handling | Per-source thresholds; PLC parser fix; re-fetch; reporter from ledger (R5.3–R5.8) | Re-qualify job; daily quarantine counts |
| From 2026-10-11 04:03 | Statutes flat at 12,618 while PakistanCode is ACTIVE | Unknown (exhausted or stuck) | Exhausted-vs-stuck test (R7.4) | Alert if flat for more than 24 h while the source is ACTIVE |

*Figures in this handbook come from the incident record to 12 Oct 2026: about 34,600 judgments, about 130–240 an hour, and 12,618 statutes. Values marked "example" are for illustration only.*
