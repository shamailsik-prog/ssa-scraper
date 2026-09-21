"""
Deterministic legal parsers wrapped as a StructuredExtractor (Amendment §1-D, §7-A).

These outputs are authoritative over anything an AI engine returns for citations, statutory
patterns, bench counting and deduplication keys. Every value carries a raw-text evidence
snippet so the validator can prove it.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup

from scraper.extractors.schemas import JudgmentExtraction, StatuteExtraction, InstrumentExtraction, SearchResultExtraction, SearchFormMapExtraction
from scraper.parsers.bench_parser import parse_bench
from scraper.parsers.citation_extractor import (
    canonicalise_statute_name,
    extract_citations,
    extract_instrument_mentions,
    extract_statute_mentions,
    extract_statutes,
    normalise_citation,
    score_confidence,
)
from scraper.parsers.statute_parser import detect_statute_name, prefer_official_statute_title, split_into_sections
from scraper.parsers.text_cleaner import clean_html

DETERMINISTIC_VERSION = "det-1.0"

MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
DATE_PATTERNS = [
    re.compile(rf"(?i)\b(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:of\s+)?({MONTHS})[,.]?\s+(\d{{4}})\b"),
    re.compile(rf"(?i)\b({MONTHS})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b"),
    re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b"),
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
]
DECIDED_HINT = re.compile(r"(?i)(decided on|date of (?:decision|judgment|hearing|order)|announced on|dated|judgment dated|order dated|heard on)[:\s]*")
TITLE_VERSUS = re.compile(r"(?im)^\s*(.{3,200}?)\s+(?:versus|vs\.?|v\.)\s+(.{3,200}?)\s*$")
TITLE_INLINE = re.compile(r"(?i)([A-Z][A-Za-z0-9&.,'()\- ]{2,160}?)\s+(?:versus|vs\.?|v\.)\s+([A-Z][A-Za-z0-9&.,'()\- ]{2,160})")
HEADNOTE = re.compile(r"(?is)\bhead\s*notes?\s*[:\-—]?\s*\n?(.{40,2500}?)(?:\n\s*\n|$)")
INSTRUMENT_TYPE = re.compile(r"(?i)\b(ordinance|amendment act|act|notification|s\.?r\.?o\.?|rules|regulations|bill|gazette)\b")
INSTRUMENT_NUMBER = re.compile(r"(?i)\b(?:act|ordinance|bill|s\.?r\.?o\.?)\s*(?:no\.?\s*)?([IVXLC]+|\d+)(?:\s*\(?[IVXLC]*\)?)?\s*(?:of\s+(\d{4}))?")
GAZETTE_REF = re.compile(r"(?i)gazette of pakistan[^\n]{0,120}")
_MONTH_IDX = {m: i % 12 + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}


def _to_date(parts, order: str) -> Optional[date]:
    try:
        if order == "dmy_name":
            d, mon, y = parts
            return date(int(y), _MONTH_IDX[mon[:3].lower()], int(d))
        if order == "mdy_name":
            mon, d, y = parts
            return date(int(y), _MONTH_IDX[mon[:3].lower()], int(d))
        if order == "dmy":
            d, m, y = parts
            return date(int(y), int(m), int(d))
        if order == "ymd":
            y, m, d = parts
            return date(int(y), int(m), int(d))
    except (ValueError, KeyError):
        return None
    return None


def find_dates(text: str) -> List[tuple]:
    """All dates in the text with their spans and evidence; earliest hint-anchored date first."""
    found: List[tuple] = []
    for pat, order in zip(DATE_PATTERNS, ("dmy_name", "mdy_name", "dmy", "ymd")):
        for m in pat.finditer(text):
            d = _to_date(m.groups(), order)
            if d and 1947 <= d.year <= 2100:
                found.append((d, m.start(), text[max(0, m.start() - 60) : m.end() + 10]))
    return found


def decision_date_from_text(text: str) -> tuple[Optional[date], Optional[str]]:
    if not text:
        return None, None
    head = text[:20000]
    best = None
    for m in DECIDED_HINT.finditer(head):
        window = head[m.end() : m.end() + 60]
        dates = find_dates(window)
        if dates:
            d, _, _ = dates[0]
            return d, head[m.start() : m.end() + 40].strip()
    dates = find_dates(head[:4000])
    if dates:
        best = dates[0]
        return best[0], best[2].strip()
    return None, None


def date_in_text(d: Optional[date], text: str) -> bool:
    if d is None or not text:
        return False
    return any(found == d for found, _, _ in find_dates(text))


def case_title_from_text(text: str, html: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for tag in ("h1", "h2", "title", "b", "strong"):
            for el in soup.find_all(tag):
                t = el.get_text(" ", strip=True)
                if TITLE_INLINE.search(t) and len(t) < 400:
                    return re.sub(r"\s+", " ", t), t[:200]
    head = (text or "")[:6000]
    m = TITLE_VERSUS.search(head) or TITLE_INLINE.search(head)
    if m:
        title = re.sub(r"\s+", " ", m.group(0)).strip(" .")
        return title, title[:200]
    return None, None


def extract_judgment_deterministic(*, html: Optional[str], text: Optional[str], source_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    source_meta = source_meta or {}
    raw_text = text or (clean_html(html) if html else "")
    evidence: Dict[str, str] = {}
    cits = extract_citations(raw_text)
    own: List[str] = []
    cited: List[str] = []
    head_limit = max(300, min(800, int(len(raw_text) * 0.15)))
    for c in cits:
        norm = c.get("normalized") or normalise_citation(c["raw"])
        if c["span"][0] < head_limit and len(own) < 6:
            own.append(norm)
            evidence.setdefault("citations", raw_text[max(0, c["span"][0] - 30) : c["span"][1] + 30].strip())
        else:
            cited.append(norm)
    hint = source_meta.get("citation")
    if hint:
        hn = normalise_citation(hint)
        if hn and hn not in own:
            own.insert(0, hn)
            evidence.setdefault("citations", f"result row: {hint}")
    cited = [c for c in dict.fromkeys(cited) if c not in own]
    bench = parse_bench(raw_text)
    if bench.evidence:
        evidence["judge_names"] = bench.evidence
        evidence["bench_size"] = bench.explicit_phrase or bench.evidence
    title, title_ev = case_title_from_text(raw_text, html)
    if not title and source_meta.get("title"):
        title, title_ev = source_meta["title"], f"result row: {source_meta['title']}"
    if title_ev:
        evidence["case_title"] = title_ev
    court = source_meta.get("court")
    if court:
        evidence["court"] = f"result row: {court}"
    else:
        for c in cits[:3]:
            if c.get("court"):
                court = c["court"]
                evidence["court"] = c["raw"]
                break
    if not court:
        m = re.search(r"(?i)\b(supreme court of pakistan|lahore high court|high court of sindh|sindh high court|peshawar high court|balochistan high court|high court of balochistan|islamabad high court|federal shariat court)\b", raw_text[:5000])
        if m:
            court = m.group(1)
            evidence["court"] = m.group(0)
    ddate, dev = decision_date_from_text(raw_text)
    if dev:
        evidence["decision_date"] = dev
    year = None
    for c in cits[:3]:
        if c.get("year"):
            year = c["year"]
            evidence["year"] = c["raw"]
            break
    if year is None and ddate:
        year = ddate.year
        evidence["year"] = dev or str(ddate)
    statutes = []
    for s in extract_statutes(raw_text):
        name = s.get("act") or s.get("statute") or s.get("act_name")
        sec = s.get("section") or s.get("article") or s.get("rule")
        if name or sec:
            statutes.append({"statute_name": name, "section_number": str(sec) if sec is not None else None})
            evidence.setdefault("statutes_cited", s.get("raw", "")[:200])
    seen = set()
    statutes_unique = []
    for s in statutes:
        k = (s["statute_name"], s["section_number"])
        if k not in seen:
            seen.add(k)
            statutes_unique.append(s)
    hn = HEADNOTE.search(raw_text[:15000])
    headnotes = hn.group(1).strip() if hn else None
    pdf_links: List[str] = []
    doc_links: List[str] = []
    if html:
        soup = BeautifulSoup(html, "html.parser")
        base = source_meta.get("url") or ""
        for a in soup.find_all("a", href=True):
            href = urljoin(base, a["href"])
            if href.lower().endswith(".pdf") or "pdf" in a.get_text(" ", strip=True).lower():
                pdf_links.append(href)
            elif any(k in href.lower() for k in ("judgment", "judgement", "case", "detail")):
                doc_links.append(href)
    fields = {"citation": own[0] if own else None, "court": court, "year": year, "title": title, "judges": bench.judge_names, "content": raw_text}
    conf = score_confidence(fields)
    data = JudgmentExtraction(
        citations=own,
        case_title=title,
        court=court,
        judge_names=bench.judge_names,
        bench_size=bench.bench_size,
        bench_type=bench.bench_type,
        decision_date=ddate,
        year=year,
        full_text_candidate=raw_text,
        headnotes=headnotes,
        statutes_cited=statutes_unique,
        citations_cited=cited[:500],
        source_document_links=list(dict.fromkeys(doc_links))[:50],
        pdf_links=list(dict.fromkeys(pdf_links))[:50],
        field_evidence=evidence,
        extractor_confidence=conf,
    )
    out = data.model_dump(mode="json")
    out["_bench_conflict"] = bench.conflict
    return out


def extract_statute_deterministic(*, html: Optional[str], text: Optional[str], source_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    source_meta = source_meta or {}
    body = html or text or ""
    raw_text = text or (clean_html(html) if html else "")
    detected_name = source_meta.get("statute_name") or detect_statute_name(body, source_meta.get("url"))
    official_title = source_meta.get("act_title") or source_meta.get("detail_title")
    name = prefer_official_statute_title(detected_name, official_title)
    sections = split_into_sections(body, name)
    evidence: Dict[str, str] = {}
    if name:
        evidence["statute_name"] = raw_text[:160]
    year = None
    m = re.search(r"\b(1[89]\d{2}|20\d{2})\b", name or "")
    if m:
        year = int(m.group(1))
        evidence["year_enacted"] = name
    stype = None
    mt = re.search(r"(?i)\b(act|ordinance|constitution|rules|regulations|order)\b", name or "")
    if mt:
        stype = mt.group(1).lower()
    jurisdiction = source_meta.get("jurisdiction") or ("Federal" if source_meta.get("source_name") in ("PakistanCode", "NationalAssembly", "Senate", "GazetteOfPakistan") else None)
    sec_models = []
    for s in sections:
        txt = s.get("section_text") or ""
        sec_models.append(
            {
                "statute_name": name,
                "short_name": None,
                "section_number": s.get("section_number"),
                "section_title": (s.get("section_title") or None),
                "section_text": txt,
                "chapter": s.get("chapter"),
                "year_enacted": year,
                "effective_from": None,
                "effective_to": None,
                "amending_instrument": _amending_footnote(txt),
                "jurisdiction": jurisdiction,
                "field_evidence": {"section_number": txt[:120]} if txt else {},
                "extractor_confidence": 0.9 if s.get("section_number") and len(txt) > 20 else 0.4,
            }
        )
    conf = 0.9 if (name and len(sec_models) >= 3) else (0.6 if sec_models else 0.2)
    data = StatuteExtraction(
        statute_name=name,
        short_name=None,
        jurisdiction=jurisdiction,
        year_enacted=year,
        statute_type=stype,
        sections=sec_models,
        field_evidence=evidence,
        extractor_confidence=conf,
    )
    return data.model_dump(mode="json")


AMENDING = re.compile(r"(?i)(?:subs(?:tituted)?|ins(?:erted)?|added|omitted|amended)\s+by\s+([^.;\n]{4,120}?(?:act|ordinance|order)[^.;\n]{0,60})")


def _amending_footnote(text: str) -> Optional[str]:
    m = AMENDING.search(text or "")
    return m.group(0).strip() if m else None


def extract_instrument_deterministic(*, html: Optional[str], text: Optional[str], source_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    source_meta = source_meta or {}
    raw_text = text or (clean_html(html) if html else "")
    head = raw_text[:4000]
    evidence: Dict[str, str] = {}
    itype = None
    mt = INSTRUMENT_TYPE.search(head)
    if mt:
        token = mt.group(1).lower().replace(".", "")
        itype = {"amendment act": "amendment", "sro": "notification", "gazette": "gazette_notice"}.get(token, token)
        evidence["type"] = head[max(0, mt.start() - 40) : mt.end() + 40].strip()
    number = None
    mn = INSTRUMENT_NUMBER.search(head)
    if mn:
        number = mn.group(1) + (f" of {mn.group(2)}" if mn.group(2) else "")
        evidence["number"] = mn.group(0)
    d, dev = decision_date_from_text(head)
    if dev:
        evidence["date"] = dev
    title = None
    for line in head.split("\n"):
        line = line.strip()
        if 10 < len(line) < 300 and re.search(r"(?i)\b(act|ordinance|rules|regulations|notification|bill)\b", line):
            title = line
            evidence["title"] = line
            break
    gz = GAZETTE_REF.search(raw_text)
    affected = None
    aff_secs: List[str] = []
    ma = re.search(r"(?i)(?:amendment(?:s)? (?:to|of|in) the|in the)\s+([A-Z][A-Za-z ,]{3,120}?(?:Act|Ordinance|Code)[, ]*\d{4})", raw_text[:20000])
    if ma:
        affected = ma.group(1).strip(" ,")
        evidence["affected_statute"] = ma.group(0)
    citation_mentions = extract_instrument_mentions(raw_text[:50000])
    statute_mentions = extract_statute_mentions(raw_text[:50000])
    for s in statute_mentions:
        sec = s.get("section_number")
        if sec:
            aff_secs.append(str(sec))
        if affected is None and s.get("canonical_statute_name"):
            affected = str(s.get("canonical_statute_name"))
            evidence.setdefault("affected_statute", s.get("raw", "")[:200])
    if affected:
        affected = canonicalise_statute_name(affected) or affected
    if not number and citation_mentions:
        first = citation_mentions[0]
        if first.get("normalized"):
            number = str(first["normalized"])
            evidence.setdefault("number", first.get("raw", "")[:120])
    for s in extract_statutes(raw_text[:50000]):
        sec = s.get("section") or s.get("article")
        if sec:
            aff_secs.append(str(sec))
    conf = 0.85 if (itype and (number or title)) else 0.4
    data = InstrumentExtraction(
        type=itype,
        number=number,
        date=d,
        title=title,
        gazette_ref=gz.group(0).strip() if gz else None,
        full_text=raw_text,
        affected_statute=affected,
        affected_sections=list(dict.fromkeys(aff_secs))[:200],
        citation_mentions=citation_mentions[:200],
        statute_mentions=statute_mentions[:300],
        field_evidence=evidence,
        extractor_confidence=conf,
    )
    return data.model_dump(mode="json")


def extract_result_rows_deterministic(*, html: str, search_map: Optional[Dict[str, Any]] = None, base_url: str = "") -> Dict[str, Any]:
    """Parse a result table using the stored search map (selectors and column indexes), or fall back to
    generic table introspection. Every row needs at least a citation or a detail/PDF link."""
    soup = BeautifulSoup(html or "", "html.parser")
    search_map = search_map or {}
    layout = search_map.get("result_layout") or {}
    row_sel = layout.get("row_selector") or "table tr"
    cols = layout.get("columns") or {}
    link_sel = layout.get("detail_link_selector")
    rows_out = []
    for tr in soup.select(row_sel):
        cells = tr.find_all(["td", "th"])
        if not cells or tr.find("th") and not tr.find("td"):
            continue
        texts = [c.get_text(" ", strip=True) for c in cells]
        row: Dict[str, Any] = {"citation": None, "title": None, "court": None, "date": None, "detail_url": None, "pdf_url": None, "case_id": None}
        if cols:
            for k in ("citation", "title", "court", "date"):
                idx = cols.get(k)
                if isinstance(idx, int) and idx < len(texts):
                    row[k] = texts[idx] or None
        else:
            for t in texts:
                if extract_citations(t) and not row["citation"]:
                    row["citation"] = normalise_citation(extract_citations(t)[0]["raw"])
                elif TITLE_INLINE.search(t) and not row["title"]:
                    row["title"] = t
            if not row["title"] and len(texts) > 1:
                row["title"] = texts[1]
            if len(texts) > 2 and not row["court"]:
                row["court"] = texts[2]
        anchors = tr.select(link_sel) if link_sel else tr.find_all("a", href=True)
        for a in anchors:
            href = urljoin(base_url, a.get("href", ""))
            if href.lower().endswith(".pdf"):
                row["pdf_url"] = row["pdf_url"] or href
            else:
                row["detail_url"] = row["detail_url"] or href
        if not row["detail_url"]:
            read_control = tr.select_one("input.courtWiseSearchBtn[casetypeid], .courtWiseSearchBtn[casetypeid], [casetypeid]")
            case_type_id = (read_control.get("casetypeid") or "").strip() if read_control else ""
            if not case_type_id:
                m_case = re.search(r"casetypeid\s*=\s*['\"]?([^'\"\s>]+)", str(tr), flags=re.IGNORECASE)
                if m_case:
                    case_type_id = m_case.group(1).strip()
            if case_type_id:
                parsed = urlsplit(base_url or "")
                origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
                row["detail_url"] = f"{origin}/Login/ReferenceCaseLawSearch?CaseName={quote(case_type_id)}&court=&Row=0&bookName=undefined"
        m = re.search(r"\b(\d{4}[A-Z]{1,3}\d{2,})\b", tr.decode())
        if m:
            row["case_id"] = m.group(1)
        if row["citation"] and not extract_citations(row["citation"]):
            row["citation"] = row["citation"] or None
        if row["citation"] or row["detail_url"] or row["pdf_url"] or row["case_id"]:
            rows_out.append(row)
    next_page = None
    pag = (search_map.get("pagination") or {}).get("next_selector")
    nxt = soup.select_one(pag) if pag else soup.find("a", string=re.compile(r"(?i)^\s*(next|›|»|>)\s*$"))
    if nxt and nxt.get("href"):
        next_page = urljoin(base_url, nxt["href"])
    page_no = None
    mp = re.search(r"(?i)page\s+(\d+)", soup.get_text(" ", strip=True)[:3000])
    if mp:
        page_no = int(mp.group(1))
    total = None
    mtot = re.search(r"(?i)(\d[\d,]*)\s+(?:results?|records?|cases?)\s+found", soup.get_text(" ", strip=True)[:3000])
    if mtot:
        total = int(mtot.group(1).replace(",", ""))
    data = SearchResultExtraction(result_rows=rows_out, next_page=next_page, page_number=page_no, total_results_if_shown=total, field_evidence={"result_rows": f"{len(rows_out)} rows via {row_sel}"}, extractor_confidence=0.95 if rows_out else 0.3)
    return data.model_dump(mode="json")


ROLE_HINTS = {
    "reporter": ("book", "journal", "reporter", "law_report"),
    "year": ("year",),
    "page": ("page", "pg", "citation_no", "cite_no"),
    "court": ("court",),
    "statute": ("statute", "act", "law"),
    "section": ("section", "sec"),
    "keyword": ("keyword", "search", "q", "party", "text", "query", "words"),
    "judge": ("judge",),
}


def introspect_search_form(html: str) -> Dict[str, Any]:
    """Deterministic search-form introspection (Step 0). Returns a SearchFormMapExtraction dict."""
    soup = BeautifulSoup(html or "", "html.parser")
    fields = []
    for el in soup.find_all(["input", "select", "textarea", "button"]):
        name = el.get("name") or el.get("id")
        if not name:
            continue
        tag = el.name
        itype = (el.get("type") or ("select" if tag == "select" else "text")).lower()
        kind = "select" if tag == "select" else ("submit" if itype in ("submit", "button") or tag == "button" else itype if itype in ("text", "checkbox", "radio", "hidden") else "text")
        options = [o.get("value") or o.get_text(strip=True) for o in el.find_all("option")] if tag == "select" else []
        role = None
        low = name.lower()
        for r, hints in ROLE_HINTS.items():
            if any(h in low for h in hints):
                role = r
                break
        if kind == "submit":
            role = "submit"
        selector = f"{tag}[name=\"{el.get('name')}\"]" if el.get("name") else f"#{el.get('id')}"
        fields.append({"name": name, "selector": selector, "kind": kind, "options": [str(o) for o in options][:200], "role": role})
    table = soup.find("table")
    row_sel = None
    columns: Dict[str, int] = {}
    if table:
        tid = table.get("id")
        row_sel = f"table#{tid} tr" if tid else "table tr"
        headers = [th.get_text(" ", strip=True).lower() for th in table.find_all("th")]
        for i, h in enumerate(headers):
            for k in ("citation", "title", "court", "date"):
                if k in h and k not in columns:
                    columns[k] = i
    nxt = soup.find("a", string=re.compile(r"(?i)^\s*(next|›|»|>)\s*$"))
    next_sel = None
    if nxt:
        next_sel = f"a#{nxt.get('id')}" if nxt.get("id") else ("a." + ".".join(nxt.get("class")) if nxt.get("class") else "a[rel=next]")
    data = SearchFormMapExtraction(fields=fields, result_row_selector=row_sel, result_columns=columns, pagination_next_selector=next_sel, page_size=None, detail_link_selector="a[href]" if table else None, extractor_confidence=0.9 if fields else 0.2)
    return data.model_dump(mode="json")
