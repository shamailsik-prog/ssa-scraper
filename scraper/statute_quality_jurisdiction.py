"""Map assembly / provincial sources to statute jurisdiction labels."""

from __future__ import annotations

from typing import Optional

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


def jurisdiction_for_source(source_name: Optional[str]) -> Optional[str]:
    return SOURCE_JURISDICTION.get(source_name or "")


def should_fix_federal_jurisdiction(current: str, source_name: Optional[str]) -> Optional[str]:
    """Return the corrected jurisdiction when a row is Federal but the source is provincial/AJK."""
    if (current or "").strip() != "Federal":
        return None
    mapped = jurisdiction_for_source(source_name)
    if not mapped or mapped == "Federal":
        return None
    return mapped
