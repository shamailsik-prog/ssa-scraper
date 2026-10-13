# Operations change log

Changes to rate limits, slot shares, confidence thresholds or the definition of done, each with the owner's
written approval and the reason (handbook R9.6, `docs/PLS_HARVEST_HANDBOOK.md`).

## 13 October 2026 — search harvest on both logins at once, with fixed reporter shares

**Approved by:** Shamail Sikander, in chat on 13 October 2026 ("make the PakistanLawSite search harvest run on BOTH
login slots at the same time, each on its own share of journals, with no overlap").

**What changed**

- **Slot shares (R3.1).** `PLS_SEARCH_HARVEST_SLOT1_REPORTERS` (default `PLD,SCMR,CLC,YLR,CLD,GBLR`) is login 1's
  share of the search-harvest ledger. Login 2 has every other reporter (PLC, PLC(CS), PCrLJ, MLD, PTD, PLJ and the
  rest) and every row with no reporter. Reporter names are compared without case, spaces or dots. A row's share is
  read from its `query_json` reporter at pick time, so editing the setting moves rows without rewriting the ledger;
  the new `slot` column records which login's tick last took each row (for the "no row touched by both slots in one
  day" audit).
- **Per-slot singleton lock (R3.7).** The single global "another search harvest is running" lock is replaced by one
  lock per slot (`PakistanLawSite:search_harvest:slot1`, `…:slot2`). Each tick opens only its own login.
- **No overlap.** Rows are read with `SELECT … FOR UPDATE SKIP LOCKED` and the row taken is held by a per-row Redis
  lock for the whole run (the run commits after every judgment, which would end a row lock).
- **Takeover.** A login that has been logged out (not ACTIVE) for `PLS_SEARCH_HARVEST_TAKEOVER_MINUTES` (default 30),
  counted from the last time it was known to work, has its share taken over by the other login once that login's
  own share has nothing pending.
- **Ticks never expire (R3.7).** Beat has `pls-search-harvest` (slot 1) and `pls-search-harvest-s2` (slot 2), each at
  least once per tick budget (`PLS_SEARCH_HARVEST_TICK_SECONDS`), with no
  `expires`; the dispatcher and the stall watchdog queue one tick per slot. A tick that worked a row queues the next
  tick for its own slot (`PLS_SEARCH_HARVEST_CONTINUE`, default on), so each login harvests continuously instead of
  once per beat interval.
- **Pick order.** Priority, then the oldest last run. A row whose site total is unknown keeps its planned priority;
  it used to be set to 1000 (the back of the ledger) because its gap read 0.
- **Repeated pages.** Rows a later page repeats (page 2 serving page 1 again) are neither opened nor counted again; two
  pages in a row with nothing new stop that run's pagination and say so in `last_error`.
- **Spot check.** The 30-minute check of three promoted judgments now runs between harvest pages on the harvest's own
  login when its turn is due, instead of being skipped because the login lock was held.

**Unchanged:** pacing per slot (R3.4), the DB lookup by citation key before any judgment is opened (R2.4), commit per
row (R4.1).

**Expected effect:** both logins show harvest activity at the same time; judgments per hour rise from about 107
towards 200 or more.

**Roll back:** set `PLS_SEARCH_HARVEST_SLOT1_REPORTERS` to every reporter to give login 1 the whole ledger, or
`PLS_SEARCH_HARVEST_CONTINUE=0` to return to one tick per beat interval per slot.
