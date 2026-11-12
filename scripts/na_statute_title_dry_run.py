#!/usr/bin/env python3
"""Propose a statute name and year from local NA or Senate text.

Reads a hits.csv-style file and text that is already on disk. It prints the
name and year the ingest would keep. It does not open a database, does not
fetch, and does not write a statute row.

Text is the file named ``{id}.txt`` or ``{na_sha}.txt`` in ``--text-dir``, or
the local path in a ``text_path`` column. A row with no local file is
``no_local_text``. The stored name is not reused as a title. Pass
``--listing-column`` only when that column is a real listing title.

    python scripts/na_statute_title_dry_run.py --csv hits.csv --text-dir ./texts
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scraper.statute_quality.assembly_statute_title import (
    resolve_assembly_statute_title,
    strip_listing_chrome_source_url,
)

_COLUMNS = (
    "id",
    "proposed_name",
    "proposed_year",
    "source",
    "cleaned_source_url",
)


def _local_file(path: Path) -> Optional[Path]:
    if not path.is_file():
        return None
    return path


def _text_for_row(row: dict, text_dir: Optional[Path]) -> Optional[str]:
    """Return on-disk text. A URL is not fetched."""
    raw_path = (row.get("text_path") or "").strip()
    if raw_path and "://" not in raw_path:
        found = _local_file(Path(raw_path))
        if found is not None:
            return found.read_text(encoding="utf-8", errors="replace")
    if text_dir is None:
        return None
    for key in ("id", "na_sha"):
        label = (row.get(key) or "").strip()
        if not label or "/" in label or "\\" in label:
            continue
        found = _local_file(text_dir / f"{label}.txt")
        if found is not None:
            return found.read_text(encoding="utf-8", errors="replace")
    return None


def propose_rows(rows: list[dict], *, text_dir: Optional[Path], listing_column: str) -> list[dict]:
    proposed = []
    for row in rows:
        cleaned_url = strip_listing_chrome_source_url(row.get("source_url") or "")
        text = _text_for_row(row, text_dir)
        if text is None:
            proposed.append(
                {
                    "id": row.get("id") or "",
                    "proposed_name": "",
                    "proposed_year": "",
                    "source": "no_local_text",
                    "cleaned_source_url": cleaned_url,
                }
            )
            continue
        listing = (row.get(listing_column) or "").strip() if listing_column else ""
        decision = resolve_assembly_statute_title(
            text,
            listing_title=listing or None,
        )
        proposed.append(
            {
                "id": row.get("id") or "",
                "proposed_name": decision.name,
                "proposed_year": "" if decision.year is None else str(decision.year),
                "source": decision.source,
                "cleaned_source_url": cleaned_url,
            }
        )
    return proposed


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path, help="hits.csv-style rows")
    parser.add_argument("--text-dir", type=Path, help="directory of {id}.txt or {na_sha}.txt files")
    parser.add_argument(
        "--listing-column",
        default="",
        help="CSV column that holds a real listing title. The stored name is not used unless named here.",
    )
    args = parser.parse_args(argv)
    with args.csv.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    writer = csv.DictWriter(sys.stdout, fieldnames=_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in propose_rows(rows, text_dir=args.text_dir, listing_column=args.listing_column):
        writer.writerow(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
