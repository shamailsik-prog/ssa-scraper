# Paste as the first and only message to a Cursor Background Agent in the SCRAPER (corpus) repository.
# Attach FINAL_SCRAPER_PROMPT_3.md to the repo root before starting.

AUTONOMOUS RUN — do not ask me anything. Work until done, then post ONE report.

Build the corpus service exactly as specified in FINAL_SCRAPER_PROMPT_3.md in this repository.
This repository is the ONLY place Playwright and login scraping are permitted. It never shares
a runtime, a store or a credential with the SIKANDER AI application; the application receives
one read-only connection string (sikander_reader) and nothing else.

Order of work (do not reorder):
1. Files 1–33 of Section 15, every one complete — zero placeholders, zero TODOs, zero stubs.
2. Section 6 for PakistanLawSite in full: 6.1 human-login session model (interactive login
   rendered inside the dashboard; never solve any verification), 6.2 the 30-second reconnect
   on the same slot and same page, 6.3 the search-form map job, 6.4 Tiers 1–4, 6.5 the
   crawl_frontier and crawl_coverage tables, 6.6 the coverage and inspection dashboard.
3. Section 9A archive mirror with all seven target types, the folder tree created by the
   service, write-once, per-target failure isolation; Section 9 reconcile_storage.
4. Section 14 tests, all green, including: disconnect → 30 s → same slot → same cursor;
   verification page → NEEDS_HUMAN_LOGIN, no solving; HTTP 403 / "account suspended" /
   CAPTCHA → source HALTED, no slot switch; Tier 1 volume close after 40 misses; same
   judgment by two routes → one row; mirror target failing → others complete.
5. Guards: Settings raises unless ENVIRONMENT=chambers when ALLOW_LOGIN_SCRAPING=true;
   no try_recover_primary anywhere; Celery concurrency 1 for login_session sources;
   /export excludes full_text of login_session rows without admin confirmation.
6. README exactly as Section 15 item 33. Then CORRECTION_LOG.md listing every file.

Decision rules: firm values (DEPLOY_REGION, subscribed reporters, earliest year, mirror
targets, credentials) are dashboard or .env inputs — never invent them; use the most
conservative default where code needs a value and log it. Conflict → the prompt wins.
Test fails → fix the code. Never add a CAPTCHA solver, a second concurrent session, an
automatic slot rotation past a block, or any write path into the application.

Report (only message): PR link · files delivered n/33 · tests PASS/FAIL (names) ·
guards PASS/FAIL · Not done (reasons).
