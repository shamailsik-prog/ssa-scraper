"""Map PLS ``court_name`` labels with ``court_id`` NULL to ``court`` rows (follow-up to #292)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set

from scraper.extractors.validation import PLS_COURT_LABELS
from scraper.parsers.foreign_court_registry import FOREIGN_COURT_SEEDS, normalize_judgment_court_name

AJK_SC_NAME = "Azad Jammu and Kashmir Supreme Court"
AJK_HC_NAME = "High Court of Azad Jammu and Kashmir"
GB_SAC_NAME = "Supreme Appellate Court Gilgit-Baltistan"
GB_CHIEF_NAME = "Gilgit-Baltistan Chief Court"

# Operator review only — never auto-link.
MANUAL_REVIEW_LABEL_KEYS = frozenset(
    {
        "supreme court",
        "baghdad ul jadid",
        "labour",
        "shariat court azad kashmir",
        "shariat court azad jammu and kashmir",
        "shariat court ajk",
        "labour appellate tribunal azad jammu and kashmir",
        "labour appellate tribunal azad kashmir",
        "labour appellate tribunal ajk",
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
    "shariat appellate bench of supreme court": "SC-SAB",
    "shariat appellate bench sc": "SC-SAB",
    "supreme appellate court gilgit baltistan": "SAC-GB",
    "supreme appellate court gilgit-baltistan": "SAC-GB",
    "supreme appellate court gb": "SAC-GB",
    "supreme appellate court of gilgit baltistan": "SAC-GB",
    "supreme appellate court northern areas": "SAC-GB",
    "supreme appellate court of northern areas": "SAC-GB",
    "chief court northern areas": "GB_CHIEF",
    "chief court of northern areas": "GB_CHIEF",
    "chief court gilgit baltistan": "GB_CHIEF",
    "chief court of gilgit baltistan": "GB_CHIEF",
}

_AJK_SC_LABEL_KEYS = frozenset(k for k, code in SEEDED_COURT_BY_KEY.items() if code == "AJK-SC")

_AJK_HC_BRACKET = re.compile(r"(?im)^\s*\[\s*azad\s+j\s*&\s*k\s*\]\s*$")
_BEFORE_BENCH_RE = re.compile(
    r"(?im)^\s*before\s+.+?(?:,\s*)?(?:j\.?|jj\.?|c\.?\s*j\.?|cj)\.?\s*$",
)
_AJK_SHARIAT_RE = re.compile(
    r"(?im)shariat\s+court.*(?:azad|aj&?k|jammu|kashmir)|(?:azad|aj&?k|jammu|kashmir).*shariat\s+court",
)
_PRESENT_CORAM_RE = re.compile(r"(?im)^\s*present\s*:")
_AJK_HC_HEADING_RE = re.compile(r"(?im)\bhigh\s+court\b|\bh\.?\s*c\.?\b")
_AJK_SC_HEADING_RE = re.compile(r"(?im)\bsupreme\s+court\b|\bs\.?\s*c\.?\b")

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
        "aliases": [
            "KPK-LABOUR-APPELLATE-TRIBUNAL",
            "LABOUR-APPELLATE-TRIBUNAL-NWFP",
            "LABOUR-APPELLATE-TRIBUNAL-KPK",
            "Labour Appellate Tribunal KPK",
            "Labour Appellate Tribunal NWFP",
        ],
    },
    {
        "name": "Balochistan Labour Appellate Tribunal",
        "short_code": "LAT_BALOCHISTAN",
        "court_level": "tribunal",
        "aliases": ["BALOCHISTAN-LABOUR-APPELLATE-TRIBUNAL", "LABOUR-APPELLATE-TRIBUNAL-BALOCHISTAN"],
    },
    {
        "name": "National Industrial Relations Commission",
        "short_code": "NIRC",
        "court_level": "commission",
        "aliases": ["NIRC", "NATIONAL-INDUSTRIAL-RELATIONS-COMMISSION"],
    },
    {
        "name": GB_CHIEF_NAME,
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
    {
        "name": "Privy Council",
        "short_code": "PRIVY_COUNCIL",
        "court_level": "supreme",
        "aliases": ["PRIVY-COUNCIL", "Judicial Committee of the Privy Council"],
    },
]

_ELECTION_TRIBUNAL_RE = re.compile(r"^election tribunal\b", re.I)
_LABOUR_APPELLATE_RE = re.compile(r"labour appellate tribunal", re.I)
_BOARD_OF_REVENUE_RE = re.compile(r"^board of revenue\b", re.I)
_FOREIGN_LABEL_COUNTRY = re.compile(
    r"(?i)\b(?:india|bangladesh|england|u\.?k\.?|united\s+kingdom|sri\s+lanka|nepal|afghanistan|iran|china|usa|united\s+states|south\s+africa|australia)\b",
)

_RESERVED_SHORT_CODES: Optional[Set[str]] = None


@dataclass(frozen=True)
class LinkResolution:
    target_court_name: str
    reason: str
    would_create_court: bool = False
    manual_review: bool = False
    uncreatable: bool = False


def pls_court_label_key(court_name: Optional[str]) -> str:
    if not court_name or not str(court_name).strip():
        return ""
    raw = str(court_name).strip()
    key = re.sub(r"[\s\-_]+", " ", raw.lower())
    key = re.sub(r"\s+", " ", key).strip()
    return normalize_link_label_key(key)


def normalize_link_label_key(key: str) -> str:
    key = re.sub(r"\bappelant\b", "appellate", key)
    key = re.sub(r"\bappelate\b", "appellate", key)
    key = re.sub(r"\bnorthern\s+areas\b", "gilgit baltistan", key)
    return key


def _title_from_label_key(key: str) -> str:
    return re.sub(r"\s+", " ", key.strip()).title()


def _header_block(full_text: Optional[str], limit: int = 1200) -> str:
    return (full_text or "")[:limit]


def resolve_ajk_court_from_header(full_text: Optional[str]) -> Optional[LinkResolution]:
    """Heading-based AJK SC vs HC split for SUPREME-COURT-AZAD-KASHMIR grid labels."""
    head = _header_block(full_text)
    if not head.strip():
        return LinkResolution(target_court_name="", reason="ajk_header_missing", manual_review=True)
    if _AJK_SHARIAT_RE.search(head):
        return LinkResolution(target_court_name="", reason="ajk_shariat_court", manual_review=True)
    if _PRESENT_CORAM_RE.search(head):
        return LinkResolution(target_court_name=AJK_SC_NAME, reason="ajk_present_coram_sc")
    if _AJK_HC_HEADING_RE.search(head):
        return LinkResolution(target_court_name=AJK_HC_NAME, reason="ajk_header_high_court")
    if _AJK_SC_HEADING_RE.search(head) and not re.search(r"(?i)shariat", head):
        return LinkResolution(target_court_name=AJK_SC_NAME, reason="ajk_header_supreme_court")
    if _AJK_HC_BRACKET.search(head) and _BEFORE_BENCH_RE.search(head):
        return LinkResolution(target_court_name=AJK_HC_NAME, reason="ajk_bracket_azad_jk_before_bench")
    return LinkResolution(target_court_name="", reason="ajk_header_ambiguous", manual_review=True)


def ajk_high_court_from_header(full_text: Optional[str]) -> bool:
    """Backward-compatible: True when header resolves to AJK High Court."""
    res = resolve_ajk_court_from_header(full_text)
    return res is not None and res.target_court_name == AJK_HC_NAME and not res.manual_review


def _seeded_court_name(short_code: str) -> Optional[str]:
    if short_code == "GB_CHIEF":
        return GB_CHIEF_NAME
    from scraper.database import COURT_SEED

    for name, code, *_rest in COURT_SEED:
        if code == short_code:
            return name
    for spec in ADDITIONAL_COURT_SEEDS:
        if spec["short_code"] == short_code:
            return spec["name"]
    return None


def _all_seed_specs() -> Sequence[Dict[str, Any]]:
    return [*ADDITIONAL_COURT_SEEDS, *FOREIGN_COURT_SEEDS]


def _match_seed_key(key: str) -> Optional[Dict[str, Any]]:
    for spec in _all_seed_specs():
        labels = {spec["name"], spec["short_code"], *(spec.get("aliases") or [])}
        norm_labels = {pls_court_label_key(normalize_judgment_court_name(lbl) or lbl) for lbl in labels}
        norm_labels.add(pls_court_label_key(spec["name"]))
        if key in norm_labels:
            return spec
    return None


def _labour_appellate_resolution(key: str) -> Optional[LinkResolution]:
    if not _LABOUR_APPELLATE_RE.search(key):
        return None
    if any(tok in key for tok in ("azad", "ajk", "j k", "jammu", "kashmir")):
        return LinkResolution(target_court_name="", reason="ajk_labour_tribunal", manual_review=True)
    spec = _match_seed_key(key)
    if spec is not None:
        return LinkResolution(target_court_name=spec["name"], reason="labour_appellate_seed")
    return LinkResolution(
        target_court_name=_title_from_label_key(key),
        reason="labour_appellate_uncreatable",
        uncreatable=True,
    )


def reserved_short_codes() -> Set[str]:
    global _RESERVED_SHORT_CODES
    if _RESERVED_SHORT_CODES is None:
        codes: Set[str] = set()
        from scraper.database import COURT_SEED

        for _n, code, *_r in COURT_SEED:
            codes.add(code)
        for spec in _all_seed_specs():
            codes.add(spec["short_code"])
        _RESERVED_SHORT_CODES = codes
    return _RESERVED_SHORT_CODES


def election_tribunal_seed_spec(display_name: str) -> Dict[str, Any]:
    base = re.sub(r"[^A-Z0-9]+", "_", display_name.upper()).strip("_")[:36] or "ELECTION_TRIBUNAL"
    slug = base
    taken = reserved_short_codes()
    n = 2
    while slug in taken:
        suffix = f"_{n}"
        slug = (base[: max(4, 40 - len(suffix))] + suffix).strip("_")
        n += 1
    return {
        "name": display_name,
        "short_code": slug,
        "court_level": "tribunal",
        "aliases": [display_name],
    }


def resolve_null_court_id_link(court_name: str, full_text: Optional[str]) -> Optional[LinkResolution]:
    """Resolve a NULL ``court_id`` row to a canonical ``court.name`` (no DB)."""
    key = pls_court_label_key(court_name)
    if not key:
        return None
    if key in MANUAL_REVIEW_LABEL_KEYS:
        return LinkResolution(target_court_name=court_name, reason="manual_review", manual_review=True)

    if key in _AJK_SC_LABEL_KEYS or PLS_COURT_LABELS.get(key) == AJK_SC_NAME:
        return resolve_ajk_court_from_header(full_text)

    pls_mapped = PLS_COURT_LABELS.get(key)
    if pls_mapped:
        return LinkResolution(target_court_name=pls_mapped, reason="pls_court_labels")

    short = SEEDED_COURT_BY_KEY.get(key)
    if short:
        name = _seeded_court_name(short)
        if name:
            return LinkResolution(target_court_name=name, reason="seeded_court_label")

    spec = _match_seed_key(key)
    if spec is not None:
        return LinkResolution(target_court_name=spec["name"], reason="additional_or_foreign_seed")

    labour = _labour_appellate_resolution(key)
    if labour is not None:
        return labour

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
        return LinkResolution(
            target_court_name=_title_from_label_key(key),
            reason="board_of_revenue_uncreatable",
            uncreatable=True,
        )

    if key in {"nirc", "national industrial relations commission"}:
        return LinkResolution(target_court_name="National Industrial Relations Commission", reason="nirc_seed")

    if "chief court" in key and ("gilgit" in key or "baltistan" in key or key.startswith("gb ")):
        return LinkResolution(target_court_name=GB_CHIEF_NAME, reason="gb_chief_court")

    if key in {"fcc", "federal constitutional court"}:
        return LinkResolution(target_court_name="Federal Constitutional Court", reason="fcc_seed")

    canonical = normalize_judgment_court_name(court_name)
    if canonical and _FOREIGN_LABEL_COUNTRY.search(canonical):
        foreign_spec = _match_seed_key(key)
        if foreign_spec:
            return LinkResolution(target_court_name=foreign_spec["name"], reason="foreign_seed")
        return LinkResolution(target_court_name=canonical, reason="foreign_label", would_create_court=True)

    return None


def all_linkable_seed_specs() -> Sequence[Dict[str, Any]]:
    return list(_all_seed_specs())
