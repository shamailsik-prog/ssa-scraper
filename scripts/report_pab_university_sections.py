#!/usr/bin/env python3
"""Re-segment four Balochistan university acts. Offline. No database writes.

The corpus rows were produced by ``split_into_sections`` on PAB OCR text.
This command prints the section count that splitter now returns. It does not
connect to a database, does not fetch, and does not deploy.

A sha-pinned PDF under the repo fixtures is preferred when one is present.
The University of Balochistan report uses the real OCR text in the fixtures.
The other three acts still use their pattern fixtures when no pinned file
is present.

    python scripts/report_pab_university_sections.py
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scraper.parsers.statute_parser import split_into_sections

_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures"
_SEARCH_ROOTS = (
    _REPO_ROOT / "tests" / "fixtures",
    _REPO_ROOT / "tests" / "fixtures_statutes",
)
# Image-only PaperPort scan. pdftotext of this file is form-feeds only.
_UOB_SHA256 = "398d88c1a9a251e7d08a50dbf3bcd46088afa95ff7a04afef24c3d5dba58e67f"
_MAX_HASH_BYTES = 20_000_000

ACTS: Sequence[dict] = (
    {
        "label": "University of Balochistan Act III/1996",
        "statute_name": "The University of Balochistan Act No. III of 1996",
        "fixture": "statute_quality/pab_uob_1996_ocr.txt",
        "sha256": _UOB_SHA256,
    },
    {
        "label": "Lasbela University Act IX/2010",
        "statute_name": "Lasbela University of Agriculture, Water and Marine Sciences Act No. IX of 2010",
        "fixture": "statute_quality/pab_lasbela_act_2010_pattern.txt",
        "sha256": "",
    },
    {
        "label": "University of Turbat Act V/2012",
        "statute_name": "University of Turbat Act No. V of 2012",
        "fixture": "statute_quality/pab_turbat_act_2012_pattern.txt",
        "sha256": "",
    },
    {
        "label": "BUITEMS Amendment Act XII/2015",
        "statute_name": "BUITEMS (Amendment) Act No. XII of 2015",
        "fixture": "statute_quality/pab_buitems_amendment_2015_pattern.txt",
        "sha256": "",
    },
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _candidate_files() -> Iterable[Path]:
    for root in _SEARCH_ROOTS:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".pdf", ".txt", ".text"}:
                continue
            try:
                if path.stat().st_size > _MAX_HASH_BYTES:
                    continue
            except OSError:
                continue
            yield path


def _pdf_text_layer(path: Path) -> str:
    """Text layer only. An image-only scan returns an empty string."""
    try:
        completed = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            check=False,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    text = completed.stdout.decode("utf-8", errors="replace")
    if sum(ch.isalpha() for ch in text) < 40:
        return ""
    return text


def find_pinned_text(sha256: str) -> Tuple[Optional[str], Optional[Path], str]:
    """Return pinned text, the file it came from, and a short note.

    The published sha256 is of the PDF bytes. A file whose name contains that
    sha is used when it is text, or when it is a PDF with a text layer. An
    image-only PDF is not re-segmented.
    """
    if not sha256:
        return None, None, "no sha published for this act"
    matches: list[Path] = []
    for path in _candidate_files():
        if sha256 in path.name or (path.suffix.lower() == ".pdf" and _sha256_file(path) == sha256):
            matches.append(path)
    for path in matches:
        if path.suffix.lower() == ".pdf":
            text = _pdf_text_layer(path)
            if text:
                return text, path, "sha-pinned PDF text layer"
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if text.strip():
            return text, path, "sha-pinned text"
    if any(path.suffix.lower() == ".pdf" for path in matches):
        return None, matches[0], "pinned PDF has no text layer"
    return None, None, "not in repo fixtures"


def section_numbers_for(text: str, statute_name: str) -> list[str]:
    return [section["section_number"] for section in split_into_sections(text, statute_name)]


def report_lines() -> list[str]:
    """One block per act. Safe to print. Does not touch a database."""
    lines = [
        "PAB university section re-segmentation",
        "offline: no database writes, no network",
    ]
    for act in ACTS:
        fixture = _FIXTURE_DIR / act["fixture"]
        pinned_text, pinned_path, note = find_pinned_text(act["sha256"])
        if pinned_text is not None and pinned_path is not None:
            source = pinned_path
            source_note = note
            text = pinned_text
        else:
            source = fixture
            source_note = note
            text = fixture.read_text(encoding="utf-8")
        numbers = section_numbers_for(text, act["statute_name"])
        lines.append(act["label"])
        lines.append(f"  pinned_sha256: {act['sha256'] or '(none published)'}")
        lines.append(f"  pinned_file: {source_note}")
        lines.append(f"  source: {source.relative_to(_REPO_ROOT)}")
        lines.append(f"  section_count: {len(numbers)}")
        lines.append(f"  section_numbers: {', '.join(numbers)}")
    return lines


def main() -> int:
    print("\n".join(report_lines()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
