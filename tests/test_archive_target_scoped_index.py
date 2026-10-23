"""Archive index rows must not cross target access-policy boundaries.

A restricted judgment may be mirrored to an explicitly authorized target, but
its citation/title/hash must never appear in another target's index CSV when
that second target forbids login-session judgments.
"""

import csv

import pytest

from scraper.config import settings
from scraper.models import ArchiveTarget
from scraper.storage.archive import ArchiveMirror, judgment_prefix
from tests.test_public_security_pdf_archive import _promote_one


def _index_rows(root):
    index_dir = root / "_index"
    files = list(index_dir.glob("judgments-*.csv")) if index_dir.exists() else []
    assert len(files) <= 1, "one mirror pass should write at most one index per target"
    if not files:
        return []
    with files[0].open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


@pytest.mark.asyncio
@pytest.mark.parametrize("include_public_judgment", [False, True])
async def test_index_rows_are_scoped_to_target_policy(
    db, source, tmp_path, monkeypatch, include_public_judgment
):
    restricted = await _promote_one(
        db, source, "PLD 2024 SC 911", access_method="login_session"
    )
    public = None
    if include_public_judgment:
        public = await _promote_one(
            db, source, "PLD 2024 SC 912", access_method="public"
        )

    permitted_root = tmp_path / "permitted"
    public_root = tmp_path / "public_only"
    db.add_all(
        [
            ArchiveTarget(
                name="a-permitted",
                target_type="local_path",
                root_path=str(permitted_root),
                mirror_login_session_rows=True,
            ),
            ArchiveTarget(
                name="z-public-only",
                target_type="local_path",
                root_path=str(public_root),
                mirror_login_session_rows=False,
            ),
        ]
    )
    await db.commit()
    monkeypatch.setattr(settings, "MIRROR_LOGIN_SESSION_ROWS", True)

    result = await ArchiveMirror(db).mirror_pending(
        limit=2, backlog_limit=0
    )
    await db.commit()

    permitted_rows = _index_rows(permitted_root)
    public_rows = _index_rows(public_root)
    permitted_citations = {row["canonical_citation"] for row in permitted_rows}
    public_citations = {row["canonical_citation"] for row in public_rows}

    assert restricted.canonical_citation in permitted_citations
    assert restricted.canonical_citation not in public_citations
    assert (permitted_root / judgment_prefix(restricted) / "judgment.txt").exists()
    assert not (public_root / judgment_prefix(restricted)).exists()
    assert result["summary"]["z-public-only"]["skipped_policy"] >= 1

    if public is None:
        assert not public_rows, "an entirely excluded target must have no index"
    else:
        assert public.canonical_citation in permitted_citations
        assert public_citations == {public.canonical_citation}
        assert (public_root / judgment_prefix(public) / "judgment.txt").exists()
