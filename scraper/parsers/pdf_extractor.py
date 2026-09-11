"""
Pakistani Legal Scraper - PDF Extractor
Section 15 - PDF Text Extraction & Download

Production module for extracting text from legal judgment PDFs.
Primary: pdfplumber text extraction
Fallback: pytesseract OCR when text layer is empty / scanned.
Download: streaming download with size limits and validation.

Tech stack: pdfplumber, pytesseract, Pillow, httpx, pdf2image fallback
"""

from __future__ import annotations

import io
import logging
import re
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants & Config
# ---------------------------------------------------------------------------
MIN_EXTRACTED_LENGTH = 200  # chars - threshold to consider pdfplumber success
MAX_PDF_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB hard limit
CHUNK_SIZE = 64 * 1024  # 64KB streaming chunks
DOWNLOAD_TIMEOUT = 120.0
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# Tesseract config - PSM 6 = assume uniform block of text, good for judgments
TESSERACT_CONFIG = "--oem 3 --psm 6 -l eng"
# Some Pakistani judgments contain Urdu citations - try combined if available
TESSERACT_CONFIG_URDU = "--oem 3 --psm 6 -l eng+urd"

PDF_MAGIC = b"%PDF"

# Regex to clean extraction artifacts
RE_MSO_JUNK = re.compile(r"mso-[a-z-]+:[^;]+;?", re.I)
RE_MULTISPACE = re.compile(r"[ \t]{2,}")
RE_MULTINEWLINE = re.compile(r"\n{3,}")
RE_CONTROL = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")


class PDFDownloadError(RuntimeError):
    """Raised when PDF download fails or validation fails."""

