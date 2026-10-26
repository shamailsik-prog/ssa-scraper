"""Recognise statute sections whose current text opens with a footnote date.

Auditor 2026-10-27, free-sites statute ``1032ddb0`` (The Bank of Azad Jammu &
Kashmir Act, 2005). ``11.10.2018.`` was stored as section 11, and the same
shape took sections 17 and 23. The real section body is not in that row, so
this module does not plan a rewrite. Re-extract from the source PDF after the
splitter refuses the line (``section_text_starts_with_amendment_date``).

This is not the 1800–2099 year-as-section class. A day or month is a small
number and collides with a real section. A bare ``2018.`` line is that other
class.
"""

from __future__ import annotations

# PostgreSQL POSIX regex. Same shape as AMENDMENT_DATE_LINE_RE.
# Visible current section text that starts with DD.MM.(19|20)YY.
AMENDMENT_DATE_SECTION_TEXT_SQL = r"^[[:space:]]*[0-9]{1,2}[.][0-9]{1,2}[.](19|20)[0-9]{2}"
