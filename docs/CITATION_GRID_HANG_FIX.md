# PLS citation-grid 16MB hang fix

## Root cause
`_capture_html` only compacted `#archivedpatientGrid` when oversize thresholds tripped. CitationSearch often omits Content-Length and can sit under the input threshold while the live DOM is still ~10-16MB, so Playwright fell through to `page.content()` and hung with `pages_scraped=0`.

## Fix
- Always compact when the archived grid is present; never call `page.content()` on that surface
- Faster snapshot via `tbody.rows` (no Array.from of all rows, no tr.innerHTML)
- Snapshot timeout + progress logs; default PLS_ARCHIVED_GRID_MAX_ROWS=200
- Cap detail fetches via PLS_CITATION_GRID_MAX_DETAIL (default 40) with progress every 5 rows
- Preserve casetypeid URL synthesis and fixed Citation/Title/Court/Read headers
