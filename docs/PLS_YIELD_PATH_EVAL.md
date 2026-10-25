# LOW — next yield path evaluation (report only)

**Status:** recommendation for Monitor; **no ops enablement** in this change set.

After keyword/party harvest filters apply correctly and linkless pagination gaps can narrow via splits, the next incremental yield path is the **case-ID / citation-grid walk** (`scraper/pls_caseid.py`): enumerate judgments by site case serial (year + court letter + serial) where CitationSearch result lists truncate or omit detail links.

**Why defer until (a)(b) land:** grid/case-ID walks compete for the same login-session budget and promotion queue; running them while search-harvest parents are falsely stalled would duplicate work and mask gap accounting.

**Suggested follow-up (engineering only):** pilot one reporter-year with known pagination gap, compare `pls_caseid` frontier yield vs keyword-split children, then decide whether to enable `PLS_CASEID_*` settings in a separate flagged PR.
