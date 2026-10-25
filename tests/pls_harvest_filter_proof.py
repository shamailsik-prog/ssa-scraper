"""Load read-only PLS harvest filter proof fixtures (manifest + HTML)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "pls_harvest_filter_proof"


def load_manifest() -> Dict[str, Any]:
    path = FIXTURE_DIR / "manifest.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"missing {path}; run scripts/capture_pls_harvest_filter_proof.py on MAIN droplet"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def load_fixture_html(name: str) -> str:
    path = FIXTURE_DIR / name
    return path.read_text(encoding="utf-8")
