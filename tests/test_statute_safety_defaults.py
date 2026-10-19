"""B3: statute safety flags default on; explicit env only disables."""

from __future__ import annotations

import inspect

import pytest
from sqlalchemy import func, select

from scraper.config import Settings, settings
from scraper.models import Statute, StatuteSection
from scraper.tasks.promotion import _resolve_statute_link
from tests.test_b3_statute_promotion import _make_statute_staging
from scraper.tasks.promotion import promote_statute_staging


def test_ssa_flags_default_on_in_settings_singleton():
    assert settings.SSA_STATUTE_NO_MENTION_STUBS is True
    assert settings.SSA_STATUTE_PDF_FOLLOW is True
    assert settings.SSA_STATUTE_NAME_QUALITY is True


def test_ssa_flags_default_on_fresh_settings_without_env(monkeypatch):
    for key in ("SSA_STATUTE_NO_MENTION_STUBS", "SSA_STATUTE_PDF_FOLLOW", "SSA_STATUTE_NAME_QUALITY"):
        monkeypatch.delenv(key, raising=False)
    fresh = Settings()
    assert fresh.SSA_STATUTE_NO_MENTION_STUBS is True
    assert fresh.SSA_STATUTE_PDF_FOLLOW is True
    assert fresh.SSA_STATUTE_NAME_QUALITY is True


@pytest.mark.parametrize(
    "env_name,attr",
    [
        ("SSA_STATUTE_NO_MENTION_STUBS", "SSA_STATUTE_NO_MENTION_STUBS"),
        ("SSA_STATUTE_PDF_FOLLOW", "SSA_STATUTE_PDF_FOLLOW"),
        ("SSA_STATUTE_NAME_QUALITY", "SSA_STATUTE_NAME_QUALITY"),
    ],
)
def test_ssa_env_zero_disables_rule(monkeypatch, env_name, attr):
    monkeypatch.setenv(env_name, "0")
    fresh = Settings()
    assert getattr(fresh, attr) is False


@pytest.mark.asyncio
async def test_default_on_no_mention_stub_without_env(db, monkeypatch):
    monkeypatch.delenv("SSA_STATUTE_NO_MENTION_STUBS", raising=False)
    monkeypatch.setattr(settings, "SSA_STATUTE_NO_MENTION_STUBS", True)
    created = await _resolve_statute_link(
        db,
        canonical_name="means the Default On Act",
        source_name="Senate",
        source_url=None,
        jurisdiction="Federal",
    )
    assert created is None
    assert (await db.execute(select(func.count()).select_from(Statute))).scalar() == 0


@pytest.mark.asyncio
async def test_env_false_restores_mention_stub_creation(db, monkeypatch):
    monkeypatch.setattr(settings, "SSA_STATUTE_NO_MENTION_STUBS", False)
    created = await _resolve_statute_link(
        db,
        canonical_name="means the Legacy Stub Act",
        source_name="GazetteOfPakistan",
        source_url=None,
        jurisdiction="Federal",
    )
    assert created is not None
    n = (
        await db.execute(select(func.count()).select_from(StatuteSection).where(StatuteSection.statute_id == created.id))
    ).scalar()
    assert int(n or 0) == 0


@pytest.mark.asyncio
async def test_default_on_no_year_quarantined(db):
    st = await _make_statute_staging(
        db,
        {
            "statute_name": "Sample Compliance Act, 2026",
            "jurisdiction": "Federal",
            "sections": [
                {
                    "section_number": "1",
                    "section_title": "Short title",
                    "section_text": "1. This Act is the Default On Year Gate Act.",
                }
            ],
        },
    )
    assert await promote_statute_staging(db, st) == "quarantined"
    assert "statute_no_year" in (st.quarantine_reason or "")


@pytest.mark.asyncio
async def test_env_false_name_quality_allows_missing_year(db, monkeypatch):
    monkeypatch.setattr(settings, "SSA_STATUTE_NAME_QUALITY", False)
    st = await _make_statute_staging(
        db,
        {
            "statute_name": "Legacy Missing Year Act",
            "jurisdiction": "Federal",
            "sections": [
                {
                    "section_number": "1",
                    "section_title": "Short title",
                    "section_text": "1. This Act is the Legacy Missing Year Act with enough body text.",
                }
            ],
        },
    )
    assert await promote_statute_staging(db, st) == "promoted"


def test_mutant_b3_m2_mention_stubs_config_default_on():
    from scraper import config as config_mod

    field = config_mod.Settings.model_fields["SSA_STATUTE_NO_MENTION_STUBS"]
    assert field.default is True


def test_mutant_b3_m3_pdf_follow_config_default_on():
    from scraper import config as config_mod

    field = config_mod.Settings.model_fields["SSA_STATUTE_PDF_FOLLOW"]
    assert field.default is True


def test_mutant_b3_m4_name_quality_config_default_on():
    from scraper import config as config_mod

    field = config_mod.Settings.model_fields["SSA_STATUTE_NAME_QUALITY"]
    assert field.default is True


def test_mutant_b3_m5_resolve_statute_link_checks_no_stub_flag():
    from scraper.tasks import promotion as promotion_mod

    src = inspect.getsource(promotion_mod._resolve_statute_link)
    assert "if settings.SSA_STATUTE_NO_MENTION_STUBS:" in src
    assert "return None" in src
