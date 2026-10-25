"""Load read-only PLS keyword/party filter capture fixtures (live MAIN droplet)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "pls_keyword_filter_capture_20261025"

HTML_FILES = {
    "parent": "SCMR_2025_party-PARENT.html",
    "child_keyword_m": "SCMR_2025_party-m.html",
    "child_keyword_z": "SCMR_2025_party-z.html",
    "child_keyword_a_low_selectivity": "SCMR_2025_party-a.html",
}


def load_totals() -> Dict[str, Any]:
    path = FIXTURE_DIR / "totals.json"
    if not path.is_file():
        raise FileNotFoundError(f"missing {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_fixture_html(key: str) -> str:
    fname = HTML_FILES[key]
    return (FIXTURE_DIR / fname).read_text(encoding="utf-8")
