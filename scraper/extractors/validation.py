"""
Legal validation and reconciliation (Amendment §7-D/E/F, §13). AI may assist, never invent.

Every AI-supplied field is accepted only when it is supported by raw evidence; otherwise it is
rejected, a conflict is logged and confidence is lowered. Deterministic citation and statute
patterns are authoritative over AI output. full_text always comes from the preserved source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from scraper.extractors.deterministic import date_in_text
from scraper.extractors.judgment_guards import detect_judgment_stub, guard_reason
from scraper.fetchers import canonical_text_hash
from scraper.parsers.bench_parser import bench_type_for_size, normalise_judge_name
from scraper.parsers.citation_extractor import extract_citations, normalise_citation

MANDATORY_JUDGMENT_FIELDS = ("citations", "court", "year", "full_text_candidate")
MANDATORY_STATUTE_FIELDS = ("statute_name", "sections")
MANDATORY_INSTRUMENT_FIELDS = ("type", "full_text")
JUDGE_NAME_CHROME_RE = re.compile(r"(?i)(obtaining\s+subscription|update\s+subscriber|^\s*read\s*$)")


@dataclass
class ValidationOutcome:
    data: Dict[str, Any]
    confidence: float
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    quarantine: bool = False
    quarantine_reason: Optional[str] = None


def _norm_ws(s: Optional[str]) -> str:
    return " ".join((s or "").split()).lower()


def citation_in_raw(citation: str, raw_text: str) -> bool:
    """True when the normalised citation appears in the raw text (normalising the raw occurrences)."""
    if not citation or not raw_text:
        return False
    target = normalise_citation(citation)
    if not target:
        return False
    for c in extract_citations(raw_text):
        if (c.get("normalized") or normalise_citation(c["raw"])) == target:
            return True
    # tolerate spacing differences in the raw
    return _norm_ws(target) in _norm_ws(raw_text)


def court_in_directory(court: Optional[str], directory: Dict[str, str]) -> Optional[str]:
    """Map a court string through the directory of {alias_lower: canonical_name}. None when unknown."""
    if not court:
        return None
    key = _norm_ws(court).replace(".", "")
    if key in directory:
        return directory[key]
    for alias, canon in directory.items():
        if alias and (alias in key or key in alias):
            return canon
    return None


def mandatory_present(data: Dict[str, Any], fields: Tuple[str, ...]) -> bool:
    for f in fields:
        v = data.get(f)
        if v is None or v == "" or v == []:
            return False
    return True


def _is_judge_chrome_noise(name: str) -> bool:
    return bool(JUDGE_NAME_CHROME_RE.search(name or ""))


def _clean_judge_names(names: List[Any]) -> List[str]:
    cleaned: List[str] = []
    seen = set()
    for value in names or []:
        normalized = normalise_judge_name(str(value))
        if not normalized:
            continue
        if _is_judge_chrome_noise(normalized):
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(normalized)
    return cleaned


# --------------------------------------------------------------------------- judgment
def reconcile_judgment(
    *,
    deterministic: Dict[str, Any],
    ai: Optional[Dict[str, Any]],
    raw_text: str,
    source_url: Optional[str] = None,
    raw_html: Optional[str] = None,
    court_directory: Dict[str, str],
    min_confidence: float,
) -> ValidationOutcome:
    conflicts: List[Dict[str, Any]] = []
    errors: List[str] = []
    out: Dict[str, Any] = dict(deterministic)
    out.pop("_bench_conflict", None)
    raw_hash_before = canonical_text_hash(raw_text)

    # G. full_text always from preserved source
    out["full_text_candidate"] = raw_text

    # E. deterministic citations are authoritative; AI citations only if present in raw
    det_cits = [normalise_citation(c) for c in deterministic.get("citations") or [] if c]
    cits = list(dict.fromkeys(c for c in det_cits if c))
    if ai:
        for c in ai.get("citations") or []:
            n = normalise_citation(str(c))
            if not n:
                continue
            if n in cits:
                continue
            if citation_in_raw(n, raw_text) or (deterministic.get("field_evidence", {}).get("citations", "").find(n) >= 0):
                cits.append(n)
            else:
                conflicts.append({"field": "citations", "ai": n, "reason": "absent from raw source"})
        if det_cits and ai.get("citations"):
            ai_norm = [normalise_citation(str(c)) for c in ai["citations"]]
            if ai_norm and ai_norm[0] and ai_norm[0] != det_cits[0] and ai_norm[0] not in det_cits:
                conflicts.append({"field": "primary_citation", "deterministic": det_cits[0], "ai": ai_norm[0], "reason": "deterministic parser wins"})
    out["citations"] = cits

    # citations_cited: re-scanned deterministically from full_text
    rescanned = []
    for c in extract_citations(raw_text):
        n = c.get("normalized") or normalise_citation(c["raw"])
        if n and n not in cits and n not in rescanned:
            rescanned.append(n)
    out["citations_cited"] = rescanned

    # court via directory
    court_raw = out.get("court")
    if ai and ai.get("court") and not court_raw:
        cand = str(ai["court"])
        if _norm_ws(cand) in _norm_ws(raw_text):
            court_raw = cand
        else:
            conflicts.append({"field": "court", "ai": cand, "reason": "not in source evidence"})
    canon = court_in_directory(court_raw, court_directory) if court_raw else None
    out["court"] = canon or court_raw
    out["court_canonical"] = canon
    if court_raw and not canon:
        errors.append(f"court '{court_raw}' not in court directory")

    # decision_date: must exist in raw evidence
    d = out.get("decision_date")
    if ai and ai.get("decision_date") and not d:
        try:
            cand = date.fromisoformat(str(ai["decision_date"]))
        except ValueError:
            cand = None
        if cand and date_in_text(cand, raw_text):
            out["decision_date"] = cand.isoformat()
            out.setdefault("field_evidence", {})["decision_date"] = ai.get("field_evidence", {}).get("decision_date", "ai, verified in raw")
        elif cand:
            conflicts.append({"field": "decision_date", "ai": cand.isoformat(), "reason": "absent from raw evidence"})

    # year plausibility and agreement with citation
    year = out.get("year")
    if ai and ai.get("year") and not year:
        y = int(ai["year"])
        if cits and any(str(y) in c for c in cits):
            year = y
        elif re.search(rf"\b{y}\b", raw_text[:20000]):
            year = y
        else:
            conflicts.append({"field": "year", "ai": y, "reason": "not supported by citation or raw"})
    if year and cits:
        cit_years = [int(m.group(0)) for c in cits for m in [re.search(r"\b(19|20)\d{2}\b", c)] if m]
        if cit_years and year not in cit_years:
            errors.append(f"year {year} disagrees with citation year {cit_years[0]}")
            year = cit_years[0]
    if year and not (1947 <= int(year) <= 2100):
        errors.append(f"implausible year {year}")
        year = None
    out["year"] = year

    # judges / bench
    judges = _clean_judge_names(list(out.get("judge_names") or []))
    if ai and ai.get("judge_names") and not judges:
        for j in ai["judge_names"]:
            nj = normalise_judge_name(str(j))
            if _is_judge_chrome_noise(nj):
                continue
            if nj and _norm_ws(nj.split()[-1]) in _norm_ws(raw_text[:20000]):
                judges.append(nj)
            else:
                conflicts.append({"field": "judge_names", "ai": j, "reason": "not in source evidence"})
    out["judge_names"] = _clean_judge_names(judges)
    bench_size = out.get("bench_size")
    if judges and bench_size is not None and bench_size != len(judges):
        explicit = (out.get("field_evidence") or {}).get("bench_size", "")
        if not re.search(r"(?i)(member|judge)s?\s+bench|full bench|larger bench|division bench", explicit or ""):
            errors.append(f"bench_size {bench_size} != judge count {len(judges)}; using judge count")
            bench_size = len(judges)
            out["bench_type"] = bench_type_for_size(bench_size)
    elif judges and bench_size is None:
        bench_size = len(judges)
        out["bench_type"] = out.get("bench_type") or bench_type_for_size(bench_size)
    out["bench_size"] = bench_size
    if deterministic.get("_bench_conflict"):
        errors.append(deterministic["_bench_conflict"])
    stub_signal = detect_judgment_stub(
        source_url=source_url,
        raw_text=raw_text,
        raw_html=raw_html,
        judge_names=out.get("judge_names"),
    )
    if stub_signal is not None:
        errors.append(guard_reason(stub_signal))

    # case title must be supported by heading / result row
    title = out.get("case_title")
    if ai and ai.get("case_title") and not title:
        cand = str(ai["case_title"])
        if _norm_ws(cand)[:60] in _norm_ws(raw_text[:8000]) or _norm_ws(cand) in _norm_ws(str((deterministic.get("field_evidence") or {}).get("case_title", ""))):
            title = cand
        else:
            conflicts.append({"field": "case_title", "ai": cand, "reason": "not supported by heading or result row"})
    out["case_title"] = title

    # statutes must be found in raw text
    statutes = list(out.get("statutes_cited") or [])
    if ai:
        for s in ai.get("statutes_cited") or []:
            name = (s or {}).get("statute_name")
            sec = (s or {}).get("section_number")
            if not (name or sec):
                continue
            key = {"statute_name": name, "section_number": str(sec) if sec is not None else None}
            if key in statutes:
                continue
            ok = True
            if sec is not None and not re.search(rf"(?i)\b(?:s\.?|section|sec\.?|art\.?|article|order|rule)\s*{re.escape(str(sec))}\b", raw_text):
                ok = False
            if name and _norm_ws(name)[:40] not in _norm_ws(raw_text):
                ok = False
            if ok:
                statutes.append(key)
            else:
                conflicts.append({"field": "statutes_cited", "ai": key, "reason": "not found in raw text"})
    out["statutes_cited"] = statutes

    # headnotes only if present verbatim-ish
    if ai and ai.get("headnotes") and not out.get("headnotes"):
        hn = str(ai["headnotes"])
        if _norm_ws(hn)[:80] in _norm_ws(raw_text):
            out["headnotes"] = hn
        else:
            conflicts.append({"field": "headnotes", "reason": "AI headnote not verbatim in source"})

    # links: keep only strings
    for k in ("source_document_links", "pdf_links"):
        merged = list(out.get(k) or [])
        if ai:
            merged += [str(u) for u in (ai.get(k) or []) if isinstance(u, str)]
        out[k] = list(dict.fromkeys(merged))

    # full_text hash invariant
    if canonical_text_hash(out["full_text_candidate"]) != raw_hash_before:
        errors.append("full_text hash changed during reconciliation")
        out["full_text_candidate"] = raw_text

    # confidence
    conf = float(deterministic.get("extractor_confidence") or 0.0)
    if ai and ai.get("extractor_confidence"):
        conf = max(conf, min(float(ai["extractor_confidence"]), conf + 0.2))
    conf -= 0.1 * len(conflicts)
    conf -= 0.05 * len(errors)
    if not mandatory_present(out, MANDATORY_JUDGMENT_FIELDS):
        conf = min(conf, 0.4)
    conf = max(0.0, min(1.0, round(conf, 3)))
    out["extractor_confidence"] = conf
    quarantine = (
        stub_signal is not None
        or conf < min_confidence
        or not out["citations"]
        or not out.get("court")
        or bool([c for c in conflicts if c["field"] in ("primary_citation",)])
    )
    reason = None
    if quarantine:
        if stub_signal is not None:
            reason = guard_reason(stub_signal)
        elif not out["citations"]:
            reason = "no citation supported by source"
        elif not out.get("court"):
            reason = "court unknown"
        elif any(c["field"] == "primary_citation" for c in conflicts):
            reason = "citation conflict between deterministic parser and AI"
        else:
            reason = f"confidence {conf} below threshold {min_confidence}"
    return ValidationOutcome(out, conf, conflicts, errors, quarantine, reason)


# --------------------------------------------------------------------------- statute
def reconcile_statute(*, deterministic: Dict[str, Any], ai: Optional[Dict[str, Any]], raw_text: str, min_confidence: float) -> ValidationOutcome:
    conflicts: List[Dict[str, Any]] = []
    errors: List[str] = []
    out = dict(deterministic)
    norm_raw = _norm_ws(raw_text)
    if ai:
        if ai.get("statute_name") and not out.get("statute_name"):
            if _norm_ws(ai["statute_name"])[:40] in norm_raw:
                out["statute_name"] = ai["statute_name"]
            else:
                conflicts.append({"field": "statute_name", "ai": ai["statute_name"], "reason": "not in raw"})
        for k in ("short_name", "jurisdiction", "statute_type", "year_enacted"):
            if ai.get(k) and not out.get(k):
                if k == "year_enacted" and not re.search(rf"\b{ai[k]}\b", raw_text):
                    conflicts.append({"field": k, "ai": ai[k], "reason": "not in raw"})
                    continue
                out[k] = ai[k]
        # sections: deterministic section numbers are authoritative; AI may supply titles/dates for existing sections
        det_secs = {str(s.get("section_number")): s for s in out.get("sections") or []}
        for s in ai.get("sections") or []:
            num = str((s or {}).get("section_number") or "")
            if not num:
                continue
            if num in det_secs:
                tgt = det_secs[num]
                for k in ("section_title", "effective_from", "effective_to", "amending_instrument", "chapter"):
                    if s.get(k) and not tgt.get(k):
                        ev = str(s.get(k))
                        if k in ("effective_from", "effective_to"):
                            try:
                                dd = date.fromisoformat(ev)
                            except ValueError:
                                conflicts.append({"field": f"section[{num}].{k}", "ai": ev, "reason": "not a date"})
                                continue
                            if not date_in_text(dd, raw_text):
                                conflicts.append({"field": f"section[{num}].{k}", "ai": ev, "reason": "date absent from raw"})
                                continue
                        elif _norm_ws(ev)[:30] not in norm_raw:
                            conflicts.append({"field": f"section[{num}].{k}", "ai": ev, "reason": "not in raw"})
                            continue
                        tgt[k] = ev
            else:
                txt = str(s.get("section_text") or "")
                if txt and _norm_ws(txt)[:80] in norm_raw and re.search(rf"(?m)^\s*{re.escape(num)}\b", raw_text):
                    s = dict(s)
                    s["section_text"] = txt
                    out.setdefault("sections", []).append(s)
                    det_secs[num] = s
                else:
                    conflicts.append({"field": "sections", "ai": num, "reason": "section not verifiable in raw"})
    for s in out.get("sections") or []:
        if s.get("section_text") and _norm_ws(s["section_text"])[:60] not in norm_raw:
            errors.append(f"section {s.get('section_number')} text not found in raw")
    conf = float(out.get("extractor_confidence") or 0.0)
    if ai and ai.get("extractor_confidence"):
        conf = max(conf, min(float(ai["extractor_confidence"]), conf + 0.2))
    conf -= 0.1 * len(conflicts) + 0.05 * len(errors)
    if not mandatory_present(out, MANDATORY_STATUTE_FIELDS):
        conf = min(conf, 0.4)
    conf = max(0.0, min(1.0, round(conf, 3)))
    out["extractor_confidence"] = conf
    q = conf < min_confidence or not out.get("statute_name") or not out.get("sections")
    return ValidationOutcome(out, conf, conflicts, errors, q, ("statute name or sections missing" if (not out.get("statute_name") or not out.get("sections")) else f"confidence {conf} below {min_confidence}") if q else None)


# --------------------------------------------------------------------------- instrument
def _valid_instrument_mentions(
    value: Any,
    *,
    required_keys: tuple[str, ...],
    raw_text: str,
    conflicts: List[Dict[str, Any]],
    field_name: str,
) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        conflicts.append({"field": field_name, "ai": type(value).__name__, "reason": "mentions payload must be a list"})
        return []
    out: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            conflicts.append({"field": field_name, "ai": str(item)[:80], "reason": "mention must be an object"})
            continue
        missing = [k for k in required_keys if not item.get(k)]
        if missing:
            conflicts.append({"field": field_name, "ai": item, "reason": f"missing keys: {', '.join(missing)}"})
            continue
        raw = str(item.get("raw") or "")
        normalized = str(item.get("normalized") or "")
        if not raw or not normalized:
            conflicts.append({"field": field_name, "ai": item, "reason": "raw/normalized cannot be empty"})
            continue
        if _norm_ws(raw) not in _norm_ws(raw_text) and _norm_ws(normalized) not in _norm_ws(raw_text):
            conflicts.append({"field": field_name, "ai": normalized, "reason": "mention absent from raw text"})
            continue
        year = item.get("year")
        if year is not None:
            try:
                y = int(year)
            except (TypeError, ValueError):
                conflicts.append({"field": field_name, "ai": item, "reason": "year is not an integer"})
                continue
            if y < 1800 or y > 2035:
                conflicts.append({"field": field_name, "ai": item, "reason": "year out of accepted range"})
                continue
        out.append(item)
    return out


def reconcile_instrument(*, deterministic: Dict[str, Any], ai: Optional[Dict[str, Any]], raw_text: str, min_confidence: float) -> ValidationOutcome:
    conflicts: List[Dict[str, Any]] = []
    errors: List[str] = []
    out = dict(deterministic)
    out["full_text"] = raw_text
    norm_raw = _norm_ws(raw_text)
    out["citation_mentions"] = _valid_instrument_mentions(
        out.get("citation_mentions"),
        required_keys=("raw", "normalized", "mention_type"),
        raw_text=raw_text,
        conflicts=conflicts,
        field_name="citation_mentions",
    )
    out["statute_mentions"] = _valid_instrument_mentions(
        out.get("statute_mentions"),
        required_keys=("raw", "normalized"),
        raw_text=raw_text,
        conflicts=conflicts,
        field_name="statute_mentions",
    )
    if ai:
        for k in ("type", "number", "title", "gazette_ref", "affected_statute"):
            if ai.get(k) and not out.get(k):
                if _norm_ws(str(ai[k]))[:30] in norm_raw:
                    out[k] = ai[k]
                else:
                    conflicts.append({"field": k, "ai": ai[k], "reason": "not in raw"})
        if ai.get("date") and not out.get("date"):
            try:
                dd = date.fromisoformat(str(ai["date"]))
                if date_in_text(dd, raw_text):
                    out["date"] = dd.isoformat()
                else:
                    conflicts.append({"field": "date", "ai": ai["date"], "reason": "absent from raw"})
            except ValueError:
                conflicts.append({"field": "date", "ai": ai["date"], "reason": "not a date"})
        for sec in ai.get("affected_sections") or []:
            s = str(sec)
            if s not in (out.get("affected_sections") or []):
                if re.search(rf"(?i)\b(?:s\.?|section|sec\.?|art\.?|article)\s*{re.escape(s)}\b", raw_text):
                    out.setdefault("affected_sections", []).append(s)
                else:
                    conflicts.append({"field": "affected_sections", "ai": s, "reason": "not in raw"})
        for mention in _valid_instrument_mentions(
            ai.get("citation_mentions"),
            required_keys=("raw", "normalized", "mention_type"),
            raw_text=raw_text,
            conflicts=conflicts,
            field_name="citation_mentions",
        ):
            if mention not in out["citation_mentions"]:
                out["citation_mentions"].append(mention)
        for mention in _valid_instrument_mentions(
            ai.get("statute_mentions"),
            required_keys=("raw", "normalized"),
            raw_text=raw_text,
            conflicts=conflicts,
            field_name="statute_mentions",
        ):
            if mention not in out["statute_mentions"]:
                out["statute_mentions"].append(mention)
    conf = float(out.get("extractor_confidence") or 0.0)
    if ai and ai.get("extractor_confidence"):
        conf = max(conf, min(float(ai["extractor_confidence"]), conf + 0.2))
    conf -= 0.1 * len(conflicts) + 0.05 * len(errors)
    if not mandatory_present(out, MANDATORY_INSTRUMENT_FIELDS):
        conf = min(conf, 0.4)
    conf = max(0.0, min(1.0, round(conf, 3)))
    out["extractor_confidence"] = conf
    q = conf < min_confidence or not out.get("type")
    return ValidationOutcome(out, conf, conflicts, errors, q, ("instrument type unknown" if not out.get("type") else f"confidence {conf} below {min_confidence}") if q else None)


# --------------------------------------------------------------------------- result rows
def reconcile_result_rows(*, deterministic: Dict[str, Any], ai: Optional[Dict[str, Any]], html: str) -> ValidationOutcome:
    conflicts: List[Dict[str, Any]] = []
    out = dict(deterministic)
    norm_html = _norm_ws(re.sub(r"<[^>]+>", " ", html or ""))
    if ai:
        known = {(r.get("citation"), r.get("detail_url")) for r in out.get("result_rows") or []}
        for r in ai.get("result_rows") or []:
            key = (r.get("citation"), r.get("detail_url"))
            if key in known:
                continue
            cit = r.get("citation")
            if cit and _norm_ws(normalise_citation(str(cit))) not in _norm_ws(normalise_citation(norm_html)) and _norm_ws(str(cit)) not in norm_html:
                conflicts.append({"field": "result_rows.citation", "ai": cit, "reason": "not on page"})
                continue
            if r.get("detail_url") and str(r["detail_url"]) not in (html or ""):
                conflicts.append({"field": "result_rows.detail_url", "ai": r["detail_url"], "reason": "link not on page"})
                continue
            out.setdefault("result_rows", []).append(r)
        if ai.get("next_page") and not out.get("next_page") and str(ai["next_page"]) in (html or ""):
            out["next_page"] = ai["next_page"]
    conf = 0.95 if out.get("result_rows") else 0.2
    conf -= 0.05 * len(conflicts)
    out["extractor_confidence"] = max(0.0, round(conf, 3))
    return ValidationOutcome(out, out["extractor_confidence"], conflicts, [], False, None)
