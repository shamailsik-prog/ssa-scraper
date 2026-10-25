#!/usr/bin/env python3
"""Read-only PLS capture for SCMR (reporter-year) keyword split proof fixtures.

Run on the MAIN droplet with an ACTIVE login slot and ALLOW_LOGIN_SCRAPING=true:

  ALLOW_LOGIN_SCRAPING=true python scripts/capture_pls_harvest_filter_proof.py \\
    --slot 1 --reporter SCMR --year 2025 --keywords a,z

Writes tests/fixtures/pls_harvest_filter_proof/ (HTML + manifest with row counts and provenance).
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
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "pls_harvest_filter_proof"


def _main_sha() -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True).strip()
        )
    except Exception:
        return "unknown"


def _html_row(citation: str, title: str, case_name: str) -> str:
    href = (
        f"/Login/ReferenceCaseLawSearch?CaseName={case_name}&court=&Row=0&bookName=undefined"
    )
    return (
        f"<tr><td>{citation}</td>"
        f'<td><a href="{href}">{title}</a></td>'
        f"<td>Supreme Court</td></tr>"
    )


def build_results_html(rows: List[Dict[str, str]]) -> str:
    body_rows = "\n".join(
        _html_row(r["citation"], r["title"], r["case_name"]) for r in rows
    )
    return f"""<html><body>
<div id="rightmenu">
  <table><thead><tr><th>Citation</th><th>Title</th><th>Court</th></tr></thead>
  <tbody>
{body_rows}
  </tbody></table>
</div>
</body></html>"""


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _capture_live(
    *,
    slot: int,
    reporter: str,
    year: int,
    keywords: List[str],
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

        captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        manifest: Dict[str, Any] = {
            "captured_at_utc": captured_at,
            "main_sha": _main_sha(),
            "login_slot": slot,
            "reporter": reporter,
            "year": year,
            "queries": {},
        }

        async def fetch_query(label: str, query_json: Dict[str, Any]) -> None:
            values = build_harvest_form_values(search_map, query_json, {"page": 1})
            page = await pipeline.fetch_results(search_map, values)
            html = page.html or ""
            extracted = extract_result_rows_deterministic(
                html=html,
                search_map=search_map,
                base_url=settings.PLS_BASE_URL,
            )
            rows = extracted.get("result_rows") or []
            fname = f"{label}.html"
            manifest["queries"][label] = {
                "query_json": query_json,
                "form_values": values,
                "row_count": len(rows),
                "html_file": fname,
                "html_sha256": _sha256_text(html),
                "sample_citations": [r.get("citation") for r in rows[:5]],
            }
            FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
            (FIXTURE_DIR / fname).write_text(html, encoding="utf-8")

        try:
            await fetch_query("parent", {"reporter": reporter, "year": year})
            for kw in keywords:
                await fetch_query(f"child_keyword_{kw}", {"reporter": reporter, "year": year, "keyword": kw})
        finally:
            try:
                await runner.close()
            except Exception:
                pass
        await db.rollback()
        return manifest


def _write_manifest(manifest: Dict[str, Any]) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture PLS harvest filter proof fixtures (read-only)")
    parser.add_argument("--slot", type=int, default=1)
    parser.add_argument("--reporter", default="SCMR")
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--keywords", default="a,z", help="Comma-separated keyword children")
    args = parser.parse_args()
    keywords = [k.strip() for k in str(args.keywords).split(",") if k.strip()]

    from scraper.database import run_async

    manifest = run_async(
        _capture_live(
            slot=args.slot,
            reporter=args.reporter,
            year=args.year,
            keywords=keywords,
        )
    )
    _write_manifest(manifest)
    parent_n = manifest["queries"]["parent"]["row_count"]
    for kw in keywords:
        child_n = manifest["queries"][f"child_keyword_{kw}"]["row_count"]
        print(f"parent={parent_n} child_{kw}={child_n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
