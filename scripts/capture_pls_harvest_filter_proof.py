#!/usr/bin/env python3
"""Read-only PLS capture for SCMR (reporter-year) party/keyword split proof fixtures.

Run on the MAIN droplet with an ACTIVE login slot and ALLOW_LOGIN_SCRAPING=true:

  ALLOW_LOGIN_SCRAPING=true python scripts/capture_pls_harvest_filter_proof.py \\
    --slot 1 --reporter SCMR --year 2025 --letters m,z,a

Writes tests/fixtures/pls_keyword_filter_capture_20261025/:
  SCMR_2025_party-PARENT.html, SCMR_2025_party-<letter>.html, totals.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "pls_keyword_filter_capture_20261025"


def _main_sha() -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True).strip()
        )
    except Exception:
        return "unknown"


def _html_filename(reporter: str, year: int, label: str) -> str:
    if label == "parent":
        return f"{reporter}_{year}_party-PARENT.html"
    letter = label.removeprefix("child_keyword_").removesuffix("_low_selectivity")
    return f"{reporter}_{year}_party-{letter}.html"


def _md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


async def _capture_live(
    *,
    slot: int,
    reporter: str,
    year: int,
    letters: List[str],
) -> Dict[str, Any]:
    from sqlalchemy import select

    from scraper.auth.session_manager import ContinuityRunner, SessionManager, playwright_browser_factory
    from scraper.config import settings
    from scraper.database import SessionLocal
    from scraper.extractors.deterministic import extract_result_rows_deterministic
    from scraper.models import ScraperSource
    from scraper.pls_search_harvest_core import build_harvest_form_values
    from scraper.tasks.pakistanlawsite import PakistanLawSitePipeline
    from scraper.tasks.search_map import active_map, map_as_dict

    if not settings.login_scraping_effective:
        raise SystemExit("ALLOW_LOGIN_SCRAPING must be true for live capture")

    async with SessionLocal() as db:
        source = (
            await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanLawSite"))
        ).scalars().first()
        if source is None:
            raise SystemExit("PakistanLawSite source missing")
        manager = SessionManager(db, source)
        slot_row = await manager.slot(slot)
        if slot_row.state != "ACTIVE":
            raise SystemExit(f"slot {slot} is not ACTIVE ({slot_row.state})")
        m = await active_map(db, "PakistanLawSite")
        if m is None:
            raise SystemExit("no active search form map")
        search_map = map_as_dict(m)
        pipeline = PakistanLawSitePipeline(db, source)
        runner = ContinuityRunner(manager, playwright_browser_factory)
        pipeline.runner = runner
        pipeline.runner.preferred_slot_number = slot
        pipeline.runner.exclusive_slot = True

        captured_pkt = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        totals: Dict[str, Any] = {
            "slot": slot,
            "source": "PakistanLawSite",
            "endpoint": f"POST /Login/CitationSearch (book={reporter},year={year},party=<letter>)",
            "tip": _main_sha(),
            "captured_pkt": captured_pkt,
            "note": (
                "no site_total element in response; rows = data <tr> minus header; "
                "single response, no pager"
            ),
        }
        parent_bytes: bytes | None = None

        async def fetch_query(label: str, query_json: Dict[str, Any]) -> None:
            nonlocal parent_bytes
            values = build_harvest_form_values(search_map, query_json, {"page": 1})
            page = await pipeline.fetch_results(search_map, values)
            html = page.html or ""
            raw = html.encode("utf-8")
            extracted = extract_result_rows_deterministic(
                html=html,
                search_map=search_map,
                base_url=settings.PLS_BASE_URL,
            )
            rows = extracted.get("result_rows") or []
            fname = _html_filename(reporter, year, label)
            FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
            path = FIXTURE_DIR / fname
            path.write_bytes(raw)
            party = (query_json.get("party") or query_json.get("keyword") or "")
            key = "parent" if label == "parent" else party
            entry: Dict[str, Any] = {
                "rows": len(rows),
                "bytes": len(raw),
                "md5": _md5_bytes(raw),
            }
            if label == "parent":
                entry["party"] = ""
                parent_bytes = raw
            if label != "parent" and parent_bytes is not None and raw == parent_bytes:
                entry["identical_to_parent"] = True
            totals[key] = entry

        try:
            await fetch_query("parent", {"reporter": reporter, "year": year})
            for letter in letters:
                suffix = "_low_selectivity" if letter == "a" else ""
                await fetch_query(f"child_keyword_{letter}{suffix}", {"reporter": reporter, "year": year, "keyword": letter})
        finally:
            try:
                await runner.close()
            except Exception:
                pass
        await db.rollback()
        return totals


def _write_totals(totals: Dict[str, Any]) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / "totals.json"
    path.write_text(json.dumps(totals, indent=0) + "\n", encoding="utf-8")
    print(f"wrote {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture PLS harvest filter proof fixtures (read-only)")
    parser.add_argument("--slot", type=int, default=1)
    parser.add_argument("--reporter", default="SCMR")
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--letters", default="m,z,a", help="Comma-separated party/keyword child letters")
    args = parser.parse_args()
    letters = [k.strip() for k in str(args.letters).split(",") if k.strip()]

    from scraper.database import run_async

    totals = run_async(
        _capture_live(
            slot=args.slot,
            reporter=args.reporter,
            year=args.year,
            letters=letters,
        )
    )
    _write_totals(totals)
    parent_n = totals["parent"]["rows"]
    for letter in letters:
        child_n = totals[letter]["rows"]
        print(f"parent={parent_n} child_{letter}={child_n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
