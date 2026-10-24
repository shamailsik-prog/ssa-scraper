"""Reconcile-time quality errors that must hard-quarantine when SSA_RECONCILE_ERROR_QUARANTINE is on."""

from __future__ import annotations


def reconcile_error_requires_quarantine(message: str) -> bool:
    """Return True for deny-listed reconcile validation_errors (mojibake, court mismatch, short body, etc.)."""
    msg = (message or "").strip().lower()
    if not msg:
        return False
    if "mojibake" in msg:
        return True
    if "court mismatch" in msg:
        return True
    if "not in court directory" in msg:
        return True
    if "judgment text shorter than" in msg:
        return True
    return False


def first_blocking_reconcile_error(errors: list) -> str | None:
    for err in errors or []:
        text = str(err)
        if reconcile_error_requires_quarantine(text):
            return text
    return None
