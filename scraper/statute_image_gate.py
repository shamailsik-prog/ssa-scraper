"""Reject image bytes before they can be saved as statutes.

Sindh Laws gazette tables link scanned acts. A WhatsApp JPEG under
``/GalleryImages/`` is not a PDF or HTML statute. ``_classify_pas_discovered_url``
does not treat that path as a document, but a frontier row whose URL, content
type, or body is an image is still handed to ``ingest_statute`` and decoded as
latin-1 text. That produced statute ``6aed1b80`` (JFIF name, binary sections).
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import unquote, urlsplit

_IMAGE_EXT_RE = re.compile(r"(?i)\.(jpe?g|png|gif|webp|bmp)$")
_JPEG_SOI = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG"
_PDF_MAGIC = b"%PDF"

# Printable ASCII, whitespace, Urdu/Arabic, and a few PDF punctuation marks.
# Latin-1 supplement is intentionally excluded: JPEG bytes decoded as latin-1
# look like letters (À–ÿ) and would otherwise pass a plain isalpha() check.
_EXTRA_PROSE = set("\u00a0\u2013\u2014\u2018\u2019\u201c\u201d\u2022\u00b7")


def statute_url_is_image(url: str) -> bool:
    path = unquote((urlsplit(url or "").path or ""))
    return _IMAGE_EXT_RE.search(path) is not None


def _content_type_is_image(content_type: str) -> bool:
    media = (content_type or "").split(";", 1)[0].strip().lower()
    return media.startswith("image/")


def _body_is_image(content: bytes) -> bool:
    """True for JPEG/PNG magic, or JFIF / ICC_PROFILE at the start of a non-PDF body."""
    if not content:
        return False
    head = content[:1024].lstrip()
    if head.startswith(_PDF_MAGIC):
        return False
    if head.startswith(_JPEG_SOI) or head.startswith(_PNG_MAGIC):
        return True
    window = content[:64]
    if window.lstrip().startswith((b"<", b"{", _PDF_MAGIC)):
        return False
    return b"JFIF" in window or b"ICC_PROFILE" in window


def statute_source_is_image(
    *,
    url: str = "",
    final_url: str = "",
    content_type: str = "",
    content: bytes = b"",
) -> bool:
    if _content_type_is_image(content_type):
        return True
    if statute_url_is_image(url) or statute_url_is_image(final_url):
        return True
    return _body_is_image(content or b"")


def image_statute_reject_reason(
    *,
    kind: str,
    url: str = "",
    final_url: str = "",
    content_type: str = "",
    content: bytes = b"",
) -> Optional[str]:
    if kind not in ("statute", "instrument"):
        return None
    if not statute_source_is_image(
        url=url,
        final_url=final_url,
        content_type=content_type,
        content=content or b"",
    ):
        return None
    return f"image statute source rejected for {kind} document URL"


def _allowed_statute_char(ch: str) -> bool:
    code = ord(ch)
    if ch in "\t\r\n" or 0x20 <= code <= 0x7E:
        return True
    if (
        0x0600 <= code <= 0x06FF
        or 0x0750 <= code <= 0x077F
        or 0xFB50 <= code <= 0xFDFF
        or 0xFE70 <= code <= 0xFEFF
    ):
        return True
    return ch in _EXTRA_PROSE


def is_binary_garbage_text(text: str) -> bool:
    """True when a statute name or section body is mostly non-text (JPEG/JFIF leftovers).

    Empty text is a different gate. A prose sentence that merely mentions JFIF stays.
    """
    raw = text or ""
    if not raw.strip():
        return False
    total = len(raw)
    allowed = sum(1 for ch in raw if _allowed_statute_char(ch))
    ratio = allowed / total
    if "\xff\xd8\xff" in raw[:32] or raw.startswith("\x89PNG"):
        return True
    if total >= 8 and ratio < 0.50:
        return True
    if ratio < 0.85 and any(sig in raw for sig in ("JFIF", "ICC_PROFILE", "\x00")):
        return True
    if total >= 32:
        nonprint = sum(1 for ch in raw if not ch.isprintable())
        if nonprint / total > 0.20:
            return True
    return False
