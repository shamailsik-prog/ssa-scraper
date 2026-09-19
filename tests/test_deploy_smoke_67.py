from __future__ import annotations

from pathlib import Path


def test_residual_smoke_checklist_documents_reconcile_inventory():
    text = Path("docs/DEPLOY_SMOKE_67.md").read_text(encoding="utf-8")
    for token in (
        "reconcile_judgment_citation_relations",
        "reconcile_instrument_relations",
        "resolution_status='unresolved'",
        "target_statute_section_id IS NULL",
        "fail_on_increase=True",
        "run_reconcile=False",
        "run_reconcile=True",
    ):
        assert token in text
