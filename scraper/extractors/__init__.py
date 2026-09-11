"""ScrapeGraphAI integration subsystem (Amendment D-15 §4)."""

from scraper.extractors.hybrid_extractor import ExtractionOutcome, HybridExtractor
from scraper.extractors.schemas import (
    SCHEMA_VERSION,
    InstrumentExtraction,
    JudgmentExtraction,
    SearchResultExtraction,
    StatuteExtraction,
    StatuteSectionExtraction,
)
from scraper.extractors.scrapegraph_base import ExtractionInput, PrivacyViolation, StructuredExtractor
from scraper.extractors.scrapegraph_local import LocalScrapeGraphEngine
from scraper.extractors.scrapegraph_managed import ManagedScrapeGraphEngine

__all__ = [
    "SCHEMA_VERSION",
    "ExtractionInput",
    "ExtractionOutcome",
    "HybridExtractor",
    "InstrumentExtraction",
    "JudgmentExtraction",
    "LocalScrapeGraphEngine",
    "ManagedScrapeGraphEngine",
    "PrivacyViolation",
    "SearchResultExtraction",
    "StatuteExtraction",
    "StatuteSectionExtraction",
    "StructuredExtractor",
]
