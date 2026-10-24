"""Corpus record verification (read-only on contract tables)."""

from scraper.verification.rules import RULES_VERSION, verify_judgment_row, verify_statute_row

__all__ = ["RULES_VERSION", "verify_judgment_row", "verify_statute_row"]
