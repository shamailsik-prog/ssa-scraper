"""Map assembly / provincial sources to statute jurisdiction labels.

The live extractor used to leave SindhAssembly jurisdiction empty. Promotion then
stored the column default, Federal. ``SOURCE_JURISDICTION`` already maps
SindhAssembly to Sindh, and the quality CLI applies that map, but the promote
path only consulted it for KPAssembly. A confident extract (three or more
sections, confidence 0.9, at or above the AI threshold) skips the model, so
nothing else fills the province. That is how the two 2026 pas.gov.pk acts
landed as Federal while older rows stayed Sindh.

``resolve_statute_jurisdiction`` is the derivation used by the extractor and by
promotion. A missing or Federal label on a provincial source is the column
default, not the province. A pas.gov.pk host is Sindh. A provincial
``Act No.`` marker in the act's own heading or act-number field is not replaced
with Federal.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlsplit

SOURCE_JURISDICTION: dict[str, str] = {
    "PunjabAssembly": "Punjab",
    "SindhAssembly": "Sindh",
    "KPAssembly": "Khyber Pakhtunkhwa",
    "BalochistanAssembly": "Balochistan",
    "AJKAssembly": "Azad Jammu and Kashmir",
    "GBAssembly": "Gilgit-Baltistan",
    "NationalAssembly": "Federal",
    "Senate": "Federal",
    "GazetteOfPakistan": "Federal",
    "PakistanCode": "Federal",
}

FEDERAL_JURISDICTION = "Federal"

# Official Provincial Assembly of Sindh hosts. www and the apex host are the
# same site; a lookalike suffix is not.
PAS_SOURCE_HOSTS = frozenset({"pas.gov.pk", "www.pas.gov.pk"})

# The act's own number is printed in the heading, before the operative sections.
# A citation of another province's act further down the text is not this marker.
ACT_NO_MARKER_HEAD_CHARS = 4000

_PROVINCIAL_ACT_NO_MARKERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bSindh\s+Act\s+No\."), "Sindh"),
    (re.compile(r"(?i)\bPunjab\s+Act\s+No\."), "Punjab"),
    (re.compile(r"(?i)\bKhyber\s+Pakhtunkhwa\s+Act\s+No\."), "Khyber Pakhtunkhwa"),
    (re.compile(r"(?i)\bBalochistan\s+Act\s+No\."), "Balochistan"),
)

_FEDERAL_SOURCE_NAMES = frozenset(
    name for name, label in SOURCE_JURISDICTION.items() if label == FEDERAL_JURISDICTION
)


def jurisdiction_for_source(source_name: Optional[str]) -> Optional[str]:
    return SOURCE_JURISDICTION.get(source_name or "")


def should_fix_federal_jurisdiction(current: str, source_name: Optional[str]) -> Optional[str]:
    """Return the corrected jurisdiction when a row is Federal but the source is provincial/AJK."""
    if not _is_federal_default(current):
        return None
    mapped = jurisdiction_for_source(source_name)
    if not mapped or mapped == FEDERAL_JURISDICTION:
        return None
    return mapped


def statute_source_host(url: Optional[str]) -> str:
    """Hostname of a statute source URL, ignoring a ``|label`` identity suffix."""
    raw = (url or "").strip()
    if not raw:
        return ""
    fetch = raw.split("|", 1)[0].strip()
    if fetch.startswith("//"):
        fetch = "https:" + fetch
    elif "://" not in fetch:
        fetch = "https://" + fetch
    try:
        host = urlsplit(fetch).hostname or ""
    except ValueError:
        return ""
    return host.lower().rstrip(".")


def is_pas_source_url(url: Optional[str]) -> bool:
    return statute_source_host(url) in PAS_SOURCE_HOSTS


def _is_federal_default(value: Optional[str]) -> bool:
    return (value or "").strip().lower() == FEDERAL_JURISDICTION.lower()


def jurisdiction_from_act_marker(*texts: Optional[str]) -> Optional[str]:
    """Province named by the act's own ``Act No.`` marker, if one is present.

    ``act_no`` should be passed first: it is the listing's act number, not a
    citation. Body text is read only at the heading. The earliest marker in a
    single text wins when a heading names one province.
    """
    for text in texts:
        if not text:
            continue
        window = text[:ACT_NO_MARKER_HEAD_CHARS]
        found: Optional[str] = None
        found_at: Optional[int] = None
        for pattern, label in _PROVINCIAL_ACT_NO_MARKERS:
            match = pattern.search(window)
            if match is None:
                continue
            if found_at is None or match.start() < found_at:
                found = label
                found_at = match.start()
        if found:
            return found
    return None


def resolve_statute_jurisdiction(
    *,
    source_name: Optional[str] = None,
    source_url: Optional[str] = None,
    detail_url: Optional[str] = None,
    explicit: Optional[str] = None,
    text: Optional[str] = None,
    act_no: Optional[str] = None,
) -> Optional[str]:
    """Jurisdiction to store for one statute extract.

    pas.gov.pk and www.pas.gov.pk are Sindh. A missing or Federal label on a
    provincial assembly source uses the source map (the KPAssembly rule, for
    every province in ``SOURCE_JURISDICTION``). An explicit provincial act-number
    marker is kept when the alternative would be Federal.
    """
    if is_pas_source_url(source_url) or is_pas_source_url(detail_url):
        return "Sindh"
    explicit_clean = (str(explicit).strip() if explicit is not None else "")
    mapped = jurisdiction_for_source(source_name)
    federal_label = explicit_clean == "" or _is_federal_default(explicit_clean)
    if mapped and mapped != FEDERAL_JURISDICTION and federal_label:
        return mapped
    if federal_label:
        marker = jurisdiction_from_act_marker(
            act_no if isinstance(act_no, str) else None,
            text if isinstance(text, str) else None,
        )
        if marker:
            return marker
    if explicit_clean:
        return explicit_clean
    if mapped:
        return mapped
    if (source_name or "") in _FEDERAL_SOURCE_NAMES:
        return FEDERAL_JURISDICTION
    return None
