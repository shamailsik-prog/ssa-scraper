"""
Strict Pydantic v2 schemas for every ScrapeGraph extraction (Amendment §5).

These models are the ONLY shapes an AI engine may return. Unknown keys are rejected, absent
fields are null / []. The JSON Schema exported from each model is what is sent to the engine
as the output contract.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = 1


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @classmethod
    def json_schema(cls) -> Dict[str, Any]:
        return cls.model_json_schema()


class StatuteCitedRef(_Strict):
    statute_name: Optional[str] = None
    section_number: Optional[str] = None


class JudgmentExtraction(_Strict):
    citations: List[str] = Field(default_factory=list)
    case_title: Optional[str] = None
    court: Optional[str] = None
    judge_names: List[str] = Field(default_factory=list)
    bench_size: Optional[int] = None
    bench_type: Optional[Literal["single", "division", "full", "larger"]] = None
    decision_date: Optional[date] = None
    year: Optional[int] = None
    full_text_candidate: Optional[str] = None
    headnotes: Optional[str] = None
    statutes_cited: List[StatuteCitedRef] = Field(default_factory=list)
    citations_cited: List[str] = Field(default_factory=list)
    source_document_links: List[str] = Field(default_factory=list)
    pdf_links: List[str] = Field(default_factory=list)
    field_evidence: Dict[str, str] = Field(default_factory=dict)
    extractor_confidence: float = 0.0

    @field_validator("extractor_confidence")
    @classmethod
    def _clamp(cls, v: float) -> float:
        return max(0.0, min(1.0, float(v or 0.0)))

    @field_validator("year")
    @classmethod
    def _year(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return None
        return v if 1947 <= int(v) <= 2100 else None


class StatuteSectionExtraction(_Strict):
    statute_name: Optional[str] = None
    short_name: Optional[str] = None
    section_number: Optional[str] = None
    section_title: Optional[str] = None
    section_text: Optional[str] = None
    chapter: Optional[str] = None
    year_enacted: Optional[int] = None
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None
    amending_instrument: Optional[str] = None
    jurisdiction: Optional[str] = None
    field_evidence: Dict[str, str] = Field(default_factory=dict)
    extractor_confidence: float = 0.0


class StatuteExtraction(_Strict):
    """A statute page: its identity plus every section on the page."""

    statute_name: Optional[str] = None
    short_name: Optional[str] = None
    jurisdiction: Optional[str] = None
    year_enacted: Optional[int] = None
    statute_type: Optional[str] = None
    sections: List[StatuteSectionExtraction] = Field(default_factory=list)
    field_evidence: Dict[str, str] = Field(default_factory=dict)
    extractor_confidence: float = 0.0


class InstrumentExtraction(_Strict):
    type: Optional[str] = None
    number: Optional[str] = None
    date: Optional[date] = None
    title: Optional[str] = None
    gazette_ref: Optional[str] = None
    full_text: Optional[str] = None
    affected_statute: Optional[str] = None
    affected_sections: List[str] = Field(default_factory=list)
    field_evidence: Dict[str, str] = Field(default_factory=dict)
    extractor_confidence: float = 0.0


class SearchResultRow(_Strict):
    citation: Optional[str] = None
    title: Optional[str] = None
    court: Optional[str] = None
    date: Optional[str] = None
    detail_url: Optional[str] = None
    pdf_url: Optional[str] = None
    case_id: Optional[str] = None


class SearchResultExtraction(_Strict):
    result_rows: List[SearchResultRow] = Field(default_factory=list)
    next_page: Optional[str] = None
    page_number: Optional[int] = None
    total_results_if_shown: Optional[int] = None
    field_evidence: Dict[str, str] = Field(default_factory=dict)
    extractor_confidence: float = 0.0


class SearchFormField(_Strict):
    name: str
    selector: str
    kind: Literal["text", "select", "checkbox", "radio", "hidden", "submit"]
    options: List[str] = Field(default_factory=list)
    role: Optional[str] = None  # reporter|year|page|court|statute|section|keyword|citation_no|submit


class SearchFormMapExtraction(_Strict):
    fields: List[SearchFormField] = Field(default_factory=list)
    result_row_selector: Optional[str] = None
    result_columns: Dict[str, int] = Field(default_factory=dict)
    pagination_next_selector: Optional[str] = None
    page_size: Optional[int] = None
    detail_link_selector: Optional[str] = None
    extractor_confidence: float = 0.0


EXTRACTION_TYPES: Dict[str, type] = {
    "judgment": JudgmentExtraction,
    "statute": StatuteExtraction,
    "instrument": InstrumentExtraction,
    "result_rows": SearchResultExtraction,
    "search_form_map": SearchFormMapExtraction,
}
