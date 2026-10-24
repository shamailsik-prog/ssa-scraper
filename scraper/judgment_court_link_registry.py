"""Map PLS ``court_name`` labels with ``court_id`` NULL to ``court`` rows (follow-up to #292)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from scraper.extractors.validation import PLS_COURT_LABELS
from scraper.parsers.foreign_court_registry import FOREIGN_COURT_SEEDS, normalize_judgment_court_name

# Operator review only — never auto-link (main counts: SUPREME-COURT 44, BAGHDAD-UL-JADID 22, Labour 1).
MANUAL_REVIEW_LABEL_KEYS = frozenset(
    {
        "supreme court",
        "baghdad ul jadid",
        "labour",
    }
)

# Seeded Pakistani superior courts (``database.COURT_SEED`` short codes).
SEEDED_COURT_BY_KEY: Dict[str, str] = {
    "supreme court azad kashmir": "AJK-SC",
    "supreme court of azad kashmir": "AJK-SC",
    "supreme court azad jammu and kashmir": "AJK-SC",
    "supreme court of azad jammu and kashmir": "AJK-SC",
    "high court azad kashmir": "AJK-HC",
    "high court of azad kashmir": "AJK-HC",
    "high court azad jammu and kashmir": "AJK-HC",
    "high court of azad jammu and kashmir": "AJK-HC",
    "azad j k": "AJK-HC",
    "federal shariat court": "FSC",
    "fsc": "FSC",
    "fed shariat court": "FSC",
    "shariat appellate bench supreme court": "SC-SAB",
    "shariat appellate bench": "SC-SAB",
    "supreme appellate court gilgit baltistan": "SAC-GB",
    "supreme appellate court gilgit-baltistan": "SAC-GB",
    "supreme appellate court gb": "SAC-GB",
}

_AJK_SC_DISAMBIG_KEYS = frozenset(
    k for k, code in SEEDED_COURT_BY_KEY.items() if code == "AJK-SC"
)

_AJK_HC_BRACKET = re.compile(r"(?im)^\s*\[\s*azad\s+j\s*&\s*k\s*\]\s*$")
_SINGLE_JUDGE_LINE = re.compile(r"(?im)\bBefore\s+[^,\n]{1,120},\s*J\b")

# Specialty bodies created idempotently when missing (not in ``COURT_SEED``).
ADDITIONAL_COURT_SEEDS: List[Dict[str, Any]] = [
    {
        "name": "Labour Appellate Tribunal Sindh",
        "short_code": "LAT_SINDH",
        "court_level": "tribunal",
        "aliases": ["LABOUR-APPELLATE-TRIBUNAL-SINDH", "Sindh Labour Appellate Tribunal"],
    },
    {
        "name": "Punjab Labour Appellate Tribunal",
        "short_code": "LAT_PUNJAB",
        "court_level": "tribunal",
        "aliases": ["PUNJAB-LABOUR-APPELLATE-TRIBUNAL", "Labour Appellate Tribunal Punjab"],
    },
    {
        "name": "Khyber Pakhtunkhwa Labour Appellate Tribunal",
        "short_code": "LAT_KPK",
        "court_level": "tribunal",
        "aliases": ["KPK-LABOUR-APPELLATE-TRIBUNAL", "Labour Appellate Tribunal KPK"],
    },
    {
        "name": "Balochistan Labour Appellate Tribunal",
        "short_code": "LAT_BALOCHISTAN",
        "court_level": "tribunal",
        "aliases": ["BALOCHISTAN-LABOUR-APPELLATE-TRIBUNAL"],
    },
    {
        "name": "National Industrial Relations Commission",
        "short_code": "NIRC",
        "court_level": "commission",
        "aliases": ["NIRC", "NATIONAL-INDUSTRIAL-RELATIONS-COMMISSION"],
    },
    {
        "name": "Gilgit-Baltistan Chief Court",
        "short_code": "GB_CHIEF",
        "court_level": "high_court",
        "aliases": ["GILGIT-BALTISTAN-CHIEF-COURT", "Chief Court Gilgit-Baltistan", "GB Chief Court"],
    },
    {
        "name": "Federal Constitutional Court",
        "short_code": "FCC",
        "court_level": "constitutional",
        "aliases": ["FCC", "FEDERAL-CONSTITUTIONAL-COURT"],
    },
    {
        "name": "Board of Revenue, Punjab",
        "short_code": "BOR_PUNJAB",
        "court_level": "board",
        "aliases": ["BOARD-OF-REVENUE-PUNJAB", "Board of Revenue Punjab"],
    },
    {
        "name": "Board of Revenue, Sindh",
        "short_code": "BOR_SINDH",
        "court_level": "board",
        "aliases": ["BOARD-OF-REVENUE-SINDH", "Board of Revenue Sindh"],
    },
    {
        "name": "Board of Revenue, Khyber Pakhtunkhwa",
        "short_code": "BOR_KPK",
        "court_level": "board",
        "aliases": ["BOARD-OF-REVENUE-KPK", "Board of Revenue KPK", "BOARD-OF-REVENUE-NWFP"],
    },
]

_ELECTION_TRIBUNAL_RE = re.compile(r"^election tribunal\b", re.I)
_LABOUR_APPELLATE_RE = re.compile(r"labour appellate tribunal", re.I)
_BOARD_OF_REVENUE_RE = re.compile(r"^board of revenue\b", re.I)
_FOREIGN_LABEL_COUNTRY = re.compile(
    r"(?i)\b(?:india|bangladesh|england|u\.?k\.?|united\s+kingdom|sri\s+lanka|nepal|afghanistan|iran|china|usa|united\s+states)\b",
)


@dataclass(frozen=True)
class LinkResolution:
    target_court_name: str
    reason: str
    would_create_court: bool = False
    manual_review: bool = False


def pls_court_label_key(court_name: Optional[str]) -> str:
    if not court_name or not str(court_name).strip():
        return ""
    raw = str(court_name).strip()
    key = re.sub(r"[\s\-_]+", " ", raw.lower())
    return re.sub(r"\s+", " ", key).strip()


def _title_from_label_key(key: str) -> str:
    return re.sub(r"\s+", " ", key.strip()).title()


def ajk_high_court_from_header(full_text: Optional[str]) -> bool:
    """True when an AJK-SC grid label is really a single-judge AJK High Court panel (e.g. 1979 CLC 288)."""
    if not full_text:
        return False
    head = full_text[:500]
    if not _AJK_HC_BRACKET.search(head):
        return False
    return bool(_SINGLE_JUDGE_LINE.search(head))


def resolve_null_court_id_link(court_name: str, full_text: Optional[str]) -> Optional[LinkResolution]:
    """Resolve a NULL ``court_id`` row to a canonical ``court.name`` (no DB)."""
    key = pls_court_label_key(court_name)
    if not key:
        return None
    if key in MANUAL_REVIEW_LABEL_KEYS:
        return LinkResolution(target_court_name=court_name, reason="manual_review", manual_review=True)

    pls_mapped = PLS_COURT_LABELS.get(key)
    if pls_mapped:
        if pls_mapped == "Azad Jammu and Kashmir Supreme Court":
            if ajk_high_court_from_header(full_text):
                return LinkResolution(
                    target_court_name="High Court of Azad Jammu and Kashmir",
                    reason="ajk_sc_label_single_judge_hc_header",
                )
            return LinkResolution(target_court_name=pls_mapped, reason="pls_court_labels")
        return LinkResolution(target_court_name=pls_mapped, reason="pls_court_labels")

    short = SEEDED_COURT_BY_KEY.get(key)
    if short == "AJK-SC":
        if ajk_high_court_from_header(full_text):
            return LinkResolution(
                target_court_name="High Court of Azad Jammu and Kashmir",
                reason="ajk_sc_label_single_judge_hc_header",
            )
        return LinkResolution(
            target_court_name="Azad Jammu and Kashmir Supreme Court",
            reason="seeded_court_label",
        )
    if short:
        from scraper.database import COURT_SEED

        for name, code, *_rest in COURT_SEED:
            if code == short:
                return LinkResolution(target_court_name=name, reason="seeded_court_label")

    for spec in ADDITIONAL_COURT_SEEDS + FOREIGN_COURT_SEEDS:
        labels = {spec["name"], spec["short_code"], *(spec.get("aliases") or [])}
        norm_labels = {pls_court_label_key(normalize_judgment_court_name(lbl) or lbl) for lbl in labels}
        norm_labels.add(pls_court_label_key(spec["name"]))
        if key in norm_labels:
            return LinkResolution(target_court_name=spec["name"], reason="additional_or_foreign_seed")

    if _LABOUR_APPELLATE_RE.search(key) and key not in MANUAL_REVIEW_LABEL_KEYS:
        display = _title_from_label_key(key)
        return LinkResolution(target_court_name=display, reason="labour_appellate_tribunal", would_create_court=True)

    if _ELECTION_TRIBUNAL_RE.search(key):
        display = _title_from_label_key(key)
        return LinkResolution(target_court_name=display, reason="election_tribunal", would_create_court=True)

    if _BOARD_OF_REVENUE_RE.search(key):
        for spec in ADDITIONAL_COURT_SEEDS:
            if not spec["short_code"].startswith("BOR_"):
                continue
            labels = {pls_court_label_key(lbl) for lbl in [spec["name"], *(spec.get("aliases") or [])]}
            if key in labels:
                return LinkResolution(target_court_name=spec["name"], reason="board_of_revenue_seed")
        display = _title_from_label_key(key)
        return LinkResolution(target_court_name=display, reason="board_of_revenue", would_create_court=True)

    if key in {"nirc", "national industrial relations commission"}:
        return LinkResolution(
            target_court_name="National Industrial Relations Commission",
            reason="nirc_seed",
        )

    if "chief court" in key and ("gilgit" in key or "baltistan" in key or key.startswith("gb ")):
        return LinkResolution(target_court_name="Gilgit-Baltistan Chief Court", reason="gb_chief_court")

    if key in {"fcc", "federal constitutional court"}:
        return LinkResolution(target_court_name="Federal Constitutional Court", reason="fcc_seed")

    canonical = normalize_judgment_court_name(court_name)
    if canonical and _FOREIGN_LABEL_COUNTRY.search(canonical):
        return LinkResolution(target_court_name=canonical, reason="foreign_label", would_create_court=True)

    for spec in FOREIGN_COURT_SEEDS:
        if pls_court_label_key(spec["name"]) == key:
            return LinkResolution(target_court_name=spec["name"], reason="foreign_seed")

    return None


def all_linkable_seed_specs() -> Sequence[Dict[str, Any]]:
    return [*ADDITIONAL_COURT_SEEDS, *FOREIGN_COURT_SEEDS]


def election_tribunal_seed_spec(display_name: str) -> Dict[str, Any]:
    slug = re.sub(r"[^A-Z0-9]+", "_", display_name.upper()).strip("_")[:40] or "ELECTION_TRIBUNAL"
    return {
        "name": display_name,
        "short_code": slug,
        "court_level": "tribunal",
        "aliases": [display_name],
    }