class PDFExtractionError(RuntimeError):
    """Raised when both pdfplumber and OCR fail to produce usable text."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _clean_text(text: str) -> str:
    """Normalize legal judgment text."""
    if not text:
        return ""
    # Remove Word/MSO artifacts often present in PakistanLawSite HTML-converted PDFs
    text = RE_MSO_JUNK.sub("", text)
    text = text.replace("\xa0", " ").replace("\u200b", "").replace("\ufeff", "")
    text = RE_CONTROL.sub("", text)
    text = RE_MULTISPACE.sub(" ", text)
    text = RE_MULTINEWLINE.sub("\n\n", text)
    # Normalize line breaks but keep paragraph structure
    lines = [ln.strip() for ln in text.splitlines()]
    # Remove excessive empty lines but preserve at least one separator
    cleaned_lines: list[str] = []
    empty_streak = 0
    for ln in lines:
        if not ln:
            empty_streak += 1
            if empty_streak <= 1:
                cleaned_lines.append("")
            continue
        empty_streak = 0
        cleaned_lines.append(ln)
    return "\n".join(cleaned_lines).strip()


def _pdfplumber_extract(pdf_bytes: bytes) -> tuple[str, int, bool]:
    """
    Attempt pdfplumber extraction.
    Returns (text, num_pages, is_scanned_hint)
    """
    try:
        import pdfplumber
    except ImportError as exc:
        logger.error("pdfplumber not installed: %s", exc)
        return "", 0, False

    try:
        text_parts: list[str] = []
        num_pages = 0
        scanned_pages = 0
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            num_pages = len(pdf.pages)
            if num_pages == 0:
                return "", 0, False
            for i, page in enumerate(pdf.pages):
                try:
                    page_text = page.extract_text(x_tolerance=2, y_tolerance=2) or ""
                    # Also try extract_text_simple for some layouts
                    if len(page_text.strip()) < 50:
                        alt = page.extract_text_simple() if hasattr(page, "extract_text_simple") else ""
                        if alt and len(alt) > len(page_text):
                            page_text = alt
                    if len(page_text.strip()) < 20:
                        scanned_pages += 1
                    text_parts.append(page_text)
                except Exception as e:
                    logger.warning("pdfplumber page %d extraction failed: %s", i, e, exc_info=False)
                    text_parts.append("")
                    scanned_pages += 1
        full_text = "\n".join(text_parts)
        is_scanned = scanned_pages > (num_pages * 0.6) if num_pages else False
        return full_text, num_pages, is_scanned
    except Exception as e:
        logger.warning("pdfplumber open/parse failed: %s", e, exc_info=True)
        return "", 0, True


def _ocr_extract_with_pdfplumber_images(pdf_bytes: bytes) -> str:
    """
    OCR fallback using pdfplumber's page.to_image() -> PIL -> pytesseract
    Works without poppler dependency.
    """
    try:
        import pdfplumber
        from PIL import Image
        import pytesseract
    except ImportError as exc:
        logger.error("OCR dependencies missing: %s", exc)
        return ""

    ocr_texts: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for idx, page in enumerate(pdf.pages):
                try:
                    # Render page to image at 300 DPI for decent OCR
                    # pdfplumber's to_image requires Wand or pdfium optional; handle fallback
                    try:
                        pil_image: Image.Image = page.to_image(resolution=300).original
                    except Exception:
                        # Fallback: try to extract embedded images and OCR them (less accurate)
                        # If no image rendering, skip
                        logger.debug("page.to_image failed on page %d, trying crop render", idx)
                        # Some pdfplumber versions need explicit
                        im = page.to_image(resolution=250)
                        pil_image = im.original if hasattr(im, "original") else None
                        if pil_image is None:
                            continue

                    if pil_image is None:
                        continue

                    # Convert to grayscale for better OCR on legal scans
                    if pil_image.mode in ("RGBA", "LA", "P"):
                        background = Image.new("RGB", pil_image.size, (255, 255, 255))
                        if pil_image.mode == "P":
                            pil_image = pil_image.convert("RGBA")
                        if pil_image.mode == "RGBA":
                            background.paste(pil_image, mask=pil_image.split()[-1])
                            pil_image = background
                        else:
                            pil_image = pil_image.convert("RGB")
                    elif pil_image.mode != "L":
                        pil_image = pil_image.convert("L")

                    # Try combined eng+urd first, fallback to eng if urdu traineddata missing
                    try:
                        txt = pytesseract.image_to_string(pil_image, config=TESSERACT_CONFIG_URDU)
                        if len(txt.strip()) < 50:
                            txt = pytesseract.image_to_string(pil_image, config=TESSERACT_CONFIG)
                    except Exception as e:
                        # Tesseract might not have urd, fallback
                        logger.debug("tesseract urd fallback triggered: %s", e)
                        txt = pytesseract.image_to_string(pil_image, config=TESSERACT_CONFIG)

                    if txt:
                        ocr_texts.append(txt)

                except Exception as e:
                    logger.warning("OCR failed on page %d: %s", idx, e, exc_info=False)
                    continue
    except Exception as e:
        logger.warning("OCR pdf open failed: %s", e, exc_info=True)
        return ""

    return "\n".join(ocr_texts)


def _validate_pdf_bytes(data: bytes, url: str = "") -> None:
    """Lightweight validation that bytes look like a PDF."""
    if not data:
        raise PDFDownloadError(f"Empty PDF download from {url}")
    if len(data) < 1024:
        # Could be error page that got returned as PDF mime
        # Allow but log
        logger.warning("Very small PDF %d bytes from %s, may be error page", len(data), url)
    if not data[:4].startswith(PDF_MAGIC):
        # Some servers prepend BOM or whitespace; check first 1024 bytes for %PDF
        if PDF_MAGIC not in data[:1024]:
            # But don't hard-fail - site sometimes sends HTML error page with PDF url
            # Examine if it looks like HTML
            if data[:5].lower().startswith(b"<!doc") or data[:6].lower().startswith(b"<html"):
                preview = data[:500].decode("utf-8", errors="ignore")
                raise PDFDownloadError(
                    f"URL did not return PDF, got HTML from {url}: {preview[:200]}"
                )
            logger.warning("Download from %s missing PDF magic, continuing anyway", url)


# ---------------------------------------------------------------------------
# Public API per spec Section 15
# ---------------------------------------------------------------------------
def extract_pdf_text(pdf_bytes: bytes) -> str:
    """
    Extract text from Pakistan legal judgment PDF bytes.
    
    Strategy:
      1. Primary: pdfplumber extract_text() for digital PDFs.
      2. If extracted length < 200 chars or >60% pages look scanned,
         fallback to pytesseract OCR via pdfplumber page.to_image().
      3. Return cleaned, normalized text. Empty string if truly unreadable.

    Args:
        pdf_bytes: Raw PDF file content.

    Returns:
        Extracted and cleaned text. Never raises - returns "" on failure
        to allow scraper to continue. Logs warnings for observability.
    """
    if not pdf_bytes:
        logger.warning("extract_pdf_text called with empty bytes")
        return ""

    _validate_pdf_bytes(pdf_bytes)

    start = time.monotonic()

    # --- Primary: pdfplumber ---
    raw_text, num_pages, is_scanned_hint = _pdfplumber_extract(pdf_bytes)
    cleaned_primary = _clean_text(raw_text)

    if len(cleaned_primary.strip()) >= MIN_EXTRACTED_LENGTH and not is_scanned_hint:
        logger.info(
            "pdfplumber success: %d pages, %d chars in %.2fs",
            num_pages,
            len(cleaned_primary),
            time.monotonic() - start,
        )
        return cleaned_primary

    # --- Fallback: OCR ---
    logger.info(
        "pdfplumber text too short (%d chars, scanned_hint=%s, pages=%d), falling back to OCR",
        len(cleaned_primary.strip()),
        is_scanned_hint,
        num_pages,
    )

    try:
        ocr_raw = _ocr_extract_with_pdfplumber_images(pdf_bytes)
        ocr_cleaned = _clean_text(ocr_raw)

        # Choose best of primary vs OCR
        if len(ocr_cleaned.strip()) > len(cleaned_primary.strip()):
            logger.info(
                "OCR fallback success: %d chars (primary was %d) in %.2fs",
                len(ocr_cleaned),
                len(cleaned_primary),
                time.monotonic() - start,
            )
            if len(ocr_cleaned.strip()) >= 50:
                return ocr_cleaned

        # If OCR produced little but primary had something, return primary
        if len(cleaned_primary.strip()) >= 50:
            logger.warning(
                "OCR output (%d) smaller than primary (%d), using primary",
                len(ocr_cleaned.strip()),
                len(cleaned_primary.strip()),
            )
            return cleaned_primary

        # Last resort - return whatever we have
        final = ocr_cleaned if len(ocr_cleaned) > len(cleaned_primary) else cleaned_primary
        if final.strip():
            return final

        logger.error("Both pdfplumber (%d chars) and OCR (%d chars) yielded insufficient text", len(cleaned_primary), len(ocr_cleaned))
        return ""

    except Exception as e:
        logger.error("extract_pdf_text OCR fallback crashed: %s", e, exc_info=True)
        # Return primary if exists, else empty - graceful degradation for scraper
        return cleaned_primary if cleaned_primary.strip() else ""


async def download_pdf(
    url: str,
    client: httpx.AsyncClient,
    max_size_bytes: int = MAX_PDF_SIZE_BYTES,
    timeout: float = DOWNLOAD_TIMEOUT,
) -> bytes:
    """
    Download a PDF with streaming to handle large Pakistani judgment files.

    Features:
      - Streaming via client.stream() + aiter_bytes() to avoid loading huge files in RAM at once
      - Hard size limit (default 100MB) to protect scraper
      - Content-Type validation with warning (some sites mis-label)
      - PDF magic validation (%PDF)
      - Raises PDFDownloadError on failure with actionable message
      - Graceful logging for scraper observability

    Args:
        url: PDF URL (could be PakistanLawSite GetCaseFile indirect or gov.pk statute)
        client: httpx.AsyncClient with existing cookies/session (dual-credential managed externally)
        max_size_bytes: Reject if larger than this
        timeout: Per-request timeout in seconds

    Returns:
        bytes: Raw PDF bytes

    Raises:
        PDFDownloadError: On HTTP error, size exceed, or content validation failure
        ValueError: On invalid URL
    """
    if not url or not isinstance(url, str):
        raise ValueError(f"Invalid url: {url!r}")
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"URL must be http(s): {url}")

    if client is None:
        raise ValueError("httpx.AsyncClient client is required for download_pdf")

    logger.info("Downloading PDF: %s", url)

    headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        # Use stream=True pattern for large PDFs - prevents buffering entire file in memory at HTTP layer
        async with client.stream(
            "GET",
            url,
            follow_redirects=True,
            timeout=httpx.Timeout(timeout, connect=30.0),
            headers=headers,
        ) as response:

            # Status check - will raise for 4xx/5xx but we want custom message
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                # Detailed logging for blocked/session expiry cases common on PakistanLawSite
                body_preview = ""
                try:
                    # try read small preview without consuming whole stream
                    body_preview = (await response.aread())[:500].decode("utf-8", errors="ignore")
                except Exception:
                    pass
                if e.response.status_code == 403 or e.response.status_code == 429:
                    logger.warning("Access blocked (%d) for %s: %s", e.response.status_code, url, body_preview[:200])
                raise PDFDownloadError(
                    f"HTTP {e.response.status_code} downloading PDF from {url}: {body_preview[:200]}"
                ) from e

            # Early content-length guard
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    cl = int(content_length)
                    if cl > max_size_bytes:
                        raise PDFDownloadError(
                            f"PDF too large: Content-Length {cl} > limit {max_size_bytes} for {url}"
                        )
                except ValueError:
                    pass  # ignore malformed header

            # Content-Type advisory (warn, don't fail - gov.pk sometimes uses octet-stream)
            ctype = response.headers.get("content-type", "").lower()
            if ctype and "pdf" not in ctype and "octet-stream" not in ctype and "binary" not in ctype:
                # If it's HTML, likely login page / session expired
                if "html" in ctype or "text" in ctype:
                    # Peek first bytes to confirm
                    logger.warning("Expected PDF but got content-type %s from %s - may be session expiry", ctype, url)
                else:
                    logger.debug("Unusual content-type %s for PDF %s", ctype, url)

            # Streamed read with size enforcement
            buffer = io.BytesIO()
            total_bytes = 0
            start_dl = time.monotonic()

            async for chunk in response.aiter_bytes(chunk_size=CHUNK_SIZE):
                if not chunk:
                    continue
                total_bytes += len(chunk)
                if total_bytes > max_size_bytes:
                    raise PDFDownloadError(
                        f"PDF exceeded max size {max_size_bytes} bytes while streaming from {url} (got {total_bytes})"
                    )
                buffer.write(chunk)

            data = buffer.getvalue()
            elapsed = time.monotonic() - start_dl

            logger.info(
                "Downloaded %d bytes in %.2fs (%.1f KB/s) from %s",
                total_bytes,
                elapsed,
                (total_bytes / 1024 / elapsed) if elapsed > 0 else 0,
                url,
            )

            # Validation
            _validate_pdf_bytes(data, url=url)

            # Guard against login page disguised as PDF (PakistanLawSite returns mainLoginForm)
            if b"mainLoginForm" in data[:5000] or b"Login.UserName" in data[:5000]:
                raise PDFDownloadError(
                    f"Session expired - received login page instead of PDF from {url}"
                )

            if len(data) < 15000:
                # For Blackletter policy, PDFs <15KB are likely stubs / forbidden content
                logger.warning("PDF from %s is very small (%d bytes), possible stub", url, len(data))
                # Still return - caller decides if to discard, but warn

            return data

    except PDFDownloadError:
        # Already wrapped with context
        raise
    except httpx.TimeoutException as e:
        msg = f"Timeout ({timeout}s) downloading PDF from {url}: {e}"
        logger.error(msg)
        raise PDFDownloadError(msg) from e
    except httpx.RequestError as e:
        # Network-level failure - per dual-credential spec, this is real failure triggering failover in caller
        msg = f"Network error downloading PDF from {url}: {e.__class__.__name__}: {e}"
        logger.error(msg)
        raise PDFDownloadError(msg) from e
    except Exception as e:
        msg = f"Unexpected error downloading PDF from {url}: {e}"
        logger.error(msg, exc_info=True)
        raise PDFDownloadError(msg) from e


# ---------------------------------------------------------------------------
# Optional sync wrapper for callers outside asyncio (e.g., Celery tasks)
# ---------------------------------------------------------------------------
def download_pdf_sync(
    url: str,
    client: Optional[httpx.Client] = None,
    max_size_bytes: int = MAX_PDF_SIZE_BYTES,
    timeout: float = DOWNLOAD_TIMEOUT,
) -> bytes:
    """
    Synchronous version for Celery workers or non-async contexts.
    Maintains same streaming + size-limit behavior.

    If client not supplied, creates temporary client and closes it.
    """
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"Invalid url: {url}")

    need_close = False
    if client is None:
        client = httpx.Client(
            headers={"User-Agent": DEFAULT_UA},
            follow_redirects=True,
            timeout=httpx.Timeout(timeout, connect=30.0),
        )
        need_close = True

    try:
        with client.stream("GET", url, headers={"User-Agent": DEFAULT_UA}) as resp:
            resp.raise_for_status()
            cl = resp.headers.get("content-length")
            if cl and int(cl) > max_size_bytes:
                raise PDFDownloadError(f"Content-Length {cl} > limit {max_size_bytes}")
            total = 0
            buf = io.BytesIO()
            for chunk in resp.iter_bytes(chunk_size=CHUNK_SIZE):
                total += len(chunk)
                if total > max_size_bytes:
                    raise PDFDownloadError(f"PDF exceeded max size {max_size_bytes}")
                buf.write(chunk)
            data = buf.getvalue()
            _validate_pdf_bytes(data, url=url)
            return data
    except httpx.HTTPStatusError as e:
        raise PDFDownloadError(f"HTTP {e.response.status_code} for {url}") from e
    finally:
        if need_close:
            client.close()
