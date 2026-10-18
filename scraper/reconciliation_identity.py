"""Fail-closed proof for automatic reported/unreported judgment reconciliation.

This deliberately prefers retaining uncertain copies over deleting a distinct order.
Identical wording is not sufficient without independent court, docket and date.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional


def _docket(value: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _body(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\xa0", " ")).strip().lower()


def same_judicial_order(
    *,
    left_docket: Optional[str],
    right_docket: Optional[str],
    left_date: Optional[date],
    right_date: Optional[date],
    left_court_id: object,
    right_court_id: object,
    left_text: Optional[str],
    right_text: Optional[str],
) -> bool:
    """Require positive, exact legal identity; permit whitespace/case differences only.

    Text similarity, identical text hashes, matching docket alone, and unknown
    court/date are insufficient for an irreversible automatic reconciliation.
    """
    docket = _docket(left_docket)
    body = _body(left_text)
    return bool(
        docket and docket == _docket(right_docket)
        and left_date is not None and right_date is not None
        and left_date == right_date
        and left_court_id is not None and right_court_id is not None
        and left_court_id == right_court_id
        and body and body == _body(right_text)
    )
