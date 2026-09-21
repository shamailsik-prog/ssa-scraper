"""Pull-off harvest layers: adapter policy and citation → Tier-1 volume mapping."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scraper.config import Settings
from scraper.harvest_layers import describe_layers, should_use_citation_grid, surface_adapter, volume_from_citation

BASE = dict(DATABASE_URL="postgresql+asyncpg://x:y@localhost/db", REDIS_URL="redis://localhost/0")


def test_surface_adapter_settings_validate():
    assert Settings(**BASE, PLS_SURFACE_ADAPTER="FORM").PLS_SURFACE_ADAPTER == "form"
    assert Settings(**BASE, PLS_SURFACE_ADAPTER="citation_grid").PLS_SURFACE_ADAPTER == "citation_grid"
    with pytest.raises(ValidationError):
        Settings(**BASE, PLS_SURFACE_ADAPTER="stealth")


def test_should_use_citation_grid_respects_form_pull_off(monkeypatch):
    from scraper import harvest_layers

    monkeypatch.setattr(harvest_layers.settings, "PLS_SURFACE_ADAPTER", "form")
    assert should_use_citation_grid(is_grid_map=True) is False
    monkeypatch.setattr(harvest_layers.settings, "PLS_SURFACE_ADAPTER", "citation_grid")
    assert should_use_citation_grid(is_grid_map=False) is True
    monkeypatch.setattr(harvest_layers.settings, "PLS_SURFACE_ADAPTER", "auto")
    assert should_use_citation_grid(is_grid_map=True) is True
    assert should_use_citation_grid(is_grid_map=False) is False


def test_volume_from_citation_reads_reporter_year_page():
    assert volume_from_citation("PLD 2024 SC 401") == {"reporter": "PLD", "year": 2024, "page": 401}
    assert volume_from_citation("CLC 2024 Lah 602") == {"reporter": "CLC", "year": 2024, "page": 602}
    assert volume_from_citation("") is None


def test_describe_layers_names_pull_off_switches():
    snapshot = describe_layers("updates")
    assert snapshot["base"] == "spec_frontier"
    assert snapshot["surface_adapter"] == surface_adapter()
    assert "PLS_SURFACE_ADAPTER=form" in snapshot["how_to_pull_off"]["surface_adapter"]
    assert snapshot["harvest_pacing"] == "updates"
