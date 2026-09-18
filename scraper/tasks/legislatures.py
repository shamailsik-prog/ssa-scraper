"""
Legislatures and the Gazette — PUBLIC statute/instrument sources (Annex B-2): National Assembly,
Senate, the four provincial assemblies and the Gazette of Pakistan.

Listing pages yield acts, ordinances, bills and notifications (PDF or HTML). Acts are ingested
as statutes (versioned sections); amendment acts, ordinances, notifications and gazette notices
as instruments that later bind to the statute sections they amend.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, quote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.fetchers import has_pdf_signature
from scraper.models import CrawlFrontier, ScraperSource
from scraper.security import ExplicitBlock, RobotsUnavailable, URLPolicyError, check_url_policy
from scraper.tasks.public_pipeline import PublicPipeline, _tests_allow_private, _url_looks_like_pdf, run_public_source

logger = logging.getLogger(__name__)

DEFAULT_LISTINGS: Dict[str, List[Dict[str, Any]]] = {
    "NationalAssembly": [
        {"url": "https://na.gov.pk/en/acts-tenure.php", "target_kind": "statute"},
        {"url": "https://na.gov.pk/en/bills.php?type=1", "target_kind": "instrument"},
        {"url": "https://na.gov.pk/en/bills.php?type=2", "target_kind": "instrument"},
        {"url": "https://na.gov.pk/en/bills.php?status=pass", "target_kind": "instrument"},
        {"url": "https://na.gov.pk/en/bills.php?status=majlis", "target_kind": "instrument"},
        {"url": "https://na.gov.pk/en/bills.php?status=ref-1", "target_kind": "instrument"},
        {"url": "https://na.gov.pk/en/bills.php?status=ref-2", "target_kind": "instrument"},
        {"url": "https://na.gov.pk/en/bills.php?type=4", "target_kind": "instrument"},
        {"url": "https://na.gov.pk/en/bills-15.php?status=pass", "target_kind": "instrument"},
        {"url": "https://na.gov.pk/en/bills-15.php?status=majlis", "target_kind": "instrument"},
        {"url": "https://na.gov.pk/en/bills-15.php?type=4", "target_kind": "instrument"},
        {"url": "https://na.gov.pk/en/bills-passed.php", "target_kind": "instrument"},
        {"url": "https://na.gov.pk/en/bills-passed.php?type=1", "target_kind": "instrument"},
        {"url": "https://na.gov.pk/en/bills-passed.php?type=2", "target_kind": "instrument"},
    ],
    "Senate": [
        {"url": "https://senate.gov.pk/en/acts.php?id=-1&catid=186&subcatid=285&cattitle=Acts", "target_kind": "statute"},
        {"url": "https://senate.gov.pk/en/ordinance.php?id=-1&catid=186&subcatid=304&cattitle=Ordinances", "target_kind": "instrument"},
        {"url": "https://senate.gov.pk/en/bills.php?id=-1&catid=186&subcatid=276&leftcatid=278&cattitle=Bills", "target_kind": "instrument"},
        {"url": "https://senate.gov.pk/en/bills.php?id=-1&catid=186&subcatid=276&leftcatid=279&cattitle=Bills", "target_kind": "instrument"},
        {"url": "https://senate.gov.pk/en/bills.php?id=-1&catid=186&subcatid=276&leftcatid=368&cattitle=Bills", "target_kind": "instrument"},
        {"url": "https://senate.gov.pk/en/pbs.php?catid=186&subcatid=276&leftcatid=278&cattitle=Bills", "target_kind": "instrument"},
        {"url": "https://senate.gov.pk/en/pbna.php?catid=186&subcatid=276&leftcatid=278&cattitle=Bills", "target_kind": "instrument"},
        {"url": "https://senate.gov.pk/en/gbs.php?catid=186&subcatid=276&leftcatid=279&cattitle=Bills", "target_kind": "instrument"},
        {"url": "https://senate.gov.pk/en/gbna.php?catid=186&subcatid=276&leftcatid=279&cattitle=Bills", "target_kind": "instrument"},
        {"url": "https://senate.gov.pk/en/bs.php?catid=186&subcatid=276&leftcatid=368&cattitle=Bills", "target_kind": "instrument"},
    ],
    "PunjabAssembly": [{"url": "https://www.pap.gov.pk/acts", "target_kind": "statute"}, {"url": "https://punjablaws.gov.pk/index.html", "target_kind": "statute"}],
    "SindhAssembly": [
        {"url": "https://www.pas.gov.pk/index.php/acts", "target_kind": "statute"},
        {"url": "https://sindhlaws.gov.pk/", "target_kind": "statute"},
        {"url": "https://sindhlaws.gov.pk/Gazette.aspx?pg=ACT", "target_kind": "statute"},
        {"url": "https://sindhlaws.gov.pk/Gazette.aspx?pg=ORDINANCE", "target_kind": "instrument"},
        {"url": "https://sindhlaws.gov.pk/Gazette.aspx?pg=BILLS", "target_kind": "instrument"},
    ],
    "KPAssembly": [{"url": "https://www.pakp.gov.pk/act/", "target_kind": "statute"}, {"url": "https://kpcode.kp.gov.pk/", "target_kind": "statute"}],
    "BalochistanAssembly": [
        {"url": "https://www.pabalochistan.gov.pk/acts", "target_kind": "statute"},
        {"url": "https://balochistancode.gob.pk/laws_rules.aspx?opento=1&wise=srbdl", "target_kind": "statute"},
    ],
    "AJKAssembly": [
        {"url": "https://law.gok.pk/revised-volume/", "target_kind": "statute"},
        {"url": "https://law.gok.pk/acts/", "target_kind": "statute"},
        {"url": "https://law.gok.pk/ordinance/", "target_kind": "instrument"},
    ],
    "GazetteOfPakistan": [
        {"url": "http://pcp.gov.pk/Download", "target_kind": "instrument"},
        {"url": "http://pcp.gov.pk/WeeklyNitifications", "target_kind": "instrument"},
        {"url": "http://pcp.gov.pk/WeeklyNotifications", "target_kind": "instrument"},
        {"url": "http://pcp.gov.pk/gazette", "target_kind": "instrument"},
        {"url": "http://pcp.gov.pk/gazette/", "target_kind": "instrument"},
    ],
}

LEGISLATURE_SOURCES = tuple(DEFAULT_LISTINGS.keys())

PAB_HOST = "pabalochistan.gov.pk"
PAB_HOST_ALIASES = (PAB_HOST, "www.pabalochistan.gov.pk")
BALOCHISTAN_CODE_HOST = "balochistancode.gob.pk"
BALOCHISTAN_CODE_HOST_ALIASES = (BALOCHISTAN_CODE_HOST, f"www.{BALOCHISTAN_CODE_HOST}")
STORAGE_DOC_RE = re.compile(r"(?i)^/storage/\d+/.+\.(pdf|doc|docx)$")
PAB_LISTING_PATH_RE = re.compile(r"(?i)^/(acts/?|public/acts/?|index\.php/acts/?|public/index\.php/acts/?)$")
BALOCHISTAN_CODE_DOC_RE = re.compile(r"(?i)^/.+\.(pdf|doc|docx)$")
BALOCHISTAN_CODE_LISTING_PATH_RE = re.compile(r"(?i)^/(|home\.aspx|laws_rules\.aspx)$")
BALOCHISTAN_CODE_DETAIL_PATH_RE = re.compile(r"(?i)^/document\.aspx$")
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
PAP_HOST = "pap.gov.pk"
PAP_HOST_ALIASES = (PAP_HOST, "www.pap.gov.pk")
PAP_ACT_DOC_RE = re.compile(r"(?i)^/uploads/acts/.+\.(pdf|html?)$")
PAP_LISTING_PATH_RE = re.compile(r"(?i)^/(acts/?|en/about-assembly/parliamentary-privileges/?)$")
PUNJABLAWS_HOST = "punjablaws.gov.pk"
PUNJABLAWS_HOST_ALIASES = (PUNJABLAWS_HOST, f"www.{PUNJABLAWS_HOST}")
PUNJABLAWS_DOC_RE = re.compile(r"(?i)^/.+\.(pdf|doc|docx)$")
PUNJABLAWS_DETAIL_PATH_RE = re.compile(r"(?i)^/(acts?|laws?|ordinances?|rules?|notifications?|codes?)/[^/?#]+(?:\.html?)?$")
PUNJABLAWS_LISTING_PATH_RE = re.compile(r"(?i)^/(|index(?:\.html?)?|search(?:\.html?)?|all[-_]?laws?(?:\.html?)?)$")
PUNJABLAWS_LEGAL_HINT_RE = re.compile(r"(?i)\b(act|ordinance|rule|rules|law|code|regulation|notification|bill|amendment)\b")
PUNJABLAWS_NAV_HINT_RE = re.compile(r"(?i)\b(index|list|search|category|archive|home|contents?)\b")
PAS_HOST = "pas.gov.pk"
PAS_HOST_ALIASES = (PAS_HOST, "www.pas.gov.pk")
PAS_ACT_DOC_RE = re.compile(r"(?i)^/uploads/acts/.+\.(pdf|doc|docx|html?)$")
PAS_LISTING_PATH_RE = re.compile(r"(?i)^/index\.php/acts/?$")
PAS_DETAIL_PATH_RE = re.compile(r"(?i)^/index\.php/acts/details/\d+/\d+/?$")
SINDHLAWS_HOST = "sindhlaws.gov.pk"
SINDHLAWS_HOST_ALIASES = (SINDHLAWS_HOST, f"www.{SINDHLAWS_HOST}")
SINDHLAWS_DOC_RE = re.compile(r"(?i)^/setup/(publications|library)/.+\.(pdf|doc|docx|html?)$")
SINDHLAWS_LISTING_PATH_RE = re.compile(r"(?i)^/(|index\.aspx|gazette\.aspx|library\.aspx)$")
SINDHLAWS_DETAIL_PATH_RE = re.compile(r"(?i)^/gazettedetail\.aspx$")
PAKP_HOST = "pakp.gov.pk"
PAKP_HOST_ALIASES = (PAKP_HOST, "www.pakp.gov.pk")
PAKP_ACT_DOC_RE = re.compile(r"(?i)^/wp-content/uploads/.+\.(pdf|doc|docx|html?)$")
PAKP_LISTING_PATH_RE = re.compile(r"(?i)^/(act|acts)/?$")
PAKP_DETAIL_PATH_RE = re.compile(r"(?i)^/act/[^/?#]+/?$")
KPCODE_HOST = "kpcode.kp.gov.pk"
KPCODE_HOST_ALIASES = (KPCODE_HOST, "www.kpcode.kp.gov.pk")
KPCODE_DOC_RE = re.compile(r"(?i)^/uploads/.+\.(pdf|doc|docx|html?)$")
KPCODE_LAW_DETAIL_RE = re.compile(r"(?i)^/homepage/lawdetails/\d+/?$")
KPCODE_RULE_DETAIL_RE = re.compile(r"(?i)^/homepage/ruledetails/\d+/?$")
NA_HOST = "na.gov.pk"
NA_HOST_ALIASES = (NA_HOST, "www.na.gov.pk")
NA_DOC_RE = re.compile(r"(?i)^/uploads/documents/.+\.(pdf|doc|docx|html?)$")
NA_LISTING_PATH_RE = re.compile(r"(?i)^/en/(acts-tenure|acts|bills|bills-15|bills-passed)\.php$")
NA_DETAIL_PATH_RE = re.compile(r"(?i)^/en/(bill|bill-detail|bill-details|act-detail|act-details|detail|details)\.php$")
SENATE_HOST = "senate.gov.pk"
SENATE_HOST_ALIASES = (SENATE_HOST, "www.senate.gov.pk")
SENATE_DOC_RE = re.compile(r"(?i)^/uploads/documents/.+\.(pdf|doc|docx|html?)$")
SENATE_LISTING_PATH_RE = re.compile(r"(?i)^/en/(acts|ordinance|bills|pbs|pbna|gbs|gbna|bs)\.php$")
SENATE_DETAIL_PATH_RE = re.compile(r"(?i)^/en/essence\.php$")
PCP_HOST = "pcp.gov.pk"
PCP_HOST_ALIASES = (PCP_HOST, "www.pcp.gov.pk")
PCP_DOC_RE = re.compile(r"(?i)^/siteimage/downloads/.+\.(pdf|doc|docx|html?)$")
PCP_LISTING_PATH_RE = re.compile(r"(?i)^/(download|weeklynitifications|weeklynotifications|gazette)/?$")
PCP_DETAIL_PATH_RE = re.compile(r"(?i)^/detail/[^/?#]+/?$")
AJK_LAW_HOST = "law.gok.pk"
AJK_LAW_HOST_ALIASES = (AJK_LAW_HOST, f"www.{AJK_LAW_HOST}")
AJK_LAW_DOC_RE = re.compile(r"(?i)^/wp-content/uploads/.+\.(pdf|doc|docx)$")
AJK_LAW_LISTING_PATH_RE = re.compile(r"(?i)^/(|acts|ordinance|revised-volume|download)(?:/page/\d+)?/?$")
AJK_LAW_DETAIL_PATH_RE = re.compile(r"(?i)^/(acts|ordinance|revised-volume|download)/[^/?#]+/?$")
AJK_LAW_LEGAL_HINT_RE = re.compile(r"(?i)\b(act|acts|ordinance|rule|rules|regulation|law|code|notification|amendment|bill|statute)\b")


def listings_for(source: ScraperSource) -> List[Dict[str, Any]]:
    cfg = source.config_json or {}
    if cfg.get("listings"):
        return [{"url": u, "target_kind": cfg.get("target_kind", "statute")} for u in cfg["listings"]]
    return DEFAULT_LISTINGS.get(source.source_name, [{"url": source.source_url, "target_kind": "statute"}])


def normalize_pab_public_url(raw: str, *, base_url: str) -> Optional[str]:
    """Normalize discovered candidates onto official public Balochistan hosts."""
    if not raw:
        return None
    candidate = html.unescape(str(raw)).replace("\\/", "/").replace("\\u002F", "/").strip().strip("\"'")
    if not candidate or candidate.lower().startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
        return None
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    if candidate.lower().startswith("www."):
        candidate = "https://" + candidate
    joined = candidate if candidate.lower().startswith(("http://", "https://")) else urljoin(base_url, candidate)
    parts = urlsplit(joined)
    host = (parts.hostname or "").lower()
    scheme = parts.scheme or "https"
    netloc = parts.netloc
    if host in PAB_HOST_ALIASES:
        scheme = "https"
        netloc = PAB_HOST + (f":{parts.port}" if parts.port else "")
    elif host in BALOCHISTAN_CODE_HOST_ALIASES:
        scheme = "https"
        netloc = BALOCHISTAN_CODE_HOST + (f":{parts.port}" if parts.port else "")
    path = quote(parts.path or "/", safe="/%:@,+;=()-.~_")
    query = (parts.query or "").replace(" ", "%20")
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_pap_public_url(raw: str, *, base_url: str) -> Optional[str]:
    """Normalize discovered candidates onto official public PAP hosts."""
    if not raw:
        return None
    candidate = html.unescape(str(raw)).replace("\\/", "/").replace("\\u002F", "/").strip().strip("\"'")
    if not candidate or candidate.lower().startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
        return None
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    if candidate.lower().startswith("www."):
        candidate = "https://" + candidate
    joined = candidate if candidate.lower().startswith(("http://", "https://")) else urljoin(base_url, candidate)
    parts = urlsplit(joined)
    host = (parts.hostname or "").lower()
    scheme = parts.scheme or "https"
    netloc = parts.netloc
    if host in PAP_HOST_ALIASES:
        scheme = "https"
        netloc = PAP_HOST + (f":{parts.port}" if parts.port else "")
    path = quote(parts.path or "/", safe="/%:@,+;=()-.~_")
    query = (parts.query or "").replace(" ", "%20")
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_punjab_public_url(raw: str, *, base_url: str) -> Optional[str]:
    """Normalize discovered candidates onto official Punjab Assembly / Punjab Laws hosts."""
    if not raw:
        return None
    candidate = html.unescape(str(raw)).replace("\\/", "/").replace("\\u002F", "/").strip().strip("\"'")
    if not candidate or candidate.lower().startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
        return None
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    if candidate.lower().startswith("www."):
        candidate = "https://" + candidate
    joined = candidate if candidate.lower().startswith(("http://", "https://")) else urljoin(base_url, candidate)
    parts = urlsplit(joined)
    host = (parts.hostname or "").lower()
    scheme = parts.scheme or "https"
    netloc = parts.netloc
    if host in PAP_HOST_ALIASES:
        scheme = "https"
        netloc = PAP_HOST + (f":{parts.port}" if parts.port else "")
    elif host in PUNJABLAWS_HOST_ALIASES:
        scheme = "https"
        netloc = PUNJABLAWS_HOST + (f":{parts.port}" if parts.port else "")
    path = quote(parts.path or "/", safe="/%:@,+;=()-.~_")
    query = (parts.query or "").replace(" ", "%20")
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_pas_public_url(raw: str, *, base_url: str) -> Optional[str]:
    """Normalize discovered candidates onto official public Sindh hosts."""
    if not raw:
        return None
    candidate = html.unescape(str(raw)).replace("\\/", "/").replace("\\u002F", "/").strip().strip("\"'")
    if not candidate or candidate.lower().startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
        return None
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    if candidate.lower().startswith("www."):
        candidate = "https://" + candidate
    joined = candidate if candidate.lower().startswith(("http://", "https://")) else urljoin(base_url, candidate)
    parts = urlsplit(joined)
    host = (parts.hostname or "").lower()
    scheme = parts.scheme or "https"
    netloc = parts.netloc
    if host in PAS_HOST_ALIASES:
        scheme = "https"
        netloc = PAS_HOST + (f":{parts.port}" if parts.port else "")
    elif host in SINDHLAWS_HOST_ALIASES:
        scheme = "https"
        netloc = SINDHLAWS_HOST + (f":{parts.port}" if parts.port else "")
    path = quote(parts.path or "/", safe="/%:@,+;=()-.~_")
    query = (parts.query or "").replace(" ", "%20")
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_pakp_public_url(raw: str, *, base_url: str) -> Optional[str]:
    """Normalize discovered candidates onto official public PAKP hosts."""
    if not raw:
        return None
    candidate = html.unescape(str(raw)).replace("\\/", "/").replace("\\u002F", "/").strip().strip("\"'")
    if not candidate or candidate.lower().startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
        return None
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    if candidate.lower().startswith("www."):
        candidate = "https://" + candidate
    joined = candidate if candidate.lower().startswith(("http://", "https://")) else urljoin(base_url, candidate)
    parts = urlsplit(joined)
    host = (parts.hostname or "").lower()
    scheme = parts.scheme or "https"
    netloc = parts.netloc
    if host in PAKP_HOST_ALIASES:
        scheme = "https"
        netloc = PAKP_HOST + (f":{parts.port}" if parts.port else "")
    path = quote(parts.path or "/", safe="/%:@,+;=()-.~_")
    query = (parts.query or "").replace(" ", "%20")
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_kpcode_public_url(raw: str, *, base_url: str) -> Optional[str]:
    """Normalize discovered candidates onto official public KPCODE hosts."""
    if not raw:
        return None
    candidate = html.unescape(str(raw)).replace("\\/", "/").replace("\\u002F", "/").strip().strip("\"'")
    if not candidate or candidate.lower().startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
        return None
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    if candidate.lower().startswith("www."):
        candidate = "https://" + candidate
    joined = candidate if candidate.lower().startswith(("http://", "https://")) else urljoin(base_url, candidate)
    parts = urlsplit(joined)
    host = (parts.hostname or "").lower()
    scheme = parts.scheme or "https"
    netloc = parts.netloc
    if host in KPCODE_HOST_ALIASES:
        scheme = "https"
        netloc = KPCODE_HOST + (f":{parts.port}" if parts.port else "")
    path = quote(parts.path or "/", safe="/%:@,+;=()-.~_")
    query = (parts.query or "").replace(" ", "%20")
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_senate_public_url(raw: str, *, base_url: str) -> Optional[str]:
    """Normalize discovered candidates onto official public Senate hosts."""
    if not raw:
        return None
    candidate = html.unescape(str(raw)).replace("\\/", "/").replace("\\u002F", "/").strip().strip("\"'")
    if not candidate or candidate.lower().startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
        return None
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    if candidate.lower().startswith("www."):
        candidate = "https://" + candidate
    joined = candidate if candidate.lower().startswith(("http://", "https://")) else urljoin(base_url, candidate)
    parts = urlsplit(joined)
    host = (parts.hostname or "").lower()
    scheme = parts.scheme or "https"
    netloc = parts.netloc
    if host in SENATE_HOST_ALIASES:
        scheme = "https"
        netloc = SENATE_HOST + (f":{parts.port}" if parts.port else "")
    path = quote(parts.path or "/", safe="/%:@,+;=()-.~_")
    query = "&".join(part for part in re.split(r"[?&]+", (parts.query or "").replace(" ", "%20")) if part)
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_na_public_url(raw: str, *, base_url: str) -> Optional[str]:
    """Normalize discovered candidates onto official public NA hosts."""
    if not raw:
        return None
    candidate = html.unescape(str(raw)).replace("\\/", "/").replace("\\u002F", "/").strip().strip("\"'")
    if not candidate or candidate.lower().startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
        return None
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    if candidate.lower().startswith("www."):
        candidate = "https://" + candidate
    joined = candidate if candidate.lower().startswith(("http://", "https://")) else urljoin(base_url, candidate)
    parts = urlsplit(joined)
    host = (parts.hostname or "").lower()
    scheme = parts.scheme or "https"
    netloc = parts.netloc
    if host in NA_HOST_ALIASES:
        scheme = "https"
        netloc = NA_HOST + (f":{parts.port}" if parts.port else "")
    path = quote(parts.path or "/", safe="/%:@,+;=()-.~_")
    query = (parts.query or "").replace(" ", "%20")
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_pcp_public_url(raw: str, *, base_url: str) -> Optional[str]:
    """Normalize discovered candidates onto official public PCP hosts."""
    if not raw:
        return None
    candidate = html.unescape(str(raw)).replace("\\/", "/").replace("\\u002F", "/").strip().strip("\"'")
    if not candidate or candidate.lower().startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
        return None
    if candidate.startswith("//"):
        candidate = "http:" + candidate
    if candidate.lower().startswith("www."):
        candidate = "http://" + candidate
    joined = candidate if candidate.lower().startswith(("http://", "https://")) else urljoin(base_url, candidate)
    parts = urlsplit(joined)
    host = (parts.hostname or "").lower()
    scheme = parts.scheme or "http"
    netloc = parts.netloc
    if host in PCP_HOST_ALIASES:
        netloc = PCP_HOST + (f":{parts.port}" if parts.port else "")
    path = quote(parts.path or "/", safe="/%:@,+;=()-.~_")
    query = (parts.query or "").replace(" ", "%20")
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_ajk_public_url(raw: str, *, base_url: str) -> Optional[str]:
    """Normalize discovered candidates onto official public AJK Law hosts."""
    if not raw:
        return None
    candidate = html.unescape(str(raw)).replace("\\/", "/").replace("\\u002F", "/").strip().strip("\"'")
    if not candidate or candidate.lower().startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
        return None
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    if candidate.lower().startswith("www."):
        candidate = "https://" + candidate
    joined = candidate if candidate.lower().startswith(("http://", "https://")) else urljoin(base_url, candidate)
    parts = urlsplit(joined)
    host = (parts.hostname or "").lower()
    scheme = parts.scheme or "https"
    netloc = parts.netloc
    if host in AJK_LAW_HOST_ALIASES:
        scheme = "https"
        netloc = AJK_LAW_HOST + (f":{parts.port}" if parts.port else "")
    path = quote(parts.path or "/", safe="/%:@,+;=()-.~_")
    query = (parts.query or "").replace(" ", "%20")
    return urlunsplit((scheme, netloc, path, query, ""))


def _classify_balochistan_discovered_url(url: str, *, hint_text: str = "") -> Optional[str]:
    parts = urlsplit(url)
    path = (parts.path or "/").lower()
    host = (parts.hostname or "").lower()
    query = (parts.query or "").lower()
    hint = (hint_text or "").lower()
    host_is_local_fixture = host in ("127.0.0.1", "localhost")

    if host in PAB_HOST_ALIASES or (host_is_local_fixture and (STORAGE_DOC_RE.search(path) or PAB_LISTING_PATH_RE.search(path))):
        if STORAGE_DOC_RE.search(path):
            return "document"
        if PAB_LISTING_PATH_RE.search(path):
            return "listing"
        return None

    if host not in BALOCHISTAN_CODE_HOST_ALIASES and not host_is_local_fixture:
        return None
    if BALOCHISTAN_CODE_DOC_RE.search(path):
        return "document"
    if "wise=download" in query or "download" in path:
        return "document"
    if BALOCHISTAN_CODE_LISTING_PATH_RE.search(path):
        return "listing"
    if BALOCHISTAN_CODE_DETAIL_PATH_RE.search(path):
        if "opendoc" in query:
            return "listing"
        qs = parse_qs(parts.query or "")
        for key in ("docid", "docc", "id", "lawid"):
            values = qs.get(key) or []
            if any(str(v).strip() for v in values):
                return "listing"
        if "act" in hint or "law" in hint or "ordinance" in hint or "rule" in hint:
            return "listing"
    return None


def _classify_pap_discovered_url(url: str) -> Optional[str]:
    path = (urlsplit(url).path or "/").lower()
    if PAP_ACT_DOC_RE.search(path):
        return "document"
    if PAP_LISTING_PATH_RE.search(path):
        return "listing"
    return None


def _classify_punjab_discovered_url(url: str, *, hint_text: str = "") -> Optional[str]:
    parts = urlsplit(url)
    path = (parts.path or "/").lower()
    host = (parts.hostname or "").lower()
    hint = (hint_text or "").lower()
    host_is_local_fixture = host in ("127.0.0.1", "localhost")
    if host in PAP_HOST_ALIASES or (host_is_local_fixture and (PAP_ACT_DOC_RE.search(path) or PAP_LISTING_PATH_RE.search(path))):
        return _classify_pap_discovered_url(url)
    if host not in PUNJABLAWS_HOST_ALIASES and not host_is_local_fixture:
        return None
    if PUNJABLAWS_DOC_RE.search(path):
        return "document"
    if "format=pdf" in (parts.query or "").lower():
        return "document"
    if PUNJABLAWS_LISTING_PATH_RE.search(path):
        return "listing"
    if PUNJABLAWS_DETAIL_PATH_RE.search(path):
        if PUNJABLAWS_LEGAL_HINT_RE.search(path) or PUNJABLAWS_LEGAL_HINT_RE.search(hint) or YEAR_RE.search(path) or YEAR_RE.search(hint):
            return "listing"
    if path.endswith((".html", ".htm")):
        if PUNJABLAWS_NAV_HINT_RE.search(path) or PUNJABLAWS_NAV_HINT_RE.search(hint):
            return "listing"
        if PUNJABLAWS_LEGAL_HINT_RE.search(path) or PUNJABLAWS_LEGAL_HINT_RE.search(hint) or YEAR_RE.search(path) or YEAR_RE.search(hint):
            return "listing"
    return None


def _classify_pas_discovered_url(url: str) -> Optional[str]:
    parts = urlsplit(url)
    path = (parts.path or "/").lower()
    host = (parts.hostname or "").lower()
    query = (parts.query or "").lower()
    host_is_local_fixture = host in ("127.0.0.1", "localhost")
    if host in PAS_HOST_ALIASES or (host_is_local_fixture and (PAS_ACT_DOC_RE.search(path) or PAS_LISTING_PATH_RE.search(path) or PAS_DETAIL_PATH_RE.search(path))):
        if PAS_ACT_DOC_RE.search(path):
            return "document"
        if PAS_LISTING_PATH_RE.search(path) or PAS_DETAIL_PATH_RE.search(path):
            return "listing"
    if host not in SINDHLAWS_HOST_ALIASES and not host_is_local_fixture:
        return None
    if SINDHLAWS_DOC_RE.search(path):
        return "document"
    if SINDHLAWS_DETAIL_PATH_RE.search(path):
        if "x=" in query and "year=" in query:
            return "listing"
        if host_is_local_fixture:
            return "listing"
    if SINDHLAWS_LISTING_PATH_RE.search(path):
        if path == "/gazette.aspx":
            if any(f"pg={section}" in query for section in ("act", "ordinance", "bills")):
                return "listing"
            if host_is_local_fixture:
                return "listing"
            return None
        return "listing"
    return None


def _classify_pakp_discovered_url(url: str) -> Optional[str]:
    path = (urlsplit(url).path or "/").lower()
    if PAKP_ACT_DOC_RE.search(path):
        return "document"
    if PAKP_LISTING_PATH_RE.search(path) or PAKP_DETAIL_PATH_RE.search(path):
        return "listing"
    return None


def _classify_kpcode_discovered_url(url: str) -> Optional[str]:
    path = (urlsplit(url).path or "/").lower()
    if KPCODE_DOC_RE.search(path):
        return "document"
    if path in ("/", "/homepage", "/homepage/"):
        return "listing"
    if KPCODE_LAW_DETAIL_RE.search(path) or KPCODE_RULE_DETAIL_RE.search(path):
        return "listing"
    if path.startswith("/homepage/list_all_law"):
        return "listing"
    if path.startswith(("/homepage/latest_more", "/homepage/recent_updated", "/homepage/most_view")):
        return "listing"
    if path.startswith(("/homepage/alphabetical", "/homepage/chronological", "/homepage/categorical", "/homepage/dept_wise", "/homepage/rules", "/homepage/urdu")):
        return "listing"
    if path.startswith(("/homepage/search_by_year/", "/homepage/search_by_category/", "/homepage/search_by_dept/", "/homepage/search_by_year_rule/")):
        return "listing"
    return None


def _classify_senate_discovered_url(url: str) -> Optional[str]:
    path = (urlsplit(url).path or "/").lower()
    if SENATE_DOC_RE.search(path):
        return "document"
    if SENATE_LISTING_PATH_RE.search(path) or SENATE_DETAIL_PATH_RE.search(path):
        return "listing"
    return None


def _classify_na_discovered_url(url: str) -> Optional[str]:
    path = (urlsplit(url).path or "/").lower()
    if NA_DOC_RE.search(path):
        return "document"
    if NA_LISTING_PATH_RE.search(path) or _is_na_detail_discovered_url(url):
        return "listing"
    return None


def _is_na_detail_discovered_url(url: str) -> bool:
    parts = urlsplit(url)
    path = (parts.path or "/").lower()
    if NA_DETAIL_PATH_RE.search(path):
        return True
    if path not in ("/en/acts-tenure.php", "/en/acts.php", "/en/bills.php", "/en/bills-15.php", "/en/bills-passed.php"):
        return False
    qs = parse_qs(parts.query or "")
    for key in ("id", "bill_id", "act_id", "detail_id", "doc_id"):
        values = qs.get(key) or []
        if any(str(v).strip() for v in values):
            return True
    return False


def _classify_pcp_discovered_url(url: str) -> Optional[str]:
    path = (urlsplit(url).path or "/").lower()
    if PCP_DOC_RE.search(path):
        return "document"
    if PCP_LISTING_PATH_RE.search(path) or PCP_DETAIL_PATH_RE.search(path):
        return "listing"
    return None


def _classify_ajk_discovered_url(url: str, *, hint_text: str = "") -> Optional[str]:
    parts = urlsplit(url)
    path = (parts.path or "/").lower()
    host = (parts.hostname or "").lower()
    hint = (hint_text or "").lower()
    host_is_local_fixture = host in ("127.0.0.1", "localhost")
    if host not in AJK_LAW_HOST_ALIASES and not host_is_local_fixture:
        return None
    if AJK_LAW_DOC_RE.search(path):
        return "document"
    if AJK_LAW_LISTING_PATH_RE.search(path):
        return "listing"
    if AJK_LAW_DETAIL_PATH_RE.search(path):
        if AJK_LAW_LEGAL_HINT_RE.search(path) or AJK_LAW_LEGAL_HINT_RE.search(hint) or YEAR_RE.search(path) or YEAR_RE.search(hint):
            return "listing"
    return None


class BalochistanAssemblyPipeline(PublicPipeline):
    """Source-specific extraction for PAB acts + Balochistan Code laws portal documents."""

    async def handle_listing(self, res, fr: CrawlFrontier) -> None:  # type: ignore[override]
        depth = int(fr.query_json.get("depth", 0))
        max_depth = int(self.source.crawl_max_depth or 2)
        target_kind = fr.query_json.get("target_kind", "statute")
        docs: Dict[str, Dict[str, Any]] = {}
        listings: Dict[str, Dict[str, Any]] = {}
        inherited_meta = dict(fr.query_json.get("meta") or {})

        if self._is_balochistan_code_detail_listing(res.final_url):
            self._collect_balochistan_code_detail_document_links(
                html_text=res.text,
                base_url=res.final_url,
                docs=docs,
                inherited_meta=inherited_meta,
            )
        elif self._is_balochistan_code_url(res.final_url):
            self._collect_balochistan_code_listing_rows(
                html_text=res.text,
                base_url=res.final_url,
                listings=listings,
            )
            self._collect_listing_links(
                html_text=res.text,
                base_url=res.final_url,
                listings=listings,
                route_meta={
                    "listing_fetch": "navigation_links",
                    "source_section": inherited_meta.get("source_section", "laws_portal"),
                    **inherited_meta,
                },
            )
        else:
            self._collect_structured_act_rows(
                html_text=res.text,
                base_url=res.final_url,
                docs=docs,
            )
            self._collect_listing_links(
                html_text=res.text,
                base_url=res.final_url,
                listings=listings,
                route_meta=inherited_meta,
            )

        added = await self._enqueue_documents_with_meta(docs, listing_url=res.final_url, default_target_kind=target_kind)
        self.stats["discovered"] += added

        if depth >= max_depth:
            return
        for nurl, nmeta in listings.items():
            next_target_kind = str(nmeta.get("target_kind") or target_kind)
            key = f"listing:{nurl}"
            exists = (
                await self.db.execute(
                    select(CrawlFrontier).where(
                        CrawlFrontier.source_name == self.source.source_name,
                        CrawlFrontier.tier == 0,
                        CrawlFrontier.query_key == key,
                    )
                )
            ).scalars().first()
            if exists is None:
                route = {"listing": res.final_url}
                for key_name in (
                    "listing_fetch",
                    "detail_fetch",
                    "discovery_channel",
                    "result_index",
                    "source_section",
                    "tenure",
                    "act_year",
                    "act_no",
                    "act_title",
                    "act_passed_on",
                    "act_assented_on",
                    "act_type",
                    "detail_url",
                    "detail_title",
                ):
                    if key_name in nmeta:
                        route[key_name] = nmeta[key_name]
                self.db.add(
                    CrawlFrontier(
                        source_name=self.source.source_name,
                        tier=0,
                        query_key=key,
                        query_json={
                            "kind": "listing",
                            "url": nurl,
                            "target_kind": next_target_kind,
                            "depth": depth + 1,
                            "route": route,
                            "meta": nmeta,
                        },
                        cursor_json={},
                        priority=40,
                    )
                )
                self.stats["discovered"] += 1
        await self.db.flush()

    def _collect_structured_act_rows(self, *, html_text: str, base_url: str, docs: Dict[str, Dict[str, Any]]) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        row_index = 0

        for tenure_item in soup.select("#tenureAccordion > .accordion-item"):
            tenure_button = tenure_item.select_one(".accordion-header button")
            tenure = (tenure_button.get_text(" ", strip=True) if tenure_button else "")[:40]
            for year_item in tenure_item.select(".accordion-body .accordion-item"):
                year_button = year_item.select_one(".accordion-header button")
                year = (year_button.get_text(" ", strip=True) if year_button else "")[:8]
                if year and not YEAR_RE.search(year):
                    year = ""

                for table in year_item.select("table.table"):
                    headers = [th.get_text(" ", strip=True).lower() for th in table.select("thead th")]
                    if "act no" not in headers or "act title" not in headers:
                        continue
                    for tr in table.select("tbody tr"):
                        cells = tr.find_all("td")
                        if len(cells) < 2:
                            continue
                        link = cells[1].find("a", href=True)
                        if link is None:
                            continue
                        row_index += 1
                        href = link.get("href", "")
                        title = link.get_text(" ", strip=True) or cells[1].get_text(" ", strip=True)
                        ext = (urlsplit(href).path.rsplit(".", 1)[-1].lower() if "." in (urlsplit(href).path or "") else "")

                        row_meta: Dict[str, Any] = {
                            "listing_fetch": "acts_table",
                            "discovery_channel": "acts-table-row",
                            "result_index": row_index,
                            "source_section": "acts",
                        }
                        if tenure:
                            row_meta["tenure"] = tenure
                        if year:
                            row_meta["act_year"] = year
                        act_no = cells[0].get_text(" ", strip=True)[:40]
                        if act_no:
                            row_meta["act_no"] = act_no
                        if title:
                            row_meta["act_title"] = title[:280]
                        if len(cells) > 2:
                            passed = cells[2].get_text(" ", strip=True)[:40]
                            if passed:
                                row_meta["act_passed_on"] = passed
                        if len(cells) > 3:
                            assented = cells[3].get_text(" ", strip=True)[:40]
                            if assented:
                                row_meta["act_assented_on"] = assented
                        if len(cells) > 4:
                            act_type = cells[4].get_text(" ", strip=True)[:80]
                            if act_type:
                                row_meta["act_type"] = act_type
                        if ext:
                            row_meta["document_format"] = ext
                            if ext == "pdf":
                                row_meta["expect_pdf"] = True

                        self._capture_candidate(
                            raw=href,
                            hint=title[:240],
                            base_url=base_url,
                            docs=docs,
                            listings={},
                            route_meta=row_meta,
                        )

    @staticmethod
    def _is_balochistan_code_url(url: str) -> bool:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if host in BALOCHISTAN_CODE_HOST_ALIASES:
            return True
        if host not in ("127.0.0.1", "localhost"):
            return False
        path = (parts.path or "/").lower()
        query = (parts.query or "").lower()
        return bool(
            BALOCHISTAN_CODE_LISTING_PATH_RE.search(path)
            or BALOCHISTAN_CODE_DETAIL_PATH_RE.search(path)
            or "wise=" in query
        )

    def _is_balochistan_code_detail_listing(self, url: str) -> bool:
        parts = urlsplit(url)
        if not self._is_balochistan_code_url(url):
            return False
        path = (parts.path or "/").lower()
        if not BALOCHISTAN_CODE_DETAIL_PATH_RE.search(path):
            return False
        query = (parts.query or "").lower()
        if "opendoc" in query:
            return True
        qs = parse_qs(parts.query or "")
        return any(any(str(v).strip() for v in (qs.get(key) or [])) for key in ("docid", "docc", "id", "lawid"))

    def _collect_balochistan_code_listing_rows(
        self,
        *,
        html_text: str,
        base_url: str,
        listings: Dict[str, Dict[str, Any]],
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        row_index = 0
        for table in soup.select("table"):
            headers = [th.get_text(" ", strip=True).lower() for th in table.select("thead th")]
            if not headers:
                headers = [th.get_text(" ", strip=True).lower() for th in table.select("tr th")]
            if not self._looks_like_balochistan_code_table(headers):
                continue
            rows = table.select("tbody tr") or table.select("tr")
            for tr in rows:
                cells = tr.find_all("td")
                if len(cells) < 2:
                    continue
                link = None
                for cell in cells:
                    link = cell.find("a", href=True)
                    if link is not None:
                        break
                if link is None:
                    continue
                row_index += 1
                title = link.get_text(" ", strip=True) or cells[min(len(cells) - 1, 1)].get_text(" ", strip=True)
                row_meta: Dict[str, Any] = {
                    "listing_fetch": "balochistancode_table",
                    "discovery_channel": "balochistancode-table-row",
                    "source_section": "laws_portal",
                    "result_index": row_index,
                }
                self._add_balochistan_code_row_provenance(row_meta=row_meta, cells=cells, headers=headers, title=title)
                self._capture_candidate(
                    raw=link.get("href", ""),
                    hint=title[:240],
                    base_url=base_url,
                    docs={},
                    listings=listings,
                    route_meta=row_meta,
                )

    @staticmethod
    def _looks_like_balochistan_code_table(headers: List[str]) -> bool:
        if not headers:
            return False
        has_title = any("title" in h or "subject" in h or "name" in h for h in headers)
        has_lawish = any(
            "law" in h or "act" in h or "ordinance" in h or "rule" in h or "year" in h or "no" in h
            for h in headers
        )
        return has_title and has_lawish

    @staticmethod
    def _balochistan_code_target_kind(*, act_type: str, title: str, fallback: str) -> str:
        """Infer statute vs instrument for Balochistan Code rows/detail pages."""
        default_kind = fallback if fallback in ("statute", "instrument") else "statute"
        lowered_type = (act_type or "").lower()
        lowered_title = (title or "").lower()
        if re.search(r"\b(act|law|statute)\b", lowered_type):
            return "statute"
        if re.search(r"\b(ordinance|rules?|regulations?|notification|order|by-law|bye-law)\b", lowered_type):
            return "instrument"
        if re.search(r"\b(ordinance|rules?|regulations?|notification|order|by-law|bye-law)\b", lowered_title):
            return "instrument"
        if re.search(r"\b(act|law|statute)\b", lowered_title):
            return "statute"
        return default_kind

    def _add_balochistan_code_row_provenance(
        self,
        *,
        row_meta: Dict[str, Any],
        cells: List[Any],
        headers: List[str],
        title: str,
    ) -> None:
        if title:
            row_meta["act_title"] = title[:280]
        for idx, header in enumerate(headers):
            if idx >= len(cells):
                continue
            value = cells[idx].get_text(" ", strip=True)
            if not value:
                continue
            if ("act no" in header or "law no" in header or "no." in header or header == "no") and "act_no" not in row_meta:
                row_meta["act_no"] = value[:80]
            elif "year" in header and "act_year" not in row_meta and YEAR_RE.search(value):
                row_meta["act_year"] = YEAR_RE.search(value).group(0)  # type: ignore[union-attr]
            elif ("promulg" in header or "passed" in header or "date" in header) and "act_passed_on" not in row_meta:
                row_meta["act_passed_on"] = value[:40]
            elif ("type" in header or "category" in header) and "act_type" not in row_meta:
                row_meta["act_type"] = value[:80]
        if "act_type" not in row_meta and title:
            low = title.lower()
            if "ordinance" in low:
                row_meta["act_type"] = "ordinance"
            elif "rule" in low:
                row_meta["act_type"] = "rules"
            elif "act" in low:
                row_meta["act_type"] = "act"
            elif "law" in low:
                row_meta["act_type"] = "law"
        if "act_year" not in row_meta:
            year_match = YEAR_RE.search(title or "") or YEAR_RE.search(row_meta.get("act_no", ""))
            if year_match:
                row_meta["act_year"] = year_match.group(0)
        row_meta["target_kind"] = self._balochistan_code_target_kind(
            act_type=str(row_meta.get("act_type", "")),
            title=str(row_meta.get("act_title", "")),
            fallback=str(row_meta.get("target_kind") or "statute"),
        )

    def _collect_balochistan_code_detail_document_links(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        inherited_meta: Dict[str, Any],
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        detail_title_node = soup.select_one("h1") or soup.select_one("h2") or soup.select_one("title")
        detail_title = detail_title_node.get_text(" ", strip=True)[:280] if detail_title_node else ""
        base_meta = dict(inherited_meta)
        base_meta.setdefault("detail_url", base_url)
        if detail_title:
            base_meta.setdefault("detail_title", detail_title)
            base_meta.setdefault("act_title", detail_title)
            if "act_year" not in base_meta and YEAR_RE.search(detail_title):
                base_meta["act_year"] = YEAR_RE.search(detail_title).group(0)  # type: ignore[union-attr]

        for row in soup.select("table tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            label = cells[0].get_text(" ", strip=True).lower()
            value = cells[1].get_text(" ", strip=True)
            if not value:
                continue
            if ("act no" in label or "law no" in label) and "act_no" not in base_meta:
                base_meta["act_no"] = value[:80]
            elif ("promulgation" in label or "passed" in label or "date of passing" in label) and "act_passed_on" not in base_meta:
                base_meta["act_passed_on"] = value[:40]
            elif ("type" in label or "category" in label) and "act_type" not in base_meta:
                base_meta["act_type"] = value[:80]
            if "act_year" not in base_meta and YEAR_RE.search(value):
                base_meta["act_year"] = YEAR_RE.search(value).group(0)  # type: ignore[union-attr]
        base_meta["target_kind"] = self._balochistan_code_target_kind(
            act_type=str(base_meta.get("act_type", "")),
            title=str(base_meta.get("act_title") or detail_title),
            fallback=str(base_meta.get("target_kind") or "statute"),
        )

        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            hint = a.get_text(" ", strip=True)[:240] or base_meta.get("act_title", "")[:240]
            low = f"{href} {hint}".lower()
            if not ("download" in low or ".pdf" in low or "wise=download" in low):
                continue
            route_meta = dict(base_meta)
            route_meta["detail_fetch"] = "balochistancode_detail_download"
            route_meta["discovery_channel"] = "balochistancode-detail-file-link"
            route_meta.setdefault("source_section", "laws_portal")
            self._capture_candidate(
                raw=href,
                hint=hint,
                base_url=base_url,
                docs=docs,
                listings={},
                route_meta=route_meta,
            )

    def _collect_listing_links(
        self,
        *,
        html_text: str,
        base_url: str,
        listings: Dict[str, Dict[str, Any]],
        route_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        for a in soup.find_all("a", href=True):
            self._capture_candidate(
                raw=a.get("href", ""),
                hint=a.get_text(" ", strip=True)[:240],
                base_url=base_url,
                docs={},
                listings=listings,
                route_meta=dict(route_meta or {}),
            )

    def _capture_candidate(
        self,
        *,
        raw: str,
        hint: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: Dict[str, Dict[str, Any]],
        route_meta: Dict[str, Any],
    ) -> None:
        normalized = normalize_pab_public_url(raw, base_url=base_url)
        if not normalized:
            return
        try:
            safe = check_url_policy(
                normalized,
                self.source.allow_list or [],
                document_cdn_hosts=self.source.document_cdn_hosts or [],
                allow_private_for_tests=_tests_allow_private(),
            )
        except URLPolicyError:
            self.stats["rejected_urls"] += 1
            return

        kind = _classify_balochistan_discovered_url(safe, hint_text=hint)
        if kind == "document":
            parts = urlsplit(safe)
            path = (parts.path or "").lower()
            host = (parts.hostname or "").lower()
            query = (parts.query or "").lower()
            ext = path.rsplit(".", 1)[-1] if "." in path else ""
            is_balochistan_code_download = (
                host in BALOCHISTAN_CODE_HOST_ALIASES or host in ("127.0.0.1", "localhost")
            ) and ("wise=download" in query or "download" in path)
            meta = {
                "discovery_hint": hint[:240],
                "pdf_endpoint_kind": (
                    "storage-file"
                    if path.startswith("/storage/")
                    else ("balochistancode-download-file" if is_balochistan_code_download else "direct-file")
                ),
                **route_meta,
            }
            if ext and "document_format" not in meta:
                meta["document_format"] = ext
            if ext == "pdf" or (is_balochistan_code_download and "expect_pdf" not in meta):
                meta["expect_pdf"] = True
                if "document_format" not in meta:
                    meta["document_format"] = "pdf"
            existing = docs.get(safe)
            if existing is None:
                docs[safe] = meta
            else:
                for key, value in meta.items():
                    if key not in existing and value not in ("", None):
                        existing[key] = value
        elif kind == "listing" and safe != base_url:
            if safe not in listings:
                listings[safe] = dict(route_meta)
                listings[safe].setdefault("detail_url", safe)
            else:
                for key, value in route_meta.items():
                    if key not in listings[safe] and value not in ("", None):
                        listings[safe][key] = value

    async def _enqueue_documents_with_meta(
        self,
        docs: Dict[str, Dict[str, Any]],
        *,
        listing_url: str,
        default_target_kind: str,
    ) -> int:
        added = 0
        for url, meta in docs.items():
            kind = str(meta.get("target_kind") or default_target_kind)
            key = f"{kind}:{url}"
            exists = (
                await self.db.execute(
                    select(CrawlFrontier).where(
                        CrawlFrontier.source_name == self.source.source_name,
                        CrawlFrontier.tier == 0,
                        CrawlFrontier.query_key == key,
                    )
                )
            ).scalars().first()
            if exists is not None:
                continue

            route: Dict[str, Any] = {"listing": listing_url}
            for key_name in (
                "listing_fetch",
                "detail_fetch",
                "discovery_channel",
                "result_index",
                "source_section",
                "tenure",
                "act_year",
                "act_no",
                "act_title",
                "act_passed_on",
                "act_assented_on",
                "act_type",
                "detail_category",
                "detail_department",
                "detail_specific_category",
                "detail_url",
                "detail_title",
                "document_format",
                "pdf_endpoint_kind",
            ):
                if key_name in meta:
                    route[key_name] = meta[key_name]
            self.db.add(
                CrawlFrontier(
                    source_name=self.source.source_name,
                    tier=0,
                    query_key=key,
                    query_json={
                        "kind": kind,
                        "url": url,
                        "route": route,
                        "meta": meta,
                        "expect_pdf": bool(meta.get("expect_pdf")),
                    },
                    cursor_json={},
                    priority=50,
                )
            )
            added += 1
        await self.db.flush()
        return added

    async def _drain_one(self, fr: CrawlFrontier) -> str:
        """
        Process one frontier row with PDF-signature gating for statute/instrument PDFs.

        Balochistan statutes may arrive from direct `/storage/...pdf` links or Balochistan Code
        download endpoints; both paths must fail closed when the payload is not a real PDF.
        """
        fr.status = "in_progress"
        fr.attempts += 1
        kind = fr.query_json.get("kind", "statute")
        url = fr.query_json.get("url")
        route = {**(fr.query_json.get("route") or {}), "frontier": fr.query_key}
        expect_pdf = bool(fr.query_json.get("expect_pdf"))
        try:
            res = await self.fetch(url)
            if res.verdict.kind in ("verification", "login"):
                fr.status = "retired"
                fr.last_error = f"page requires {res.verdict.kind}; public source has no login path"
            elif expect_pdf and not has_pdf_signature(res.content):
                fr.status = "retired"
                fr.last_error = f"missing %PDF signature for {kind} document URL"
            elif kind == "judgment" and (_url_looks_like_pdf(url) or res.is_pdf) and not has_pdf_signature(res.content):
                fr.status = "retired"
                fr.last_error = "missing %PDF signature for judgment document URL"
            elif res.status_code >= 400:
                fr.status = "retired" if res.status_code in (404, 410) else "pending"
                fr.last_error = f"HTTP {res.status_code}"
            else:
                if kind == "judgment":
                    await self.ingest_judgment(res, route=route, row_meta=fr.query_json.get("meta") or {})
                elif kind in ("statute", "instrument"):
                    await self.ingest_statute(res, route=route, kind=kind, meta=fr.query_json.get("meta") or {})
                elif kind == "listing":
                    await self.handle_listing(res, fr)
                fr.status = "done"
                fr.last_error = None
                fr.last_run_at = datetime.now(timezone.utc)
        except URLPolicyError as exc:
            fr.status = "retired"
            fr.last_error = str(exc)[:1000]
        except RobotsUnavailable as exc:
            fr.status = "pending"
            fr.attempts -= 1
            fr.last_error = str(exc)[:1000]
            self.stats["deferred"] += 1
        except ExplicitBlock:
            fr.status = "pending"
            await self.db.flush()
            return "halted"
        except Exception as exc:
            self.stats["errors"] += 1
            fr.last_error = str(exc)[:1000]
            fr.status = "pending" if fr.attempts < 3 else "retired"
            logger.exception("%s: frontier item failed %s", self.source.source_name, url)
        await self.db.flush()
        return "ok"


class PunjabAssemblyPipeline(BalochistanAssemblyPipeline):
    """Source-specific extraction for PAP + PunjabLaws listings, detail pages and document links."""

    async def handle_listing(self, res, fr: CrawlFrontier) -> None:  # type: ignore[override]
        depth = int(fr.query_json.get("depth", 0))
        max_depth = int(self.source.crawl_max_depth or 2)
        target_kind = fr.query_json.get("target_kind", "statute")
        docs: Dict[str, Dict[str, Any]] = {}
        listings: Dict[str, Dict[str, Any]] = {}
        inherited_meta = dict(fr.query_json.get("meta") or {})

        if self._is_punjablaws_detail_listing(res.final_url):
            self._collect_punjablaws_detail_document_links(
                html_text=res.text,
                base_url=res.final_url,
                docs=docs,
                inherited_meta=inherited_meta,
            )
        else:
            self._collect_structured_act_rows(
                html_text=res.text,
                base_url=res.final_url,
                docs=docs,
                listings=listings,
            )
            self._collect_punjablaws_table_rows(
                html_text=res.text,
                base_url=res.final_url,
                docs=docs,
                listings=listings,
            )

        self._collect_listing_links(
            html_text=res.text,
            base_url=res.final_url,
            docs=docs,
            listings=listings,
            inherited_meta={
                "listing_fetch": "navigation_links",
                "source_section": "acts",
                **inherited_meta,
            },
        )

        added = await self._enqueue_documents_with_meta(docs, listing_url=res.final_url, default_target_kind=target_kind)
        self.stats["discovered"] += added

        if depth >= max_depth:
            return
        for nurl, nmeta in listings.items():
            key = f"listing:{nurl}"
            exists = (
                await self.db.execute(
                    select(CrawlFrontier).where(
                        CrawlFrontier.source_name == self.source.source_name,
                        CrawlFrontier.tier == 0,
                        CrawlFrontier.query_key == key,
                    )
                )
            ).scalars().first()
            if exists is None:
                route = {"listing": res.final_url}
                for key_name in (
                    "listing_fetch",
                    "detail_fetch",
                    "discovery_channel",
                    "result_index",
                    "source_section",
                    "act_year",
                    "act_no",
                    "act_title",
                    "act_passed_on",
                    "act_assented_on",
                    "act_type",
                    "detail_url",
                    "detail_title",
                ):
                    if key_name in nmeta:
                        route[key_name] = nmeta[key_name]
                self.db.add(
                    CrawlFrontier(
                        source_name=self.source.source_name,
                        tier=0,
                        query_key=key,
                        query_json={
                            "kind": "listing",
                            "url": nurl,
                            "target_kind": target_kind,
                            "depth": depth + 1,
                            "route": route,
                            "meta": nmeta,
                        },
                        cursor_json={},
                        priority=40,
                    )
                )
                self.stats["discovered"] += 1
        await self.db.flush()

    @staticmethod
    def _is_punjablaws_detail_listing(url: str) -> bool:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if host not in PUNJABLAWS_HOST_ALIASES and host not in ("127.0.0.1", "localhost"):
            return False
        path = (parts.path or "/").lower()
        if PUNJABLAWS_LISTING_PATH_RE.search(path):
            return False
        return PUNJABLAWS_DETAIL_PATH_RE.search(path) is not None

    def _collect_structured_act_rows(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:  # type: ignore[override]
        soup = BeautifulSoup(html_text or "", "html.parser")
        row_index = 0

        for table in soup.select("table"):
            headers = [th.get_text(" ", strip=True).lower() for th in table.select("thead th")]
            if not headers:
                headers = [th.get_text(" ", strip=True).lower() for th in table.select("tr th")]
            if not self._looks_like_acts_table(headers):
                continue

            rows = table.select("tbody tr") or table.select("tr")
            for tr in rows:
                cells = tr.find_all("td")
                if len(cells) < 2:
                    continue
                link = self._row_link(cells, headers)
                if link is None:
                    continue

                row_index += 1
                href = link.get("href", "")
                title = link.get_text(" ", strip=True) or cells[min(len(cells) - 1, 1)].get_text(" ", strip=True)
                ext = (urlsplit(href).path.rsplit(".", 1)[-1].lower() if "." in (urlsplit(href).path or "") else "")

                row_meta: Dict[str, Any] = {
                    "listing_fetch": "acts_table",
                    "discovery_channel": "acts-table-row",
                    "result_index": row_index,
                    "source_section": "acts",
                }
                self._add_row_provenance(row_meta=row_meta, cells=cells, headers=headers, title=title)

                if ext:
                    row_meta["document_format"] = ext
                    if ext == "pdf":
                        row_meta["expect_pdf"] = True
                self._capture_candidate(
                    raw=href,
                    hint=title[:240],
                    base_url=base_url,
                    docs=docs,
                    listings=(listings if listings is not None else {}),
                    route_meta=row_meta,
                )

    def _collect_punjablaws_table_rows(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: Dict[str, Dict[str, Any]],
    ) -> None:
        host = (urlsplit(base_url).hostname or "").lower()
        if host not in PUNJABLAWS_HOST_ALIASES and host not in ("127.0.0.1", "localhost"):
            return
        soup = BeautifulSoup(html_text or "", "html.parser")
        row_index = 0
        for table in soup.select("table"):
            headers = [th.get_text(" ", strip=True).lower() for th in table.select("thead th")]
            if not headers:
                headers = [th.get_text(" ", strip=True).lower() for th in table.select("tr th")]
            if not self._looks_like_punjablaws_table(headers):
                continue
            rows = table.select("tbody tr") or table.select("tr")
            for tr in rows:
                cells = tr.find_all("td")
                if len(cells) < 2:
                    continue
                link = None
                for cell in cells:
                    link = cell.find("a", href=True)
                    if link is not None:
                        break
                if link is None:
                    continue
                row_index += 1
                title = link.get_text(" ", strip=True) or cells[min(len(cells) - 1, 1)].get_text(" ", strip=True)
                row_meta: Dict[str, Any] = {
                    "listing_fetch": "punjablaws_table",
                    "discovery_channel": "punjablaws-table-row",
                    "source_section": "acts",
                    "result_index": row_index,
                }
                self._add_punjablaws_row_provenance(row_meta=row_meta, cells=cells, headers=headers, title=title)
                self._capture_candidate(
                    raw=link.get("href", ""),
                    hint=title[:240],
                    base_url=base_url,
                    docs=docs,
                    listings=listings,
                    route_meta=row_meta,
                )

    @staticmethod
    def _looks_like_punjablaws_table(headers: List[str]) -> bool:
        if not headers:
            return False
        has_title = any("title" in h or "subject" in h or "name" in h for h in headers)
        has_lawish_column = any("act" in h or "ordinance" in h or "rule" in h or "law" in h or "year" in h for h in headers)
        return has_title and has_lawish_column

    def _add_punjablaws_row_provenance(self, *, row_meta: Dict[str, Any], cells: List[Any], headers: List[str], title: str) -> None:
        if title:
            row_meta["act_title"] = title[:280]
        for idx, header in enumerate(headers):
            if idx >= len(cells):
                continue
            value = cells[idx].get_text(" ", strip=True)
            if not value:
                continue
            if ("act no" in header or "law no" in header or "no." in header) and "act_no" not in row_meta:
                row_meta["act_no"] = value[:80]
            elif "year" in header and "act_year" not in row_meta and YEAR_RE.search(value):
                row_meta["act_year"] = YEAR_RE.search(value).group(0)  # type: ignore[union-attr]
            elif ("passed" in header or "date" in header) and "act_passed_on" not in row_meta:
                row_meta["act_passed_on"] = value[:40]
            elif "assent" in header and "act_assented_on" not in row_meta:
                row_meta["act_assented_on"] = value[:40]
            elif ("type" in header or "category" in header) and "act_type" not in row_meta:
                row_meta["act_type"] = value[:80]
        if "act_type" not in row_meta and title:
            low = title.lower()
            if "ordinance" in low:
                row_meta["act_type"] = "ordinance"
            elif "rule" in low:
                row_meta["act_type"] = "rules"
            elif "act" in low:
                row_meta["act_type"] = "act"
        if "act_year" not in row_meta:
            year_match = YEAR_RE.search(title or "") or YEAR_RE.search(row_meta.get("act_no", ""))
            if year_match:
                row_meta["act_year"] = year_match.group(0)

    @staticmethod
    def _looks_like_acts_table(headers: List[str]) -> bool:
        if not headers:
            return False
        has_no = any("act no" in h or "act number" in h or "act #" in h for h in headers)
        has_title = any("act title" in h or h == "title" for h in headers)
        return has_no and has_title

    @staticmethod
    def _row_link(cells: List[Any], headers: List[str]):
        title_idx = next((i for i, h in enumerate(headers) if "act title" in h or h == "title"), None)
        if title_idx is not None and title_idx < len(cells):
            link = cells[title_idx].find("a", href=True)
            if link is not None:
                return link
        for cell in cells:
            link = cell.find("a", href=True)
            if link is not None:
                return link
        return None

    def _add_row_provenance(self, *, row_meta: Dict[str, Any], cells: List[Any], headers: List[str], title: str) -> None:
        act_no_idx = next((i for i, h in enumerate(headers) if "act no" in h or "act number" in h or "act #" in h), None)
        if act_no_idx is not None and act_no_idx < len(cells):
            act_no = cells[act_no_idx].get_text(" ", strip=True)[:40]
            if act_no:
                row_meta["act_no"] = act_no

        if title:
            row_meta["act_title"] = title[:280]

        for idx, header in enumerate(headers):
            if idx >= len(cells):
                continue
            value = cells[idx].get_text(" ", strip=True)
            if not value:
                continue
            if "passed" in header:
                row_meta["act_passed_on"] = value[:40]
            elif "assent" in header:
                row_meta["act_assented_on"] = value[:40]
            elif "type" in header:
                row_meta["act_type"] = value[:80]
            elif "year" in header and YEAR_RE.search(value):
                row_meta["act_year"] = YEAR_RE.search(value).group(0)  # type: ignore[union-attr]

        if "act_year" not in row_meta:
            year_match = YEAR_RE.search(title or "")
            if year_match:
                row_meta["act_year"] = year_match.group(0)

    def _collect_punjablaws_detail_document_links(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        inherited_meta: Dict[str, Any],
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        detail_title_node = soup.select_one("h1") or soup.select_one("h2") or soup.select_one("title")
        detail_title = detail_title_node.get_text(" ", strip=True)[:280] if detail_title_node else ""
        base_meta = dict(inherited_meta)
        base_meta.setdefault("detail_url", base_url)
        if detail_title:
            base_meta.setdefault("detail_title", detail_title)
            base_meta.setdefault("act_title", detail_title)
            if "act_type" not in base_meta:
                low = detail_title.lower()
                if "ordinance" in low:
                    base_meta["act_type"] = "ordinance"
                elif "rule" in low:
                    base_meta["act_type"] = "rules"
                elif "act" in low:
                    base_meta["act_type"] = "act"
            if "act_year" not in base_meta and YEAR_RE.search(detail_title):
                base_meta["act_year"] = YEAR_RE.search(detail_title).group(0)  # type: ignore[union-attr]
        for row in soup.select("table tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            label = cells[0].get_text(" ", strip=True).lower()
            value = cells[1].get_text(" ", strip=True)
            if not value:
                continue
            if ("act no" in label or "law no" in label) and "act_no" not in base_meta:
                base_meta["act_no"] = value[:80]
            elif ("passed" in label or "date of passing" in label) and "act_passed_on" not in base_meta:
                base_meta["act_passed_on"] = value[:40]
            elif "assent" in label and "act_assented_on" not in base_meta:
                base_meta["act_assented_on"] = value[:40]
            elif ("type" in label or "category" in label) and "act_type" not in base_meta:
                base_meta["act_type"] = value[:80]
            if "act_year" not in base_meta and YEAR_RE.search(value):
                base_meta["act_year"] = YEAR_RE.search(value).group(0)  # type: ignore[union-attr]
        for a in soup.find_all("a", href=True):
            route_meta = dict(base_meta)
            route_meta["detail_fetch"] = "detail_documents"
            route_meta["discovery_channel"] = "punjablaws-detail-file-link"
            self._capture_candidate(
                raw=a.get("href", ""),
                hint=a.get_text(" ", strip=True)[:240] or route_meta.get("act_title", "")[:240],
                base_url=base_url,
                docs=docs,
                listings={},
                route_meta=route_meta,
            )

    def _collect_listing_links(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: Dict[str, Dict[str, Any]],
        inherited_meta: Dict[str, Any],
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        for a in soup.find_all("a", href=True):
            self._capture_candidate(
                raw=a.get("href", ""),
                hint=a.get_text(" ", strip=True)[:240],
                base_url=base_url,
                docs=docs,
                listings=listings,
                route_meta=inherited_meta,
            )

    def _capture_candidate(  # type: ignore[override]
        self,
        *,
        raw: str,
        hint: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: Dict[str, Dict[str, Any]],
        route_meta: Dict[str, Any],
    ) -> None:
        normalized = normalize_punjab_public_url(raw, base_url=base_url)
        if not normalized:
            return
        try:
            safe = check_url_policy(
                normalized,
                self.source.allow_list or [],
                document_cdn_hosts=self.source.document_cdn_hosts or [],
                allow_private_for_tests=_tests_allow_private(),
            )
        except URLPolicyError:
            self.stats["rejected_urls"] += 1
            return

        kind = _classify_punjab_discovered_url(safe, hint_text=hint)
        if kind == "document":
            path = (urlsplit(safe).path or "").lower()
            host = (urlsplit(safe).hostname or "").lower()
            host_is_punjablaws_like = host in PUNJABLAWS_HOST_ALIASES or host in ("127.0.0.1", "localhost")
            ext = path.rsplit(".", 1)[-1] if "." in path else ""
            meta = {
                "discovery_hint": hint[:240],
                "pdf_endpoint_kind": (
                    "uploads-acts-file"
                    if path.startswith("/uploads/acts/")
                    else ("punjablaws-download-file" if host_is_punjablaws_like and "/download" in path else "direct-file")
                ),
                **route_meta,
            }
            if ext and "document_format" not in meta:
                meta["document_format"] = ext
            if ext == "pdf":
                meta["expect_pdf"] = True
            existing = docs.get(safe)
            if existing is None:
                docs[safe] = meta
            else:
                for key, value in meta.items():
                    if key not in existing and value not in ("", None):
                        existing[key] = value
        elif kind == "listing" and safe != base_url:
            if safe not in listings:
                listings[safe] = dict(route_meta)
                listings[safe].setdefault("detail_url", safe)
            else:
                for key, value in route_meta.items():
                    if key not in listings[safe] and value not in ("", None):
                        listings[safe][key] = value


class SindhAssemblyPipeline(BalochistanAssemblyPipeline):
    """Source-specific extraction for PAS + SindhLaws listing/detail document routing."""

    async def handle_listing(self, res, fr: CrawlFrontier) -> None:  # type: ignore[override]
        depth = int(fr.query_json.get("depth", 0))
        max_depth = int(self.source.crawl_max_depth or 2)
        target_kind = fr.query_json.get("target_kind", "statute")
        docs: Dict[str, Dict[str, Any]] = {}
        listings: Dict[str, Dict[str, Any]] = {}
        inherited_meta = dict(fr.query_json.get("meta") or {})

        if self._is_detail_listing(res.final_url):
            self._collect_detail_document_links(
                html_text=res.text,
                base_url=res.final_url,
                docs=docs,
                inherited_meta=inherited_meta,
            )
        else:
            self._collect_structured_listing_rows(
                html_text=res.text,
                base_url=res.final_url,
                listings=listings,
            )
            self._collect_sindhlaws_listing_links(
                html_text=res.text,
                base_url=res.final_url,
                listings=listings,
                inherited_meta=inherited_meta,
            )

        added = await self._enqueue_documents_with_meta(docs, listing_url=res.final_url, default_target_kind=target_kind)
        self.stats["discovered"] += added

        if depth >= max_depth:
            return
        for nurl, nmeta in listings.items():
            key = f"listing:{nurl}"
            exists = (
                await self.db.execute(
                    select(CrawlFrontier).where(
                        CrawlFrontier.source_name == self.source.source_name,
                        CrawlFrontier.tier == 0,
                        CrawlFrontier.query_key == key,
                    )
                )
            ).scalars().first()
            if exists is None:
                route = {"listing": res.final_url}
                for key_name in (
                    "listing_fetch",
                    "detail_fetch",
                    "discovery_channel",
                    "result_index",
                    "source_section",
                    "act_year",
                    "act_no",
                    "act_title",
                    "act_passed_on",
                    "act_assented_on",
                    "act_type",
                    "detail_url",
                    "detail_title",
                ):
                    if key_name in nmeta:
                        route[key_name] = nmeta[key_name]
                next_target_kind = nmeta.get("target_kind", target_kind)
                self.db.add(
                    CrawlFrontier(
                        source_name=self.source.source_name,
                        tier=0,
                        query_key=key,
                        query_json={
                            "kind": "listing",
                            "url": nurl,
                            "target_kind": next_target_kind,
                            "depth": depth + 1,
                            "route": route,
                            "meta": nmeta,
                        },
                        cursor_json={},
                        priority=40,
                    )
                )
                self.stats["discovered"] += 1
        await self.db.flush()

    @staticmethod
    def _is_detail_listing(url: str) -> bool:
        parts = urlsplit(url)
        path = (parts.path or "").lower()
        host = (parts.hostname or "").lower()
        host_is_local_fixture = host in ("127.0.0.1", "localhost")
        if PAS_DETAIL_PATH_RE.search(path):
            return True
        if (host in SINDHLAWS_HOST_ALIASES or host_is_local_fixture) and SINDHLAWS_DETAIL_PATH_RE.search(path):
            return True
        return False

    @staticmethod
    def _sindhlaws_source_section(url: str) -> Optional[str]:
        parts = urlsplit(url)
        query = {key.lower(): value for key, value in parse_qs(parts.query or "").items()}
        section_raw = ""
        if query.get("pg"):
            section_raw = str(query["pg"][0])
        elif query.get("x"):
            section_raw = str(query["x"][0])
        section = section_raw.strip().lower()
        if section == "act":
            return "acts"
        if section == "ordinance":
            return "ordinances"
        if section in ("bill", "bills"):
            return "bills"
        return None

    @staticmethod
    def _sindhlaws_target_kind(source_section: Optional[str]) -> str:
        if str(source_section or "").strip().lower() in ("ordinances", "bills"):
            return "instrument"
        return "statute"

    @staticmethod
    def _sindhlaws_year(url: str) -> Optional[str]:
        parts = urlsplit(url)
        query = {key.lower(): value for key, value in parse_qs(parts.query or "").items()}
        year = (query.get("year") or [""])[0]
        year_text = str(year).strip()
        if YEAR_RE.fullmatch(year_text):
            return year_text
        return None

    @staticmethod
    def _is_sindhlaws_listing(url: str) -> bool:
        parts = urlsplit(url)
        path = (parts.path or "/").lower()
        host = (parts.hostname or "").lower()
        host_is_local_fixture = host in ("127.0.0.1", "localhost")
        if host not in SINDHLAWS_HOST_ALIASES and not host_is_local_fixture:
            return False
        if not SINDHLAWS_LISTING_PATH_RE.search(path):
            return False
        if path == "/gazette.aspx":
            query = (parts.query or "").lower()
            return any(f"pg={section}" in query for section in ("act", "ordinance", "bills")) or host_is_local_fixture
        return True

    def _collect_structured_listing_rows(self, *, html_text: str, base_url: str, listings: Dict[str, Dict[str, Any]]) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        row_index = 0
        for table in soup.select("table"):
            headers = [th.get_text(" ", strip=True).lower() for th in table.select("thead th")]
            if not headers:
                headers = [th.get_text(" ", strip=True).lower() for th in table.select("tr th")]
            if not self._looks_like_listing_table(headers):
                continue
            rows = table.select("tbody tr") or table.select("tr")
            for tr in rows:
                cells = tr.find_all("td")
                if len(cells) < 2:
                    continue
                link = self._detail_link(cells, headers)
                if link is None:
                    continue
                row_index += 1
                title = link.get("title", "").strip() or link.get_text(" ", strip=True) or cells[1].get_text(" ", strip=True)
                row_meta: Dict[str, Any] = {
                    "listing_fetch": "acts_table",
                    "discovery_channel": "acts-table-row",
                    "source_section": "acts",
                    "target_kind": "statute",
                    "result_index": row_index,
                }
                self._add_listing_row_provenance(row_meta=row_meta, cells=cells, headers=headers, title=title)
                self._capture_candidate(raw=link.get("href", ""), hint=title[:240], base_url=base_url, docs={}, listings=listings, route_meta=row_meta)

    @staticmethod
    def _looks_like_listing_table(headers: List[str]) -> bool:
        if not headers:
            return False
        has_no = any("act no" in h for h in headers)
        has_title = any("title" in h for h in headers)
        return has_no and has_title

    @staticmethod
    def _detail_link(cells: List[Any], headers: List[str]):
        title_idx = next((i for i, h in enumerate(headers) if "title" in h), None)
        if title_idx is not None and title_idx < len(cells):
            link = cells[title_idx].find("a", href=True)
            if link is not None:
                return link
        for cell in cells:
            link = cell.find("a", href=True)
            if link is not None:
                return link
        return None

    def _add_listing_row_provenance(self, *, row_meta: Dict[str, Any], cells: List[Any], headers: List[str], title: str) -> None:
        act_no_idx = next((i for i, h in enumerate(headers) if "act no" in h), None)
        if act_no_idx is not None and act_no_idx < len(cells):
            act_no = cells[act_no_idx].get_text(" ", strip=True)[:80]
            if act_no:
                row_meta["act_no"] = act_no

        if title:
            row_meta["act_title"] = title[:280]

        for idx, header in enumerate(headers):
            if idx >= len(cells):
                continue
            value = cells[idx].get_text(" ", strip=True)
            if not value:
                continue
            if "passing" in header:
                row_meta["act_passed_on"] = value[:40]
                if "act_year" not in row_meta and YEAR_RE.search(value):
                    row_meta["act_year"] = YEAR_RE.search(value).group(0)  # type: ignore[union-attr]
            elif "governor" in header or "assent" in header:
                row_meta["act_assented_on"] = value[:40]
                if "act_year" not in row_meta and YEAR_RE.search(value):
                    row_meta["act_year"] = YEAR_RE.search(value).group(0)  # type: ignore[union-attr]
            elif "year" in header and YEAR_RE.search(value):
                row_meta["act_year"] = YEAR_RE.search(value).group(0)  # type: ignore[union-attr]

        if "act_year" not in row_meta:
            year_match = YEAR_RE.search(title or "") or YEAR_RE.search(row_meta.get("act_no", ""))
            if year_match:
                row_meta["act_year"] = year_match.group(0)

    def _collect_detail_document_links(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        inherited_meta: Dict[str, Any],
    ) -> None:
        if self._is_sindhlaws_detail_listing(base_url):
            self._collect_sindhlaws_detail_document_links(
                html_text=html_text,
                base_url=base_url,
                docs=docs,
                inherited_meta=inherited_meta,
            )
            return
        soup = BeautifulSoup(html_text or "", "html.parser")
        detail_title = (soup.select_one("h2.act-title") or soup.select_one("h1") or soup.select_one("title"))
        detail_title_text = detail_title.get_text(" ", strip=True)[:280] if detail_title else ""
        base_meta = dict(inherited_meta)
        base_meta.setdefault("detail_url", base_url)
        if detail_title_text and not base_meta.get("act_title"):
            base_meta["act_title"] = detail_title_text
        if detail_title_text:
            base_meta["detail_title"] = detail_title_text

        for p in soup.select("p"):
            label_node = p.find("label")
            if label_node is None:
                continue
            label = label_node.get_text(" ", strip=True).lower()
            value = p.get_text(" ", strip=True).replace(label_node.get_text(" ", strip=True), "", 1).strip(" :")
            if not value:
                continue
            if "act no" in label and "act_no" not in base_meta:
                base_meta["act_no"] = value[:80]
            elif ("passed on" in label or "date of passing" in label) and "act_passed_on" not in base_meta:
                base_meta["act_passed_on"] = value[:40]
            elif ("assent" in label or "enforcement" in label) and "act_assented_on" not in base_meta:
                base_meta["act_assented_on"] = value[:40]
            elif "subject" in label and "act_type" not in base_meta:
                base_meta["act_type"] = value[:80]
            if "act_year" not in base_meta and YEAR_RE.search(value):
                base_meta["act_year"] = YEAR_RE.search(value).group(0)  # type: ignore[union-attr]

        for a in soup.find_all("a", href=True):
            hint = a.get_text(" ", strip=True)[:240]
            route_meta = dict(base_meta)
            route_meta["detail_fetch"] = "act_files_section"
            route_meta["discovery_channel"] = "act-detail-file-link"
            self._capture_candidate(raw=a.get("href", ""), hint=hint, base_url=base_url, docs=docs, listings={}, route_meta=route_meta)

    @staticmethod
    def _is_sindhlaws_detail_listing(url: str) -> bool:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        host_is_local_fixture = host in ("127.0.0.1", "localhost")
        if host not in SINDHLAWS_HOST_ALIASES and not host_is_local_fixture:
            return False
        return SINDHLAWS_DETAIL_PATH_RE.search((parts.path or "").lower()) is not None

    def _collect_sindhlaws_listing_links(
        self,
        *,
        html_text: str,
        base_url: str,
        listings: Dict[str, Dict[str, Any]],
        inherited_meta: Dict[str, Any],
    ) -> None:
        if not self._is_sindhlaws_listing(base_url):
            return
        soup = BeautifulSoup(html_text or "", "html.parser")
        row_index = 0
        for anchor in soup.find_all("a", href=True):
            href = anchor.get("href", "")
            if not href:
                continue
            row_index += 1
            joined = urljoin(base_url, href)
            section = self._sindhlaws_source_section(joined) or self._sindhlaws_source_section(base_url) or inherited_meta.get("source_section") or "acts"
            target_kind = self._sindhlaws_target_kind(section)
            route_meta: Dict[str, Any] = {
                **inherited_meta,
                "listing_fetch": "gazette_navigation",
                "discovery_channel": "gazette-navigation-link",
                "result_index": row_index,
                "source_section": section,
                "target_kind": target_kind,
            }
            year = self._sindhlaws_year(joined)
            if year:
                route_meta["act_year"] = year
                route_meta["discovery_channel"] = "gazette-year-link"
                route_meta["listing_fetch"] = "gazette_year_grid"
            if self._is_sindhlaws_detail_listing(joined):
                route_meta.setdefault("detail_url", joined)
            self._capture_candidate(
                raw=href,
                hint=anchor.get_text(" ", strip=True)[:240],
                base_url=base_url,
                docs={},
                listings=listings,
                route_meta=route_meta,
            )

    def _collect_sindhlaws_detail_document_links(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        inherited_meta: Dict[str, Any],
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        detail_title_node = soup.select_one("h1") or soup.select_one("h2") or soup.select_one("title")
        detail_title = detail_title_node.get_text(" ", strip=True)[:280] if detail_title_node else ""
        section = self._sindhlaws_source_section(base_url) or inherited_meta.get("source_section") or "acts"
        detail_year = self._sindhlaws_year(base_url)
        base_meta = dict(inherited_meta)
        base_meta["detail_url"] = base_url
        base_meta.setdefault("source_section", section)
        base_meta.setdefault("target_kind", self._sindhlaws_target_kind(section))
        if detail_year and "act_year" not in base_meta:
            base_meta["act_year"] = detail_year
        if detail_title:
            base_meta.setdefault("detail_title", detail_title)

        row_index = 0
        for row in soup.select("table tr"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            link = row.find("a", href=True)
            if link is None:
                continue
            row_index += 1
            route_meta = dict(base_meta)
            route_meta["detail_fetch"] = "gazette_detail_table"
            route_meta["discovery_channel"] = "gazette-detail-row"
            route_meta["result_index"] = row_index

            serial = cells[0].get_text(" ", strip=True)
            if serial and "act_no" not in route_meta:
                route_meta["act_no"] = serial[:80]
            title = cells[1].get_text(" ", strip=True)
            if title:
                route_meta["act_title"] = title[:280]
                if "act_year" not in route_meta and YEAR_RE.search(title):
                    route_meta["act_year"] = YEAR_RE.search(title).group(0)  # type: ignore[union-attr]
            if len(cells) > 3:
                publication_date = cells[3].get_text(" ", strip=True)
                if publication_date:
                    route_meta.setdefault("act_passed_on", publication_date[:40])
                    if "act_year" not in route_meta and YEAR_RE.search(publication_date):
                        route_meta["act_year"] = YEAR_RE.search(publication_date).group(0)  # type: ignore[union-attr]
            if "act_type" not in route_meta:
                route_meta["act_type"] = "act" if section == "acts" else section.rstrip("s")

            self._capture_candidate(
                raw=link.get("href", ""),
                hint=link.get_text(" ", strip=True)[:240] or title[:240],
                base_url=base_url,
                docs=docs,
                listings={},
                route_meta=route_meta,
            )

    def _capture_candidate(
        self,
        *,
        raw: str,
        hint: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: Dict[str, Dict[str, Any]],
        route_meta: Dict[str, Any],
    ) -> None:
        normalized = normalize_pas_public_url(raw, base_url=base_url)
        if not normalized:
            return
        try:
            safe = check_url_policy(
                normalized,
                self.source.allow_list or [],
                document_cdn_hosts=self.source.document_cdn_hosts or [],
                allow_private_for_tests=_tests_allow_private(),
            )
        except URLPolicyError:
            self.stats["rejected_urls"] += 1
            return

        kind = _classify_pas_discovered_url(safe)
        if kind == "document":
            path = (urlsplit(safe).path or "").lower()
            ext = path.rsplit(".", 1)[-1] if "." in path else ""
            if path.startswith("/uploads/acts/"):
                pdf_endpoint_kind = "uploads-acts-file"
            elif path.startswith("/setup/publications/"):
                pdf_endpoint_kind = "setup-publications-file"
            elif path.startswith("/setup/library/"):
                pdf_endpoint_kind = "setup-library-file"
            else:
                pdf_endpoint_kind = "direct-file"
            meta = {
                "discovery_hint": hint[:240],
                "pdf_endpoint_kind": pdf_endpoint_kind,
                **route_meta,
            }
            if ext and "document_format" not in meta:
                meta["document_format"] = ext
            if ext == "pdf":
                meta["expect_pdf"] = True
            existing = docs.get(safe)
            if existing is None:
                docs[safe] = meta
            else:
                for key, value in meta.items():
                    if key not in existing and value not in ("", None):
                        existing[key] = value
        elif kind == "listing" and safe != base_url:
            if safe not in listings:
                listings[safe] = dict(route_meta)
                listings[safe].setdefault("detail_url", safe)
            else:
                for key, value in route_meta.items():
                    if key not in listings[safe] and value not in ("", None):
                        listings[safe][key] = value


class AJKAssemblyPipeline(BalochistanAssemblyPipeline):
    """Source-specific extraction for AJK Law Department public acts/ordinances listings."""

    async def handle_listing(self, res, fr: CrawlFrontier) -> None:  # type: ignore[override]
        depth = int(fr.query_json.get("depth", 0))
        max_depth = int(self.source.crawl_max_depth or 2)
        target_kind = str(fr.query_json.get("target_kind", "statute"))
        docs: Dict[str, Dict[str, Any]] = {}
        listings: Dict[str, Dict[str, Any]] = {}
        inherited_meta = dict(fr.query_json.get("meta") or {})
        source_section = inherited_meta.get("source_section") or self._ajk_source_section(res.final_url) or "acts"

        base_meta = dict(inherited_meta)
        base_meta.setdefault("source_section", source_section)
        base_meta.setdefault(
            "target_kind",
            self._ajk_target_kind(
                source_section=str(base_meta.get("source_section") or source_section),
                title=str(base_meta.get("act_title") or ""),
                fallback=target_kind,
            ),
        )

        if self._is_ajk_detail_listing(res.final_url):
            self._collect_ajk_detail_document_links(
                html_text=res.text,
                base_url=res.final_url,
                docs=docs,
                inherited_meta=base_meta,
            )
        else:
            self._collect_ajk_listing_rows(
                html_text=res.text,
                base_url=res.final_url,
                docs=docs,
                listings=listings,
                inherited_meta=base_meta,
            )

        navigation_meta = {
            "listing_fetch": "navigation_links",
            "discovery_channel": "navigation-link",
            **base_meta,
        }
        for key_name in (
            "act_title",
            "detail_title",
            "act_no",
            "act_year",
            "act_passed_on",
            "act_type",
            "detail_url",
            "target_kind",
        ):
            navigation_meta.pop(key_name, None)

        self._collect_listing_links(
            html_text=res.text,
            base_url=res.final_url,
            docs=docs,
            listings=listings,
            inherited_meta=navigation_meta,
        )

        added = await self._enqueue_documents_with_meta(docs, listing_url=res.final_url, default_target_kind=target_kind)
        self.stats["discovered"] += added

        if depth >= max_depth:
            return
        for nurl, nmeta in listings.items():
            next_target_kind = str(nmeta.get("target_kind") or target_kind)
            key = f"listing:{nurl}"
            exists = (
                await self.db.execute(
                    select(CrawlFrontier).where(
                        CrawlFrontier.source_name == self.source.source_name,
                        CrawlFrontier.tier == 0,
                        CrawlFrontier.query_key == key,
                    )
                )
            ).scalars().first()
            if exists is None:
                route = {"listing": res.final_url}
                for key_name in (
                    "listing_fetch",
                    "detail_fetch",
                    "discovery_channel",
                    "result_index",
                    "source_section",
                    "act_year",
                    "act_no",
                    "act_title",
                    "act_passed_on",
                    "act_type",
                    "detail_url",
                    "detail_title",
                ):
                    if key_name in nmeta:
                        route[key_name] = nmeta[key_name]
                self.db.add(
                    CrawlFrontier(
                        source_name=self.source.source_name,
                        tier=0,
                        query_key=key,
                        query_json={
                            "kind": "listing",
                            "url": nurl,
                            "target_kind": next_target_kind,
                            "depth": depth + 1,
                            "route": route,
                            "meta": nmeta,
                        },
                        cursor_json={},
                        priority=40,
                    )
                )
                self.stats["discovered"] += 1
        await self.db.flush()

    @staticmethod
    def _ajk_source_section(url: str) -> Optional[str]:
        path = (urlsplit(url).path or "/").lower()
        if path.startswith("/ordinance"):
            return "ordinance"
        if path.startswith("/download"):
            return "download"
        if path.startswith("/revised-volume"):
            return "revised_volume"
        if path.startswith("/acts") or path == "/":
            return "acts"
        return None

    @staticmethod
    def _is_ajk_detail_listing(url: str) -> bool:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if host not in AJK_LAW_HOST_ALIASES and host not in ("127.0.0.1", "localhost"):
            return False
        path = (parts.path or "/").lower()
        if AJK_LAW_LISTING_PATH_RE.search(path):
            return False
        return AJK_LAW_DETAIL_PATH_RE.search(path) is not None

    @staticmethod
    def _ajk_target_kind(*, source_section: str, title: str, fallback: str) -> str:
        default_kind = fallback if fallback in ("statute", "instrument") else "statute"
        section = (source_section or "").lower()
        lowered_title = (title or "").lower()
        if section in ("ordinance", "download"):
            return "instrument"
        if re.search(r"\b(act|acts|law|code|statute)\b", lowered_title):
            return "statute"
        if re.search(r"\b(ordinance|rules?|regulations?|notification|order|by-law|bye-law)\b", lowered_title):
            return "instrument"
        if section == "acts":
            return "statute"
        return default_kind

    @staticmethod
    def _looks_like_ajk_table(headers: List[str]) -> bool:
        if not headers:
            return False
        has_title = any("title" in h or "act title" in h or "name" in h for h in headers)
        has_lawish = any("act" in h or "ordinance" in h or "law" in h or "year" in h or "no" in h for h in headers)
        return has_title and has_lawish

    def _collect_ajk_listing_rows(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: Dict[str, Dict[str, Any]],
        inherited_meta: Dict[str, Any],
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        row_index = 0
        base_section = self._ajk_source_section(base_url) or str(inherited_meta.get("source_section") or "acts")
        listing_fetch = "revised_volume_table" if base_section == "revised_volume" else "acts_table"

        for table in soup.select("table"):
            headers = [th.get_text(" ", strip=True).lower() for th in table.select("thead th")]
            if not headers:
                headers = [th.get_text(" ", strip=True).lower() for th in table.select("tr th")]
            if not self._looks_like_ajk_table(headers):
                continue
            rows = table.select("tbody tr") or table.select("tr")
            for tr in rows:
                cells = tr.find_all("td")
                if len(cells) < 2:
                    continue
                link = tr.find("a", href=True)
                if link is None:
                    continue
                row_index += 1
                title = link.get_text(" ", strip=True) or cells[min(len(cells) - 1, 1)].get_text(" ", strip=True)
                row_meta: Dict[str, Any] = {
                    **inherited_meta,
                    "listing_fetch": listing_fetch,
                    "discovery_channel": "ajk-table-row",
                    "result_index": row_index,
                    "source_section": base_section,
                }

                if title:
                    row_meta["act_title"] = title[:280]
                for idx, header in enumerate(headers):
                    if idx >= len(cells):
                        continue
                    value = cells[idx].get_text(" ", strip=True)
                    if not value:
                        continue
                    if ("act no" in header or "act no." in header or "ordinance no" in header) and "act_no" not in row_meta:
                        row_meta["act_no"] = value[:80]
                    elif "year" in header and "act_year" not in row_meta and YEAR_RE.search(value):
                        row_meta["act_year"] = YEAR_RE.search(value).group(0)  # type: ignore[union-attr]
                    elif ("date" in header or "passed" in header) and "act_passed_on" not in row_meta:
                        row_meta["act_passed_on"] = value[:40]
                    elif ("type" in header or "category" in header) and "act_type" not in row_meta:
                        row_meta["act_type"] = value[:80]

                if "act_type" not in row_meta:
                    if "ordinance" in title.lower():
                        row_meta["act_type"] = "ordinance"
                    elif "rule" in title.lower():
                        row_meta["act_type"] = "rules"
                    elif "act" in title.lower():
                        row_meta["act_type"] = "act"

                row_meta["target_kind"] = self._ajk_target_kind(
                    source_section=str(row_meta.get("source_section") or base_section),
                    title=str(row_meta.get("act_title") or title),
                    fallback=str(row_meta.get("target_kind") or "statute"),
                )
                self._capture_candidate(
                    raw=link.get("href", ""),
                    hint=title[:240],
                    base_url=base_url,
                    docs=docs,
                    listings=listings,
                    route_meta=row_meta,
                )

    def _collect_ajk_detail_document_links(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        inherited_meta: Dict[str, Any],
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        detail_title_node = soup.select_one("h1") or soup.select_one("h2") or soup.select_one("title")
        detail_title = detail_title_node.get_text(" ", strip=True)[:280] if detail_title_node else ""
        section = self._ajk_source_section(base_url) or str(inherited_meta.get("source_section") or "acts")
        base_meta = dict(inherited_meta)
        base_meta["detail_url"] = base_url
        base_meta.setdefault("source_section", section)
        if detail_title:
            base_meta["detail_title"] = detail_title
            base_meta["act_title"] = detail_title
        base_meta["target_kind"] = self._ajk_target_kind(
            source_section=str(base_meta.get("source_section") or section),
            title=str(base_meta.get("act_title") or detail_title),
            fallback=str(base_meta.get("target_kind") or "statute"),
        )

        for a in soup.find_all("a", href=True):
            hint = a.get_text(" ", strip=True)[:240] or base_meta.get("act_title", "")[:240]
            route_meta = dict(base_meta)
            route_meta["detail_fetch"] = "detail_file_link"
            route_meta["discovery_channel"] = "detail-file-link"
            self._capture_candidate(
                raw=a.get("href", ""),
                hint=hint,
                base_url=base_url,
                docs=docs,
                listings={},
                route_meta=route_meta,
            )

    def _collect_listing_links(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: Dict[str, Dict[str, Any]],
        inherited_meta: Dict[str, Any],
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        for a in soup.find_all("a", href=True):
            self._capture_candidate(
                raw=a.get("href", ""),
                hint=a.get_text(" ", strip=True)[:240],
                base_url=base_url,
                docs=docs,
                listings=listings,
                route_meta=dict(inherited_meta),
            )

    def _capture_candidate(
        self,
        *,
        raw: str,
        hint: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: Dict[str, Dict[str, Any]],
        route_meta: Dict[str, Any],
    ) -> None:
        normalized = normalize_ajk_public_url(raw, base_url=base_url)
        if not normalized:
            return
        try:
            safe = check_url_policy(
                normalized,
                self.source.allow_list or [],
                document_cdn_hosts=self.source.document_cdn_hosts or [],
                allow_private_for_tests=_tests_allow_private(),
            )
        except URLPolicyError:
            self.stats["rejected_urls"] += 1
            return

        kind = _classify_ajk_discovered_url(safe, hint_text=hint)
        if kind == "document":
            path = (urlsplit(safe).path or "").lower()
            ext = path.rsplit(".", 1)[-1] if "." in path else ""
            section = str(route_meta.get("source_section") or self._ajk_source_section(base_url) or "acts")
            meta = {
                "discovery_hint": hint[:240],
                "pdf_endpoint_kind": "wp-content-uploads-file" if path.startswith("/wp-content/uploads/") else "direct-file",
                **route_meta,
            }
            if ext and "document_format" not in meta:
                meta["document_format"] = ext
            if ext == "pdf":
                meta["expect_pdf"] = True
            meta["target_kind"] = self._ajk_target_kind(
                source_section=section,
                title=str(meta.get("act_title") or hint),
                fallback=str(meta.get("target_kind") or "statute"),
            )
            existing = docs.get(safe)
            if existing is None:
                docs[safe] = meta
            else:
                for key, value in meta.items():
                    if key not in existing and value not in ("", None):
                        existing[key] = value
        elif kind == "listing" and safe != base_url:
            listing_meta = dict(route_meta)
            section = self._ajk_source_section(safe) or self._ajk_source_section(base_url) or str(listing_meta.get("source_section") or "acts")
            listing_meta["source_section"] = section
            listing_meta["target_kind"] = self._ajk_target_kind(
                source_section=str(listing_meta.get("source_section") or section),
                title=str(hint or listing_meta.get("act_title") or ""),
                fallback=str(listing_meta.get("target_kind") or "statute"),
            )
            if self._is_ajk_detail_listing(safe):
                listing_meta.setdefault("detail_url", safe)
            if safe not in listings:
                listings[safe] = listing_meta
            else:
                for key, value in listing_meta.items():
                    if key not in listings[safe] and value not in ("", None):
                        listings[safe][key] = value


class KPAssemblyPipeline(BalochistanAssemblyPipeline):
    """Source-specific extraction for PAKP + KPCode statutes/rules detail-page document routing."""

    async def handle_listing(self, res, fr: CrawlFrontier) -> None:  # type: ignore[override]
        depth = int(fr.query_json.get("depth", 0))
        max_depth = int(self.source.crawl_max_depth or 2)
        target_kind = fr.query_json.get("target_kind", "statute")
        docs: Dict[str, Dict[str, Any]] = {}
        listings: Dict[str, Dict[str, Any]] = {}
        inherited_meta = dict(fr.query_json.get("meta") or {})

        if self._is_kpcode_url(res.final_url):
            if self._is_detail_listing(res.final_url):
                self._collect_kpcode_detail_document_links(
                    html_text=res.text,
                    base_url=res.final_url,
                    docs=docs,
                    inherited_meta=inherited_meta,
                )
            else:
                self._collect_kpcode_listing_rows(
                    html_text=res.text,
                    base_url=res.final_url,
                    docs=docs,
                    listings=listings,
                )
        else:
            if self._is_detail_listing(res.final_url):
                self._collect_detail_document_links(
                    html_text=res.text,
                    base_url=res.final_url,
                    docs=docs,
                    inherited_meta=inherited_meta,
                )
            else:
                self._collect_structured_listing_rows(
                    html_text=res.text,
                    base_url=res.final_url,
                    docs=docs,
                    listings=listings,
                )

        added = await self._enqueue_documents_with_meta(docs, listing_url=res.final_url, default_target_kind=target_kind)
        self.stats["discovered"] += added

        if depth >= max_depth:
            return
        for nurl, nmeta in listings.items():
            key = f"listing:{nurl}"
            exists = (
                await self.db.execute(
                    select(CrawlFrontier).where(
                        CrawlFrontier.source_name == self.source.source_name,
                        CrawlFrontier.tier == 0,
                        CrawlFrontier.query_key == key,
                    )
                )
            ).scalars().first()
            if exists is None:
                route = {"listing": res.final_url}
                for key_name in (
                    "listing_fetch",
                    "result_index",
                    "source_section",
                    "act_year",
                    "act_no",
                    "act_title",
                    "act_passed_on",
                    "act_assented_on",
                    "act_type",
                    "detail_url",
                    "detail_category",
                    "detail_department",
                    "detail_specific_category",
                ):
                    if key_name in nmeta:
                        route[key_name] = nmeta[key_name]
                next_target_kind = nmeta.get("target_kind", target_kind)
                self.db.add(
                    CrawlFrontier(
                        source_name=self.source.source_name,
                        tier=0,
                        query_key=key,
                        query_json={
                            "kind": "listing",
                            "url": nurl,
                            "target_kind": next_target_kind,
                            "depth": depth + 1,
                            "route": route,
                            "meta": nmeta,
                        },
                        cursor_json={},
                        priority=40,
                    )
                )
                self.stats["discovered"] += 1
        await self.db.flush()

    @staticmethod
    def _is_detail_listing(url: str) -> bool:
        path = (urlsplit(url).path or "").lower()
        return PAKP_DETAIL_PATH_RE.search(path) is not None or KPCODE_LAW_DETAIL_RE.search(path) is not None or KPCODE_RULE_DETAIL_RE.search(path) is not None

    @staticmethod
    def _is_kpcode_url(url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        if host in KPCODE_HOST_ALIASES:
            return True
        path = (urlsplit(url).path or "").lower()
        return path.startswith("/homepage/") or path.startswith("/uploads/")

    def _collect_structured_listing_rows(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: Dict[str, Dict[str, Any]],
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        row_index = 0
        for table in soup.select("table"):
            headers = [th.get_text(" ", strip=True).lower() for th in table.select("thead th")]
            if not headers:
                headers = [th.get_text(" ", strip=True).lower() for th in table.select("tr th")]
            if not self._looks_like_listing_table(headers):
                continue
            rows = table.select("tbody tr") or table.select("tr")
            for tr in rows:
                cells = tr.find_all("td")
                if len(cells) < 3:
                    continue
                links = tr.find_all("a", href=True)
                if not links:
                    continue
                row_index += 1
                title_link = self._title_link(cells, headers)
                title = (
                    title_link.get_text(" ", strip=True)
                    if title_link is not None
                    else cells[min(len(cells) - 1, 2)].get_text(" ", strip=True)
                )
                row_meta: Dict[str, Any] = {
                    "listing_fetch": "acts_table",
                    "discovery_channel": "acts-table-row",
                    "source_section": "acts",
                    "result_index": row_index,
                }
                self._add_listing_row_provenance(row_meta=row_meta, cells=cells, headers=headers, title=title)
                for link in links:
                    hint = link.get_text(" ", strip=True)[:240]
                    self._capture_candidate(
                        raw=link.get("href", ""),
                        hint=hint,
                        base_url=base_url,
                        docs=docs,
                        listings=listings,
                        route_meta=row_meta,
                    )

    @staticmethod
    def _looks_like_listing_table(headers: List[str]) -> bool:
        if not headers:
            return False
        has_act_no = any("act #" in h or "act no" in h or "act number" in h for h in headers)
        has_title = any("title" in h for h in headers)
        return has_act_no and has_title

    @staticmethod
    def _title_link(cells: List[Any], headers: List[str]):
        title_idx = next((i for i, h in enumerate(headers) if "title" in h), None)
        if title_idx is not None and title_idx < len(cells):
            link = cells[title_idx].find("a", href=True)
            if link is not None:
                return link
        for cell in cells:
            link = cell.find("a", href=True)
            if link is not None:
                return link
        return None

    def _add_listing_row_provenance(self, *, row_meta: Dict[str, Any], cells: List[Any], headers: List[str], title: str) -> None:
        act_no_idx = next((i for i, h in enumerate(headers) if "act #" in h or "act no" in h or "act number" in h), None)
        if act_no_idx is not None and act_no_idx < len(cells):
            act_no = cells[act_no_idx].get_text(" ", strip=True)[:80]
            if act_no:
                row_meta["act_no"] = act_no

        if title:
            row_meta["act_title"] = title[:280]

        for idx, header in enumerate(headers):
            if idx >= len(cells):
                continue
            value = cells[idx].get_text(" ", strip=True)
            if not value:
                continue
            if "passage" in header or "passed" in header:
                row_meta["act_passed_on"] = value[:40]
            elif "enforcement" in header or "assent" in header:
                row_meta["act_assented_on"] = value[:40]
            elif "year" in header and YEAR_RE.search(value):
                row_meta["act_year"] = YEAR_RE.search(value).group(0)  # type: ignore[union-attr]

            if "act_year" not in row_meta and YEAR_RE.search(value):
                row_meta["act_year"] = YEAR_RE.search(value).group(0)  # type: ignore[union-attr]

        if "act_year" not in row_meta:
            year_match = YEAR_RE.search(title or "") or YEAR_RE.search(row_meta.get("act_no", ""))
            if year_match:
                row_meta["act_year"] = year_match.group(0)

    def _collect_detail_document_links(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        inherited_meta: Dict[str, Any],
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        detail_title_node = soup.select_one(".sinpost-content h1") or soup.select_one("h1") or soup.select_one("title")
        detail_title = detail_title_node.get_text(" ", strip=True)[:280] if detail_title_node else ""
        base_meta = dict(inherited_meta)
        base_meta.setdefault("detail_url", base_url)
        if detail_title and not base_meta.get("act_title"):
            base_meta["act_title"] = detail_title
        if detail_title:
            base_meta["detail_title"] = detail_title

        for row in soup.select(".leg-row"):
            label_node = row.select_one(".act-title")
            value_node = row.select_one(".act-info")
            if label_node is None or value_node is None:
                continue
            label = label_node.get_text(" ", strip=True).strip(": ").lower()
            value = value_node.get_text(" ", strip=True)
            if not value:
                continue
            if "act #" in label and "act_no" not in base_meta:
                base_meta["act_no"] = value[:80]
            elif "passage date" in label and "act_passed_on" not in base_meta:
                base_meta["act_passed_on"] = value[:40]
            elif ("enforcement" in label or "assent" in label) and "act_assented_on" not in base_meta:
                base_meta["act_assented_on"] = value[:40]
            if "act_year" not in base_meta and YEAR_RE.search(value):
                base_meta["act_year"] = YEAR_RE.search(value).group(0)  # type: ignore[union-attr]

            if "document" in label:
                for a in value_node.select("a[href]"):
                    route_meta = dict(base_meta)
                    route_meta["detail_fetch"] = "act_document_row"
                    route_meta["discovery_channel"] = "act-detail-file-link"
                    self._capture_candidate(
                        raw=a.get("href", ""),
                        hint=a.get_text(" ", strip=True)[:240],
                        base_url=base_url,
                        docs=docs,
                        listings={},
                        route_meta=route_meta,
                    )

    @staticmethod
    def _kpcode_source_section_for_url(url: str) -> str:
        path = (urlsplit(url).path or "").lower()
        if "ruledetails" in path or path.startswith("/homepage/rules") or "/search_by_year_rule/" in path:
            return "rules"
        if "lawdetails" in path:
            return "laws"
        if path.startswith("/homepage/search_by_dept/"):
            return "department"
        if path.startswith("/homepage/search_by_category/"):
            return "category"
        if path.startswith("/homepage/search_by_year/"):
            return "year"
        if path.startswith("/homepage/urdu"):
            return "urdu"
        return "acts"

    @staticmethod
    def _kpcode_target_kind(*, source_section: str, title: str, category: str) -> str:
        lowered = " ".join((source_section or "", title or "", category or "")).lower()
        if any(token in lowered for token in ("ordinance", "rules", "rule", "regulation", "notification", "order", "by-law", "bye-law")):
            return "instrument"
        return "statute"

    def _collect_kpcode_listing_rows(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: Dict[str, Dict[str, Any]],
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        row_index = 0
        source_section = self._kpcode_source_section_for_url(base_url)

        for block in soup.select("div.artlist"):
            link = block.select_one("a[href]")
            if link is None:
                continue
            row_index += 1
            title = link.get_text(" ", strip=True)
            row_meta: Dict[str, Any] = {
                "listing_fetch": "kpcode_artlist",
                "discovery_channel": "kpcode-artlist-row",
                "source_section": source_section,
                "result_index": row_index,
            }
            if title:
                row_meta["act_title"] = title[:280]
                year_match = YEAR_RE.search(title)
                if year_match:
                    row_meta["act_year"] = year_match.group(0)
            details_block = block.find_next_sibling("div", class_=re.compile(r"(?i)\bartdets\b"))
            if details_block is not None:
                self._add_kpcode_listing_details(row_meta=row_meta, details_text=details_block.get_text(" ", strip=True))
            row_meta["target_kind"] = self._kpcode_target_kind(
                source_section=source_section,
                title=row_meta.get("act_title", ""),
                category=row_meta.get("detail_category", ""),
            )
            self._capture_candidate(
                raw=link.get("href", ""),
                hint=title[:240],
                base_url=base_url,
                docs=docs,
                listings=listings,
                route_meta=row_meta,
            )

        for anchor in soup.select("a[href]"):
            hint = anchor.get_text(" ", strip=True)
            nav_meta: Dict[str, Any] = {
                "listing_fetch": "kpcode_navigation_links",
                "source_section": source_section,
                "target_kind": self._kpcode_target_kind(source_section=source_section, title=hint, category=""),
            }
            self._capture_candidate(
                raw=anchor.get("href", ""),
                hint=hint[:240],
                base_url=base_url,
                docs=docs,
                listings=listings,
                route_meta=nav_meta,
            )

    @staticmethod
    def _add_kpcode_listing_details(*, row_meta: Dict[str, Any], details_text: str) -> None:
        parts = [p.strip() for p in (details_text or "").split("|") if p.strip()]
        if parts:
            row_meta["detail_department"] = parts[0][:160]
        for part in parts:
            lowered = part.lower()
            if ("act no" in lowered or "ordinance no" in lowered or "rule no" in lowered) and "act_no" not in row_meta:
                row_meta["act_no"] = part[:80]
            if "promulgation date" in lowered:
                _, _, rhs = part.partition(":")
                value = (rhs or part).strip()
                if value:
                    row_meta["act_assented_on"] = value[:40]
            if "year" in lowered and "act_year" not in row_meta:
                year_match = YEAR_RE.search(part)
                if year_match:
                    row_meta["act_year"] = year_match.group(0)

    def _collect_kpcode_detail_document_links(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        inherited_meta: Dict[str, Any],
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        base_meta = dict(inherited_meta)
        base_meta.setdefault("detail_url", base_url)
        detail_section = self._kpcode_source_section_for_url(base_url)
        if detail_section in ("rules", "laws"):
            base_meta["source_section"] = detail_section
        else:
            base_meta.setdefault("source_section", detail_section)
        base_meta.setdefault("detail_fetch", "kpcode_detail_download")

        title = self._extract_kpcode_detail_title(soup)
        if title:
            base_meta.setdefault("act_title", title)
            base_meta.setdefault("detail_title", title)
            if "act_year" not in base_meta:
                year_match = YEAR_RE.search(title)
                if year_match:
                    base_meta["act_year"] = year_match.group(0)

        for row in soup.select("table tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            label = cells[0].get_text(" ", strip=True).lower().strip(": ")
            value = cells[1].get_text(" ", strip=True)
            if not value:
                continue
            if "department" in label and "detail_department" not in base_meta:
                base_meta["detail_department"] = value[:160]
            elif "main category" in label and "detail_category" not in base_meta:
                base_meta["detail_category"] = value[:80]
                base_meta.setdefault("act_type", value[:80].lower())
            elif "specific category" in label and "detail_specific_category" not in base_meta:
                base_meta["detail_specific_category"] = value[:280]
            elif "promulgation" in label and "act_assented_on" not in base_meta:
                base_meta["act_assented_on"] = value[:40]
            elif "year" in label and "act_year" not in base_meta:
                year_match = YEAR_RE.search(value)
                if year_match:
                    base_meta["act_year"] = year_match.group(0)
            if ("act no" in label or "ordinance no" in label or "rule no" in label) and "act_no" not in base_meta:
                base_meta["act_no"] = value[:80]
            if "act_year" not in base_meta:
                year_match = YEAR_RE.search(value)
                if year_match:
                    base_meta["act_year"] = year_match.group(0)

        base_meta["target_kind"] = self._kpcode_target_kind(
            source_section=base_meta.get("source_section", ""),
            title=base_meta.get("act_title", ""),
            category=base_meta.get("detail_category", ""),
        )

        for a in soup.select("a[href]"):
            route_meta = dict(base_meta)
            route_meta["discovery_channel"] = "kpcode-detail-file-link"
            self._capture_candidate(
                raw=a.get("href", ""),
                hint=a.get_text(" ", strip=True)[:240],
                base_url=base_url,
                docs=docs,
                listings={},
                route_meta=route_meta,
            )

    @staticmethod
    def _extract_kpcode_detail_title(soup: BeautifulSoup) -> str:
        candidates: List[str] = []
        for node in soup.select("h1, h2, h3, title"):
            text = node.get_text(" ", strip=True)
            if not text:
                continue
            lowered = text.lower()
            if lowered == "khyber pakhtunkhwa code":
                continue
            candidates.append(text)
        for text in candidates:
            lowered = text.lower()
            if any(token in lowered for token in (" act", "act ", "ordinance", "rules", "rule", "regulation", "notification", "code")):
                return text[:280]
        return candidates[0][:280] if candidates else ""

    def _capture_candidate(
        self,
        *,
        raw: str,
        hint: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: Dict[str, Dict[str, Any]],
        route_meta: Dict[str, Any],
    ) -> None:
        base_host = (urlsplit(base_url).hostname or "").lower()
        if base_host in KPCODE_HOST_ALIASES:
            normalized = normalize_kpcode_public_url(raw, base_url=base_url)
        elif base_host in PAKP_HOST_ALIASES:
            normalized = normalize_pakp_public_url(raw, base_url=base_url)
        else:
            normalized = normalize_pakp_public_url(raw, base_url=base_url) or normalize_kpcode_public_url(raw, base_url=base_url)
        if not normalized:
            return
        try:
            safe = check_url_policy(
                normalized,
                self.source.allow_list or [],
                document_cdn_hosts=self.source.document_cdn_hosts or [],
                allow_private_for_tests=_tests_allow_private(),
            )
        except URLPolicyError:
            self.stats["rejected_urls"] += 1
            return

        host = (urlsplit(safe).hostname or "").lower()
        kind = _classify_kpcode_discovered_url(safe)
        if kind is None:
            kind = _classify_pakp_discovered_url(safe)
        if kind == "document":
            path = (urlsplit(safe).path or "").lower()
            ext = path.rsplit(".", 1)[-1] if "." in path else ""
            if KPCODE_DOC_RE.search(path):
                pdf_kind = "uploads-file" if path.startswith("/uploads/") else "direct-file"
            else:
                pdf_kind = "wp-content-uploads-file" if path.startswith("/wp-content/uploads/") else "direct-file"
            meta = {
                "discovery_hint": hint[:240],
                "pdf_endpoint_kind": pdf_kind,
                **route_meta,
            }
            if ext and "document_format" not in meta:
                meta["document_format"] = ext
            if ext == "pdf":
                meta["expect_pdf"] = True
            if "target_kind" not in meta:
                meta["target_kind"] = self._kpcode_target_kind(
                    source_section=meta.get("source_section", ""),
                    title=meta.get("act_title", hint),
                    category=meta.get("detail_category", ""),
                )
            existing = docs.get(safe)
            if existing is None:
                docs[safe] = meta
            else:
                for key, value in meta.items():
                    if key not in existing and value not in ("", None):
                        existing[key] = value
        elif kind == "listing" and safe != base_url:
            if safe not in listings:
                listings[safe] = dict(route_meta)
                listings[safe].setdefault("detail_url", safe)
            else:
                for key, value in route_meta.items():
                    if key not in listings[safe] and value not in ("", None):
                        listings[safe][key] = value


class NationalAssemblyPipeline(BalochistanAssemblyPipeline):
    """Source-specific extraction for NA acts/bills tables and direct document links."""

    async def handle_listing(self, res, fr: CrawlFrontier) -> None:  # type: ignore[override]
        depth = int(fr.query_json.get("depth", 0))
        max_depth = int(self.source.crawl_max_depth or 2)
        target_kind = fr.query_json.get("target_kind", "instrument")
        docs: Dict[str, Dict[str, Any]] = {}
        listings: Dict[str, Dict[str, Any]] = {}
        inherited_meta = dict(fr.query_json.get("meta") or {})

        if self._is_detail_listing(res.final_url):
            self._collect_detail_document_links(
                html_text=res.text,
                base_url=res.final_url,
                docs=docs,
                inherited_meta=inherited_meta,
            )
        else:
            self._collect_structured_listing_rows(
                html_text=res.text,
                base_url=res.final_url,
                docs=docs,
                listings=listings,
            )
            self._collect_listing_links(
                html_text=res.text,
                base_url=res.final_url,
                listings=listings,
                inherited_meta={
                    "listing_fetch": "navigation_links",
                    "source_section": self._source_section_for_url(res.final_url),
                },
            )

        added = await self._enqueue_documents_with_meta(docs, listing_url=res.final_url, default_target_kind=target_kind)
        self.stats["discovered"] += added

        if depth >= max_depth:
            return
        for nurl, nmeta in listings.items():
            key = f"listing:{nurl}"
            exists = (
                await self.db.execute(
                    select(CrawlFrontier).where(
                        CrawlFrontier.source_name == self.source.source_name,
                        CrawlFrontier.tier == 0,
                        CrawlFrontier.query_key == key,
                    )
                )
            ).scalars().first()
            if exists is None:
                route = {"listing": res.final_url}
                for key_name in (
                    "listing_fetch",
                    "result_index",
                    "source_section",
                    "act_year",
                    "act_no",
                    "act_title",
                    "act_passed_on",
                    "act_assented_on",
                    "act_type",
                    "detail_url",
                ):
                    if key_name in nmeta:
                        route[key_name] = nmeta[key_name]
                self.db.add(
                    CrawlFrontier(
                        source_name=self.source.source_name,
                        tier=0,
                        query_key=key,
                        query_json={
                            "kind": "listing",
                            "url": nurl,
                            "target_kind": target_kind,
                            "depth": depth + 1,
                            "route": route,
                            "meta": nmeta,
                        },
                        cursor_json={},
                        priority=40,
                    )
                )
                self.stats["discovered"] += 1
        await self.db.flush()

    @staticmethod
    def _is_detail_listing(url: str) -> bool:
        return _is_na_detail_discovered_url(url)

    def _collect_structured_listing_rows(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: Dict[str, Dict[str, Any]],
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        row_index = 0
        for table in soup.select("table"):
            headers = [th.get_text(" ", strip=True).lower() for th in table.select("thead th")]
            if not headers:
                headers = [th.get_text(" ", strip=True).lower() for th in table.select("tr th")]
            if not self._looks_like_legislation_table(headers):
                continue
            rows = table.select("tbody tr") or table.select("tr")
            for tr in rows:
                cells = tr.find_all("td")
                if len(cells) < 2:
                    continue
                links = tr.find_all("a", href=True)
                if not links:
                    continue

                row_index += 1
                row_meta: Dict[str, Any] = {
                    "listing_fetch": "legislation_table",
                    "discovery_channel": "legislation-table-row",
                    "source_section": self._source_section_for_url(base_url),
                    "result_index": row_index,
                }
                self._add_row_provenance(row_meta=row_meta, cells=cells, headers=headers)
                for link in links:
                    hint = link.get_text(" ", strip=True)[:240]
                    self._capture_candidate(
                        raw=link.get("href", ""),
                        hint=hint,
                        base_url=base_url,
                        docs=docs,
                        listings=listings,
                        route_meta=row_meta,
                    )

    @staticmethod
    def _looks_like_legislation_table(headers: List[str]) -> bool:
        if not headers:
            return False
        has_title = any("title" in h for h in headers)
        has_date = any("date" in h for h in headers)
        has_sr = any("sr no" in h or "s. no" in h for h in headers)
        return has_title and has_date and has_sr

    def _add_row_provenance(self, *, row_meta: Dict[str, Any], cells: List[Any], headers: List[str]) -> None:
        title_idx = next((i for i, h in enumerate(headers) if "title" in h), None)
        if title_idx is not None and title_idx < len(cells):
            title = cells[title_idx].get_text(" ", strip=True)[:280]
            if title:
                row_meta["act_title"] = title

        date_idx = next((i for i, h in enumerate(headers) if "date" in h), None)
        if date_idx is not None and date_idx < len(cells):
            date_value = cells[date_idx].get_text(" ", strip=True)[:40]
            if date_value:
                row_meta["act_passed_on"] = date_value
                if YEAR_RE.search(date_value):
                    row_meta["act_year"] = YEAR_RE.search(date_value).group(0)  # type: ignore[union-attr]

        sr_idx = next((i for i, h in enumerate(headers) if "sr no" in h or "s. no" in h), None)
        if sr_idx is not None and sr_idx < len(cells):
            sr = cells[sr_idx].get_text(" ", strip=True)[:40]
            if sr:
                row_meta["act_no"] = sr

        title = row_meta.get("act_title", "")
        if title and "act_no" in row_meta:
            no_match = re.search(r"\b(?:act|ordinance)\s*no\.?\s*([^\),;]+)", title, re.IGNORECASE)
            if no_match:
                row_meta["act_no"] = no_match.group(1).strip()[:80]
        if title and "act_year" not in row_meta:
            year_match = YEAR_RE.search(title)
            if year_match:
                row_meta["act_year"] = year_match.group(0)

        section = row_meta.get("source_section")
        if section == "ordinances":
            row_meta["act_type"] = "ordinance"
        elif section == "bills":
            row_meta["act_type"] = "bill"
        elif section == "acts":
            row_meta["act_type"] = "act"

    def _collect_listing_links(
        self,
        *,
        html_text: str,
        base_url: str,
        listings: Dict[str, Dict[str, Any]],
        inherited_meta: Dict[str, Any],
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        for a in soup.find_all("a", href=True):
            self._capture_candidate(
                raw=a.get("href", ""),
                hint=a.get_text(" ", strip=True)[:240],
                base_url=base_url,
                docs={},
                listings=listings,
                route_meta=inherited_meta,
            )

    @staticmethod
    def _source_section_for_url(url: str) -> str:
        parts = urlsplit(url)
        path = (parts.path or "").lower()
        qs = parse_qs(parts.query or "")
        if _is_na_detail_discovered_url(url):
            return "detail"
        if path.endswith(("/acts-tenure.php", "/acts.php")):
            return "acts"
        if path.endswith(("/bills.php", "/bills-15.php", "/bills-passed.php")):
            status = (qs.get("status", [""])[0] or "").lower()
            btype = (qs.get("type", [""])[0] or "").lower()
            if btype == "4":
                return "ordinances"
            if status in ("pass", "majlis") or btype in ("1", "2", "3"):
                return "bills"
            return "bills"
        return "legislation"

    def _collect_detail_document_links(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        inherited_meta: Dict[str, Any],
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        detail_title_node = soup.select_one("h1") or soup.select_one("h2") or soup.select_one("title")
        detail_title = detail_title_node.get_text(" ", strip=True)[:280] if detail_title_node else ""
        base_meta = dict(inherited_meta)
        base_meta.setdefault("detail_url", base_url)
        if detail_title:
            base_meta["act_title"] = detail_title
            base_meta["detail_title"] = detail_title

        for row in soup.select("table tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            label = cells[0].get_text(" ", strip=True).strip(": ").lower()
            value = cells[1].get_text(" ", strip=True)
            if not value:
                continue
            current_act_no = str(base_meta.get("act_no", "")).strip()
            if ("act #" in label or "act no" in label or "ordinance no" in label) and (
                not current_act_no or re.fullmatch(r"\d+\.?", current_act_no)
            ):
                base_meta["act_no"] = value[:80]
            elif ("passage" in label or "passed" in label or "date" == label) and "act_passed_on" not in base_meta:
                base_meta["act_passed_on"] = value[:40]
            elif ("enforcement" in label or "assent" in label or "promulgation" in label) and "act_assented_on" not in base_meta:
                base_meta["act_assented_on"] = value[:40]
            if "act_year" not in base_meta and YEAR_RE.search(value):
                base_meta["act_year"] = YEAR_RE.search(value).group(0)  # type: ignore[union-attr]

        for a in soup.find_all("a", href=True):
            route_meta = dict(base_meta)
            route_meta["detail_fetch"] = "detail_documents"
            route_meta["discovery_channel"] = "act-detail-file-link"
            self._capture_candidate(
                raw=a.get("href", ""),
                hint=a.get_text(" ", strip=True)[:240],
                base_url=base_url,
                docs=docs,
                listings={},
                route_meta=route_meta,
            )

    def _capture_candidate(
        self,
        *,
        raw: str,
        hint: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: Dict[str, Dict[str, Any]],
        route_meta: Dict[str, Any],
    ) -> None:
        normalized = normalize_na_public_url(raw, base_url=base_url)
        if not normalized:
            return
        try:
            safe = check_url_policy(
                normalized,
                self.source.allow_list or [],
                document_cdn_hosts=self.source.document_cdn_hosts or [],
                allow_private_for_tests=_tests_allow_private(),
            )
        except URLPolicyError:
            self.stats["rejected_urls"] += 1
            return

        kind = _classify_na_discovered_url(safe)
        if kind == "document":
            path = (urlsplit(safe).path or "").lower()
            ext = path.rsplit(".", 1)[-1] if "." in path else ""
            meta = {
                "discovery_hint": hint[:240],
                "pdf_endpoint_kind": "uploads-documents-file" if path.startswith("/uploads/documents/") else "direct-file",
                **route_meta,
            }
            if ext and "document_format" not in meta:
                meta["document_format"] = ext
            if ext == "pdf":
                meta["expect_pdf"] = True
            existing = docs.get(safe)
            if existing is None:
                docs[safe] = meta
            else:
                for key, value in meta.items():
                    if key not in existing and value not in ("", None):
                        existing[key] = value
        elif kind == "listing" and safe != base_url:
            if safe not in listings:
                listings[safe] = dict(route_meta)
                listings[safe].setdefault("detail_url", safe)
            else:
                for key, value in route_meta.items():
                    if key not in listings[safe] and value not in ("", None):
                        listings[safe][key] = value


class SenatePipeline(BalochistanAssemblyPipeline):
    """Source-specific extraction for Senate legislation tables + detail-page document routing."""

    async def handle_listing(self, res, fr: CrawlFrontier) -> None:  # type: ignore[override]
        depth = int(fr.query_json.get("depth", 0))
        max_depth = int(self.source.crawl_max_depth or 2)
        target_kind = fr.query_json.get("target_kind", "instrument")
        docs: Dict[str, Dict[str, Any]] = {}
        listings: Dict[str, Dict[str, Any]] = {}
        inherited_meta = dict(fr.query_json.get("meta") or {})

        if self._is_detail_listing(res.final_url):
            self._collect_detail_document_links(
                html_text=res.text,
                base_url=res.final_url,
                docs=docs,
                inherited_meta=inherited_meta,
            )
        else:
            self._collect_structured_listing_rows(
                html_text=res.text,
                base_url=res.final_url,
                docs=docs,
                listings=listings,
            )

        added = await self._enqueue_documents_with_meta(docs, listing_url=res.final_url, default_target_kind=target_kind)
        self.stats["discovered"] += added

        if depth >= max_depth:
            return
        for nurl, nmeta in listings.items():
            key = f"listing:{nurl}"
            exists = (
                await self.db.execute(
                    select(CrawlFrontier).where(
                        CrawlFrontier.source_name == self.source.source_name,
                        CrawlFrontier.tier == 0,
                        CrawlFrontier.query_key == key,
                    )
                )
            ).scalars().first()
            if exists is None:
                route = {"listing": res.final_url}
                for key_name in (
                    "listing_fetch",
                    "result_index",
                    "source_section",
                    "act_year",
                    "act_no",
                    "act_title",
                    "act_passed_on",
                    "act_assented_on",
                    "act_type",
                    "detail_url",
                ):
                    if key_name in nmeta:
                        route[key_name] = nmeta[key_name]
                self.db.add(
                    CrawlFrontier(
                        source_name=self.source.source_name,
                        tier=0,
                        query_key=key,
                        query_json={
                            "kind": "listing",
                            "url": nurl,
                            "target_kind": target_kind,
                            "depth": depth + 1,
                            "route": route,
                            "meta": nmeta,
                        },
                        cursor_json={},
                        priority=40,
                    )
                )
                self.stats["discovered"] += 1
        await self.db.flush()

    @staticmethod
    def _is_detail_listing(url: str) -> bool:
        return SENATE_DETAIL_PATH_RE.search((urlsplit(url).path or "").lower()) is not None

    def _collect_structured_listing_rows(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: Dict[str, Dict[str, Any]],
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        row_index = 0
        for table in soup.select("table"):
            headers = [th.get_text(" ", strip=True).lower() for th in table.select("thead th")]
            if not headers:
                headers = [th.get_text(" ", strip=True).lower() for th in table.select("tr th")]
            if not self._looks_like_legislation_table(headers):
                continue
            rows = table.select("tbody tr") or table.select("tr")
            for tr in rows:
                cells = tr.find_all("td")
                if len(cells) < 2:
                    continue
                links = tr.find_all("a", href=True)
                if not links:
                    continue

                row_index += 1
                row_meta: Dict[str, Any] = {
                    "listing_fetch": "legislation_table",
                    "discovery_channel": "legislation-table-row",
                    "source_section": self._source_section_for_url(base_url),
                    "result_index": row_index,
                }
                self._add_row_provenance(row_meta=row_meta, cells=cells, headers=headers)

                for link in links:
                    hint = link.get_text(" ", strip=True)[:240]
                    self._capture_candidate(
                        raw=link.get("href", ""),
                        hint=hint,
                        base_url=base_url,
                        docs=docs,
                        listings=listings,
                        route_meta=row_meta,
                    )

    @staticmethod
    def _looks_like_legislation_table(headers: List[str]) -> bool:
        if not headers:
            return False
        has_title = any("title" in h for h in headers)
        has_legislation_signal = any(
            "act no" in h or "ordinance" in h or "name of mover" in h or "date of" in h or "file" in h for h in headers
        )
        return has_title and has_legislation_signal

    @staticmethod
    def _source_section_for_url(url: str) -> str:
        path = (urlsplit(url).path or "").lower()
        if path.endswith("/acts.php"):
            return "acts"
        if path.endswith("/ordinance.php"):
            return "ordinances"
        if path.endswith(("/pbs.php", "/pbna.php", "/gbs.php", "/gbna.php", "/bs.php", "/bills.php")):
            return "bills"
        if path.endswith("/essence.php"):
            return "detail"
        return "legislation"

    def _add_row_provenance(self, *, row_meta: Dict[str, Any], cells: List[Any], headers: List[str]) -> None:
        title_idx = next((i for i, h in enumerate(headers) if "title" in h), None)
        if title_idx is not None and title_idx < len(cells):
            title = cells[title_idx].get_text(" ", strip=True)[:280]
            if title:
                row_meta["act_title"] = title

        for idx, header in enumerate(headers):
            if idx >= len(cells):
                continue
            value = cells[idx].get_text(" ", strip=True)
            if not value:
                continue

            if "act no" in header:
                row_meta["act_no"] = value[:80]
            elif "name of mover" in header:
                row_meta["act_type"] = f"bill by {value[:60]}"
            elif "category of the bill" in header:
                row_meta["act_type"] = value[:80]
            elif "promulgation" in header and "act_assented_on" not in row_meta:
                row_meta["act_assented_on"] = value[:40]
            elif "assent" in header and "act_assented_on" not in row_meta:
                row_meta["act_assented_on"] = value[:40]
            elif ("passage" in header or "passed by" in header or "consideration" in header) and "act_passed_on" not in row_meta:
                row_meta["act_passed_on"] = value[:40]

            if "act_year" not in row_meta and YEAR_RE.search(value):
                row_meta["act_year"] = YEAR_RE.search(value).group(0)  # type: ignore[union-attr]

        if "act_no" not in row_meta and row_meta.get("act_title"):
            no_match = re.search(r"\b(?:act|ordinance)\s+no\.?\s*([^\),;]+)", row_meta["act_title"], re.IGNORECASE)
            if no_match:
                row_meta["act_no"] = no_match.group(1).strip()[:80]
        if "act_year" not in row_meta and row_meta.get("act_title"):
            year_match = YEAR_RE.search(row_meta["act_title"])
            if year_match:
                row_meta["act_year"] = year_match.group(0)

    def _collect_detail_document_links(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        inherited_meta: Dict[str, Any],
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        detail_title_node = soup.select_one("h1") or soup.select_one("h2") or soup.select_one("title")
        detail_title = detail_title_node.get_text(" ", strip=True)[:280] if detail_title_node else ""
        base_meta = dict(inherited_meta)
        base_meta.setdefault("detail_url", base_url)
        if detail_title and not base_meta.get("act_title"):
            base_meta["act_title"] = detail_title
        if detail_title:
            base_meta["detail_title"] = detail_title

        for row in soup.select("table tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            label = cells[0].get_text(" ", strip=True).lower()
            value = cells[1].get_text(" ", strip=True)
            if not value:
                continue
            if ("act no" in label or "ordinance no" in label) and "act_no" not in base_meta:
                base_meta["act_no"] = value[:80]
            elif ("passage" in label or "passed" in label) and "act_passed_on" not in base_meta:
                base_meta["act_passed_on"] = value[:40]
            elif ("promulgation" in label or "assent" in label) and "act_assented_on" not in base_meta:
                base_meta["act_assented_on"] = value[:40]
            if "act_year" not in base_meta and YEAR_RE.search(value):
                base_meta["act_year"] = YEAR_RE.search(value).group(0)  # type: ignore[union-attr]

        for a in soup.find_all("a", href=True):
            route_meta = dict(base_meta)
            route_meta["detail_fetch"] = "essence_documents"
            route_meta["discovery_channel"] = "act-detail-file-link"
            self._capture_candidate(
                raw=a.get("href", ""),
                hint=a.get_text(" ", strip=True)[:240],
                base_url=base_url,
                docs=docs,
                listings={},
                route_meta=route_meta,
            )

    def _capture_candidate(
        self,
        *,
        raw: str,
        hint: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: Dict[str, Dict[str, Any]],
        route_meta: Dict[str, Any],
    ) -> None:
        normalized = normalize_senate_public_url(raw, base_url=base_url)
        if not normalized:
            return
        try:
            safe = check_url_policy(
                normalized,
                self.source.allow_list or [],
                document_cdn_hosts=self.source.document_cdn_hosts or [],
                allow_private_for_tests=_tests_allow_private(),
            )
        except URLPolicyError:
            self.stats["rejected_urls"] += 1
            return

        kind = _classify_senate_discovered_url(safe)
        if kind == "document":
            path = (urlsplit(safe).path or "").lower()
            ext = path.rsplit(".", 1)[-1] if "." in path else ""
            meta = {
                "discovery_hint": hint[:240],
                "pdf_endpoint_kind": "uploads-documents-file" if path.startswith("/uploads/documents/") else "direct-file",
                **route_meta,
            }
            if ext and "document_format" not in meta:
                meta["document_format"] = ext
            if ext == "pdf":
                meta["expect_pdf"] = True
            existing = docs.get(safe)
            if existing is None:
                docs[safe] = meta
            else:
                for key, value in meta.items():
                    if key not in existing and value not in ("", None):
                        existing[key] = value
        elif kind == "listing" and safe != base_url:
            if safe not in listings:
                listings[safe] = dict(route_meta)
                listings[safe].setdefault("detail_url", safe)
            else:
                for key, value in route_meta.items():
                    if key not in listings[safe] and value not in ("", None):
                        listings[safe][key] = value


class GazetteOfPakistanPipeline(BalochistanAssemblyPipeline):
    """Source-specific extraction for PCP Gazette listings and direct document links."""

    async def handle_listing(self, res, fr: CrawlFrontier) -> None:  # type: ignore[override]
        depth = int(fr.query_json.get("depth", 0))
        max_depth = int(self.source.crawl_max_depth or 2)
        target_kind = fr.query_json.get("target_kind", "instrument")
        docs: Dict[str, Dict[str, Any]] = {}
        listings: Dict[str, Dict[str, Any]] = {}
        inherited_meta = dict(fr.query_json.get("meta") or {})

        if self._is_detail_listing(res.final_url):
            self._collect_detail_document_links(
                html_text=res.text,
                base_url=res.final_url,
                docs=docs,
                inherited_meta=inherited_meta,
            )
        else:
            self._collect_structured_listing_rows(
                html_text=res.text,
                base_url=res.final_url,
                docs=docs,
                listings=listings,
            )

        self._collect_listing_links(
            html_text=res.text,
            base_url=res.final_url,
            listings=listings,
            inherited_meta={
                "listing_fetch": "navigation_links",
                "source_section": self._source_section_for_url(res.final_url),
            },
        )

        added = await self._enqueue_documents_with_meta(docs, listing_url=res.final_url, default_target_kind=target_kind)
        self.stats["discovered"] += added

        if depth >= max_depth:
            return
        for nurl, nmeta in listings.items():
            key = f"listing:{nurl}"
            exists = (
                await self.db.execute(
                    select(CrawlFrontier).where(
                        CrawlFrontier.source_name == self.source.source_name,
                        CrawlFrontier.tier == 0,
                        CrawlFrontier.query_key == key,
                    )
                )
            ).scalars().first()
            if exists is None:
                route = {"listing": res.final_url}
                for key_name in (
                    "listing_fetch",
                    "detail_fetch",
                    "discovery_channel",
                    "result_index",
                    "source_section",
                    "detail_url",
                    "detail_title",
                    "gazette_job_id",
                    "gazette_department",
                    "gazette_title",
                    "gazette_date",
                    "gazette_issue_no",
                    "gazette_part",
                    "gazette_active",
                ):
                    if key_name in nmeta:
                        route[key_name] = nmeta[key_name]
                self.db.add(
                    CrawlFrontier(
                        source_name=self.source.source_name,
                        tier=0,
                        query_key=key,
                        query_json={
                            "kind": "listing",
                            "url": nurl,
                            "target_kind": target_kind,
                            "depth": depth + 1,
                            "route": route,
                            "meta": nmeta,
                        },
                        cursor_json={},
                        priority=40,
                    )
                )
                self.stats["discovered"] += 1
        await self.db.flush()

    @staticmethod
    def _is_detail_listing(url: str) -> bool:
        return PCP_DETAIL_PATH_RE.search((urlsplit(url).path or "").lower()) is not None

    @staticmethod
    def _source_section_for_url(url: str) -> str:
        path = (urlsplit(url).path or "").lower()
        if path.endswith("/download"):
            return "download_notifications"
        if path.endswith("/weeklynitifications") or path.endswith("/weeklynotifications"):
            return "weekly_notifications"
        if PCP_DETAIL_PATH_RE.search(path):
            return "detail"
        return "gazette"

    def _collect_structured_listing_rows(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: Dict[str, Dict[str, Any]],
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        row_index = 0
        for table in soup.select("table"):
            headers = [th.get_text(" ", strip=True).lower() for th in table.select("thead th")]
            if not headers:
                headers = [th.get_text(" ", strip=True).lower() for th in table.select("tr th")]
            table_kind = self._table_kind(headers)
            if not table_kind:
                continue

            rows = table.select("tbody tr") or table.select("tr")
            for tr in rows:
                cells = tr.find_all("td")
                if len(cells) < 2:
                    continue
                links = tr.find_all("a", href=True)
                if not links:
                    continue

                row_index += 1
                row_meta: Dict[str, Any] = {
                    "listing_fetch": f"{table_kind}_table",
                    "discovery_channel": "gazette-table-row",
                    "source_section": self._source_section_for_url(base_url),
                    "result_index": row_index,
                }
                self._add_row_provenance(row_meta=row_meta, cells=cells, headers=headers, links=links, table_kind=table_kind)
                for link in links:
                    hint = link.get_text(" ", strip=True)[:240] or row_meta.get("gazette_title", "")[:240] or row_meta.get("gazette_job_id", "")[:240]
                    self._capture_candidate(
                        raw=link.get("href", ""),
                        hint=hint,
                        base_url=base_url,
                        docs=docs,
                        listings=listings,
                        route_meta=row_meta,
                    )

    @staticmethod
    def _table_kind(headers: List[str]) -> Optional[str]:
        if not headers:
            return None
        if any("job id" in h for h in headers) and any("department" in h for h in headers) and any("download" in h for h in headers):
            return "downloads"
        if any("weekly issue" in h for h in headers) and any("download" in h for h in headers):
            return "weekly"
        return None

    def _add_row_provenance(self, *, row_meta: Dict[str, Any], cells: List[Any], headers: List[str], links: List[Any], table_kind: str) -> None:
        for idx, header in enumerate(headers):
            if idx >= len(cells):
                continue
            value = cells[idx].get_text(" ", strip=True)
            if not value:
                continue

            if "job id" in header:
                row_meta["gazette_job_id"] = value[:120]
            elif "department" in header:
                row_meta["gazette_department"] = value[:160]
            elif header == "title" or "title" in header:
                row_meta["gazette_title"] = value[:280]
                row_meta.setdefault("act_title", value[:280])
            elif "weekly issue" in header:
                row_meta["gazette_issue_no"] = value[:80]
            elif "date" in header:
                row_meta["gazette_date"] = value[:40]
            elif "parts" in header:
                row_meta["gazette_part"] = value[:40]
            elif "active" in header:
                row_meta["gazette_active"] = value[:16].lower() in ("true", "1", "yes")

        title = row_meta.get("gazette_title") or row_meta.get("act_title") or ""
        if title:
            row_meta.setdefault("act_title", title)
            row_meta.setdefault("act_type", self._infer_act_type(title))

        for candidate in (title, row_meta.get("gazette_job_id", ""), row_meta.get("gazette_issue_no", ""), row_meta.get("gazette_date", "")):
            if not row_meta.get("act_year") and candidate:
                year_match = YEAR_RE.search(str(candidate))
                if year_match:
                    row_meta["act_year"] = year_match.group(0)

        if not row_meta.get("gazette_part"):
            part_hint = self._extract_part_hint(
                row_meta.get("gazette_job_id", ""),
                row_meta.get("gazette_title", ""),
                *(a.get("href", "") for a in links),
            )
            if part_hint:
                row_meta["gazette_part"] = part_hint

        if table_kind == "weekly" and "act_type" not in row_meta:
            row_meta["act_type"] = "gazette_notice"

    def _collect_detail_document_links(
        self,
        *,
        html_text: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        inherited_meta: Dict[str, Any],
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        detail_title_node = soup.select_one("h1") or soup.select_one("h2") or soup.select_one("title")
        detail_title = detail_title_node.get_text(" ", strip=True)[:280] if detail_title_node else ""
        base_meta = dict(inherited_meta)
        base_meta.setdefault("detail_url", base_url)
        if detail_title:
            base_meta.setdefault("detail_title", detail_title)
            base_meta.setdefault("gazette_title", detail_title)
            base_meta.setdefault("act_title", detail_title)
            base_meta.setdefault("act_type", self._infer_act_type(detail_title))
            year_match = YEAR_RE.search(detail_title)
            if year_match and "act_year" not in base_meta:
                base_meta["act_year"] = year_match.group(0)

        for row in soup.select("table tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            label = cells[0].get_text(" ", strip=True).lower()
            value = cells[1].get_text(" ", strip=True)
            if not value:
                continue
            if "job id" in label and "gazette_job_id" not in base_meta:
                base_meta["gazette_job_id"] = value[:120]
            elif "department" in label and "gazette_department" not in base_meta:
                base_meta["gazette_department"] = value[:160]
            elif "title" in label and "gazette_title" not in base_meta:
                base_meta["gazette_title"] = value[:280]
                base_meta.setdefault("act_title", value[:280])
            elif "issue" in label and "gazette_issue_no" not in base_meta:
                base_meta["gazette_issue_no"] = value[:80]
            elif "part" in label and "gazette_part" not in base_meta:
                base_meta["gazette_part"] = value[:40]
            elif "date" in label and "gazette_date" not in base_meta:
                base_meta["gazette_date"] = value[:40]

            if "act_year" not in base_meta and YEAR_RE.search(value):
                base_meta["act_year"] = YEAR_RE.search(value).group(0)  # type: ignore[union-attr]

        for a in soup.find_all("a", href=True):
            route_meta = dict(base_meta)
            route_meta["detail_fetch"] = "detail_documents"
            route_meta["discovery_channel"] = "gazette-detail-file-link"
            self._capture_candidate(
                raw=a.get("href", ""),
                hint=a.get_text(" ", strip=True)[:240] or route_meta.get("gazette_title", "")[:240],
                base_url=base_url,
                docs=docs,
                listings={},
                route_meta=route_meta,
            )

    def _collect_listing_links(
        self,
        *,
        html_text: str,
        base_url: str,
        listings: Dict[str, Dict[str, Any]],
        inherited_meta: Dict[str, Any],
    ) -> None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        for a in soup.find_all("a", href=True):
            self._capture_candidate(
                raw=a.get("href", ""),
                hint=a.get_text(" ", strip=True)[:240],
                base_url=base_url,
                docs={},
                listings=listings,
                route_meta=inherited_meta,
            )

    @staticmethod
    def _infer_act_type(title: str) -> str:
        lowered = (title or "").lower()
        if "ordinance" in lowered:
            return "ordinance"
        if " act" in lowered or lowered.startswith("act "):
            return "act"
        if "rules" in lowered or "rule" in lowered:
            return "rules"
        if "bill" in lowered:
            return "bill"
        return "gazette_notice"

    @staticmethod
    def _extract_part_hint(*values: str) -> Optional[str]:
        for value in values:
            text = str(value or "")
            part_match = re.search(r"(?i)\bpart[\s\-_]*([ivx0-9]+)\b", text)
            if part_match:
                return part_match.group(1).upper()
            ex_match = re.search(r"(?i)\bex[\s\.-]*gaz[\s\.-]*([ivx0-9]+)\b", text)
            if ex_match:
                return ex_match.group(1).upper()
        return None

    def _capture_candidate(
        self,
        *,
        raw: str,
        hint: str,
        base_url: str,
        docs: Dict[str, Dict[str, Any]],
        listings: Dict[str, Dict[str, Any]],
        route_meta: Dict[str, Any],
    ) -> None:
        normalized = normalize_pcp_public_url(raw, base_url=base_url)
        if not normalized:
            return
        try:
            safe = check_url_policy(
                normalized,
                self.source.allow_list or [],
                document_cdn_hosts=self.source.document_cdn_hosts or [],
                allow_private_for_tests=_tests_allow_private(),
            )
        except URLPolicyError:
            self.stats["rejected_urls"] += 1
            return

        kind = _classify_pcp_discovered_url(safe)
        if kind == "document":
            path = (urlsplit(safe).path or "").lower()
            ext = path.rsplit(".", 1)[-1] if "." in path else ""
            meta = {
                "discovery_hint": hint[:240],
                "pdf_endpoint_kind": "siteimage-downloads-file" if path.startswith("/siteimage/downloads/") else "direct-file",
                **route_meta,
            }
            if ext and "document_format" not in meta:
                meta["document_format"] = ext
            if ext == "pdf":
                meta["expect_pdf"] = True
            existing = docs.get(safe)
            if existing is None:
                docs[safe] = meta
            else:
                for key, value in meta.items():
                    if key not in existing and value not in ("", None):
                        existing[key] = value
        elif kind == "listing" and safe != base_url:
            path = (urlsplit(safe).path or "").lower()
            is_row_scoped = any(route_meta.get(k) for k in ("gazette_job_id", "gazette_issue_no", "gazette_title"))
            if PCP_DETAIL_PATH_RE.search(path) and not is_row_scoped:
                return
            if safe not in listings:
                listings[safe] = dict(route_meta)
                listings[safe].setdefault("detail_url", safe)
            else:
                for key, value in route_meta.items():
                    if key not in listings[safe] and value not in ("", None):
                        listings[safe][key] = value

    async def _enqueue_documents_with_meta(
        self,
        docs: Dict[str, Dict[str, Any]],
        *,
        listing_url: str,
        default_target_kind: str,
    ) -> int:
        added = 0
        for url, meta in docs.items():
            kind = default_target_kind
            key = f"{kind}:{url}"
            exists = (
                await self.db.execute(
                    select(CrawlFrontier).where(
                        CrawlFrontier.source_name == self.source.source_name,
                        CrawlFrontier.tier == 0,
                        CrawlFrontier.query_key == key,
                    )
                )
            ).scalars().first()
            if exists is not None:
                continue

            route: Dict[str, Any] = {"listing": listing_url}
            for key_name in (
                "listing_fetch",
                "detail_fetch",
                "discovery_channel",
                "result_index",
                "source_section",
                "detail_url",
                "detail_title",
                "gazette_job_id",
                "gazette_department",
                "gazette_title",
                "gazette_date",
                "gazette_issue_no",
                "gazette_part",
                "gazette_active",
                "act_title",
                "act_no",
                "act_year",
                "act_type",
                "document_format",
                "pdf_endpoint_kind",
            ):
                if key_name in meta:
                    route[key_name] = meta[key_name]
            self.db.add(
                CrawlFrontier(
                    source_name=self.source.source_name,
                    tier=0,
                    query_key=key,
                    query_json={
                        "kind": kind,
                        "url": url,
                        "route": route,
                        "meta": meta,
                        "expect_pdf": bool(meta.get("expect_pdf")),
                    },
                    cursor_json={},
                    priority=50,
                )
            )
            added += 1
        await self.db.flush()
        return added


async def scrape_legislature(source: ScraperSource, db: AsyncSession, **kwargs) -> Dict[str, Any]:
    pipeline_cls = PublicPipeline
    if source.source_name == "BalochistanAssembly":
        pipeline_cls = BalochistanAssemblyPipeline
    elif source.source_name == "AJKAssembly":
        pipeline_cls = AJKAssemblyPipeline
    elif source.source_name == "PunjabAssembly":
        pipeline_cls = PunjabAssemblyPipeline
    elif source.source_name == "SindhAssembly":
        pipeline_cls = SindhAssemblyPipeline
    elif source.source_name == "KPAssembly":
        pipeline_cls = KPAssemblyPipeline
    elif source.source_name == "NationalAssembly":
        pipeline_cls = NationalAssemblyPipeline
    elif source.source_name == "Senate":
        pipeline_cls = SenatePipeline
    elif source.source_name == "GazetteOfPakistan":
        pipeline_cls = GazetteOfPakistanPipeline
    return await run_public_source(db, source, seed_listings=listings_for(source), pipeline_cls=pipeline_cls, **kwargs)
