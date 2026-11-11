"""Disposable-database apply, backup, rollback, and refusal tests.

The production allowlist is not extended. Each test patches the allowlist to
the rows it inserts and feeds the recorded bytes itself. Nothing is fetched.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from scraper.models import CorpusMetadata, DocumentQuarantine, Statute, StatuteSection, StatuteSectionVersion
from scraper.tasks.bundled_gazette_reextract import backup_key, require_confirmed_ids

PAT_FEEDER_URL = "https://senate.gov.pk/uploads/documents/1660623691_478.pdf|XXXIX of 1976"
_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "bundled_gazette" / "s478.txt"


def _pdf(text: str) -> bytes:
    return b"%PDF-1.4\n" + text.encode("utf-8")


def _pin_ids(monkeypatch, pins: dict[UUID, str]) -> None:
    import scraper.tasks.bundled_gazette_acts as acts

    monkeypatch.setattr(acts, "CONFIRMED_BLEED_STATUTE_IDS", tuple(pins))
    monkeypatch.setattr(acts, "CONFIRMED_PDF_SHA256", dict(pins))
    monkeypatch.setattr(acts, "APPROVED_REEXTRACT_IDS", frozenset(pins))


async def _add_statute(db, *, name: str, url: str, sections: list[dict], statute_id: UUID | None = None):
    statute = Statute(
        name=name,
        jurisdiction="Federal",
        source_name="Senate",
        source_url=url,
        year_enacted=1976,
        needs_section_refetch=False,
        **({"id": statute_id} if statute_id is not None else {}),
    )
    db.add(statute)
    await db.flush()
    for index, section in enumerate(sections):
        row = StatuteSection(
            statute_id=statute.id,
            section_number=section["section_number"],
            section_title=section.get("section_title"),
            sort_key=index,
        )
        db.add(row)
        await db.flush()
        version = StatuteSectionVersion(
            section_id=row.id,
            version_no=1,
            section_text=section["section_text"],
            text_hash=hashlib.sha256(section["section_text"].encode("utf-8")).hexdigest(),
        )
        db.add(version)
        await db.flush()
        row.current_version_id = version.id
    statute_id = statute.id
    await db.commit()
    return statute_id


async def _bodies(db, statute_id: UUID) -> list[tuple]:
    db.expire_all()
    rows = (
        await db.execute(
            select(StatuteSection)
            .where(StatuteSection.statute_id == statute_id)
            .order_by(StatuteSection.sort_key, StatuteSection.section_number)
        )
    ).scalars().all()
    found = []
    for row in rows:
        text = ""
        if row.current_version_id is not None:
            version = (
                await db.execute(select(StatuteSectionVersion).where(StatuteSectionVersion.id == row.current_version_id))
            ).scalars().first()
            text = version.section_text if version is not None else ""
        found.append((row.id, row.section_number, text))
    return found


async def _section_times(db, statute_id: UUID) -> list[tuple]:
    db.expire_all()
    rows = (
        await db.execute(
            select(StatuteSection)
            .where(StatuteSection.statute_id == statute_id)
            .order_by(StatuteSection.sort_key, StatuteSection.section_number)
        )
    ).scalars().all()
    found = []
    for row in rows:
        version_created = None
        if row.current_version_id is not None:
            version = (
                await db.execute(select(StatuteSectionVersion).where(StatuteSectionVersion.id == row.current_version_id))
            ).scalars().first()
            version_created = version.created_at if version is not None else None
        found.append((row.id, row.created_at, row.updated_at, version_created))
    return found


async def _quarantine(db, statute_id: UUID):
    db.expire_all()
    return (
        await db.execute(
            select(DocumentQuarantine).where(
                DocumentQuarantine.doc_type == "statute",
                DocumentQuarantine.doc_id == statute_id,
            )
        )
    ).scalars().first()


def test_prefix_and_unhyphenated_ids_are_refused_before_a_write():
    from scraper.tasks.bundled_gazette_acts import main
    from scraper.tasks.bundled_gazette_reextract import parse_explicit_statute_id

    with pytest.raises(ValueError):
        parse_explicit_statute_id("6c73a686")
    with pytest.raises(ValueError):
        parse_explicit_statute_id("6c73a686c03846118f058a429168c8a7")
    with pytest.raises(SystemExit) as prefix:
        main(["--apply-reextract", "--statute-id", "6c73a686"])
    with pytest.raises(SystemExit) as bare:
        main(["--apply-reextract", "--statute-id", "6c73a686c03846118f058a429168c8a7"])
    with pytest.raises(SystemExit) as combined:
        main(["--apply-reextract", "--apply", "--statute-id", "6c73a686-c038-4611-8f05-8a429168c8a7"])
    assert prefix.value.code == 2
    assert bare.value.code == 2
    assert combined.value.code == 2


def test_an_empty_pin_is_not_an_authentic_allowlist_entry(monkeypatch):
    statute_id = UUID("11111111-1111-1111-1111-111111111111")
    _pin_ids(monkeypatch, {statute_id: ""})
    with pytest.raises(ValueError, match="allowlisted"):
        require_confirmed_ids([statute_id])


@pytest.mark.asyncio
async def test_apply_backs_up_and_rolls_back_only_that_statute(db, tmp_path, monkeypatch):
    from scraper.tasks.bundled_gazette_reextract import (
        apply_confirmed_reextract,
        lift_confirmed_quarantine,
        rollback_reextract,
    )

    text = _FIXTURE.read_text(encoding="utf-8")
    pdf = _pdf(text)
    digest = hashlib.sha256(pdf).hexdigest()
    target_id = await _add_statute(
        db,
        name="Apply target Pat Feeder",
        url=PAT_FEEDER_URL,
        sections=[
            {"section_number": "2", "section_text": "BLEED FROM THE OTHER ACT and nothing of Pat Feeder."},
            {"section_number": "59", "section_text": "59. Footer stored as a section from the gazette chrome."},
        ],
    )
    control_id = await _add_statute(
        db,
        name="Apply control untouched",
        url="https://example.test/control.pdf|I of 1970",
        sections=[{"section_number": "1", "section_text": "Control section stays exactly as stored."}],
    )
    db.add(
        DocumentQuarantine(
            doc_type="statute",
            doc_id=target_id,
            reason="bundled_gazette_act_bleed",
            run_id="older-run",
        )
    )
    await db.commit()
    _pin_ids(monkeypatch, {target_id: digest})
    before = await _bodies(db, target_id)
    times_before = await _section_times(db, target_id)
    control_before = await _bodies(db, control_id)
    calls = {"fetch": 0}

    async def fetch_pdf(_url):
        calls["fetch"] += 1
        raise AssertionError("apply-reextract must not fetch")

    monkeypatch.setattr("scraper.tasks.bundled_gazette_acts.fetch_gazette_pdf", fetch_pdf)

    async def loader(_url):
        return pdf

    def extract(_data):
        return text

    report = await apply_confirmed_reextract(
        statute_ids=[target_id],
        expected_sections={target_id: ["1", "2"]},
        backup_dir=tmp_path,
        recorded_loader=loader,
        extract_text=extract,
    )
    assert calls["fetch"] == 0
    assert report["writes"] == 1
    assert report["refused"] is False
    assert report["run_id"]
    applied = await _bodies(db, target_id)
    assert [number for _id, number, _text in applied] == ["1", "2"]
    assert applied[-1][2].rstrip().endswith("1976.")
    assert "BLEED FROM THE OTHER ACT" not in applied[-1][2]
    assert {row[0] for row in applied}.isdisjoint({row[0] for row in before})
    assert await _bodies(db, control_id) == control_before
    quarantine = await _quarantine(db, target_id)
    assert quarantine is not None
    assert quarantine.run_id == "older-run"
    assert quarantine.reason == "bundled_gazette_act_bleed"
    db.expire_all()
    stored = (await db.execute(select(Statute).where(Statute.id == target_id))).scalars().one()
    assert stored.name == "Apply target Pat Feeder"
    backup = Path(report["backup_path"]).read_text(encoding="utf-8")
    assert "BLEED FROM THE OTHER ACT" in backup
    meta = (
        await db.execute(select(CorpusMetadata).where(CorpusMetadata.key == backup_key(report["run_id"])))
    ).scalars().first()
    assert meta is not None
    assert "BLEED FROM THE OTHER ACT" in meta.value
    meta_count = (await db.execute(select(func.count()).select_from(CorpusMetadata))).scalar()

    again = await apply_confirmed_reextract(
        statute_ids=[target_id],
        expected_sections={target_id: ["1", "2"]},
        backup_dir=tmp_path,
        recorded_loader=loader,
        extract_text=extract,
    )
    assert again["writes"] == 0
    assert again["run_id"] is None
    assert again["rows"][0]["status"] == "unchanged"
    assert await _bodies(db, target_id) == applied
    assert (await db.execute(select(func.count()).select_from(CorpusMetadata))).scalar() == meta_count
    assert list(tmp_path.glob("*.jsonl")) == [Path(report["backup_path"])]

    lifted = await lift_confirmed_quarantine(statute_ids=[target_id], backup_dir=tmp_path)
    assert lifted["writes"] == 1
    assert await _quarantine(db, target_id) is None
    assert await _bodies(db, target_id) == applied
    lifted_again = await lift_confirmed_quarantine(statute_ids=[target_id], backup_dir=tmp_path)
    assert lifted_again["writes"] == 0
    assert lifted_again["run_id"] is None

    restored_lift = await rollback_reextract(lifted["run_id"])
    assert restored_lift["writes"] == 1
    quarantine = await _quarantine(db, target_id)
    assert quarantine is not None
    assert quarantine.run_id == "older-run"
    assert await _bodies(db, target_id) == applied
    assert (await rollback_reextract(lifted["run_id"]))["writes"] == 0

    restored = await rollback_reextract(report["run_id"])
    assert restored["writes"] == 1
    assert await _bodies(db, target_id) == before
    assert await _section_times(db, target_id) == times_before
    quarantine = await _quarantine(db, target_id)
    assert quarantine is not None
    assert quarantine.run_id == "older-run"
    assert await _bodies(db, control_id) == control_before
    assert (await rollback_reextract(report["run_id"]))["writes"] == 0
    assert calls["fetch"] == 0


@pytest.mark.asyncio
async def test_sha_mismatch_refuses_without_fetch_and_a_sibling_can_still_apply(db, tmp_path, monkeypatch):
    from scraper.tasks.bundled_gazette_reextract import apply_confirmed_reextract

    text = _FIXTURE.read_text(encoding="utf-8")
    pdf = _pdf(text)
    digest = hashlib.sha256(pdf).hexdigest()
    good_id = await _add_statute(
        db,
        name="Sha sibling good",
        url=PAT_FEEDER_URL,
        sections=[{"section_number": "9", "section_text": "OLD GOOD SECTION that should be replaced."}],
    )
    bad_id = await _add_statute(
        db,
        name="Sha sibling bad",
        url="https://example.test/bad.pdf|XXXIX of 1976",
        sections=[{"section_number": "9", "section_text": "OLD BAD SECTION that must stay."}],
    )
    _pin_ids(monkeypatch, {good_id: digest, bad_id: digest})
    bad_before = await _bodies(db, bad_id)

    async def fetch_pdf(_url):
        raise AssertionError("sha mismatch must not fetch")

    monkeypatch.setattr("scraper.tasks.bundled_gazette_acts.fetch_gazette_pdf", fetch_pdf)

    async def loader(url):
        if "bad.pdf" in url:
            return b"%PDF-1.4\nDIFFERENT"
        return pdf

    report = await apply_confirmed_reextract(
        statute_ids=[bad_id, good_id],
        backup_dir=tmp_path,
        recorded_loader=loader,
        extract_text=lambda _data: text,
    )
    by_id = {row["statute_id"]: row for row in report["rows"]}
    assert by_id[str(bad_id)]["status"] == "refused"
    assert by_id[str(bad_id)]["reason"] == "sha_mismatch"
    assert by_id[str(bad_id)]["writes"] == 0
    assert by_id[str(good_id)]["status"] == "applied"
    assert await _bodies(db, bad_id) == bad_before
    assert [number for _id, number, _text in await _bodies(db, good_id)] == ["1", "2"]
    assert report["refused"] is True


@pytest.mark.asyncio
async def test_an_id_outside_the_allowlist_writes_nothing(db, tmp_path, monkeypatch):
    from scraper.tasks.bundled_gazette_reextract import apply_confirmed_reextract

    text = _FIXTURE.read_text(encoding="utf-8")
    target_id = await _add_statute(
        db,
        name="Not on the allowlist",
        url=PAT_FEEDER_URL,
        sections=[{"section_number": "1", "section_text": "Stored section stays because the id is refused."}],
    )
    stranger = uuid4()
    _pin_ids(monkeypatch, {stranger: hashlib.sha256(_pdf(text)).hexdigest()})
    before = await _bodies(db, target_id)
    report = await apply_confirmed_reextract(
        statute_ids=[target_id],
        backup_dir=tmp_path,
        recorded_loader=lambda _url: _pdf(text),
        extract_text=lambda _data: text,
    )
    assert report["writes"] == 0
    assert report["backup_path"] is None
    assert report["refused"] is True
    assert report["rows"][0]["status"] == "refused"
    assert report["rows"][0]["reason"] == "not_approved"
    assert await _bodies(db, target_id) == before
    assert list(tmp_path.glob("*.jsonl")) == []


@pytest.mark.asyncio
async def test_held_preview_and_expected_section_mismatch_skip(db, tmp_path, monkeypatch):
    from scraper.tasks.bundled_gazette_reextract import apply_confirmed_reextract

    text = _FIXTURE.read_text(encoding="utf-8")
    pdf = _pdf(text)
    digest = hashlib.sha256(pdf).hexdigest()
    held_id = await _add_statute(
        db,
        name="Held preview row",
        url="https://senate.gov.pk/uploads/documents/1660623691_478.pdf|I of 1999",
        sections=[{"section_number": "1", "section_text": "HELD SECTION stays in place."}],
    )
    mismatched_id = await _add_statute(
        db,
        name="Expected section mismatch",
        url=PAT_FEEDER_URL,
        sections=[{"section_number": "4", "section_text": "MISMATCH SECTION stays in place."}],
    )
    _pin_ids(monkeypatch, {held_id: digest, mismatched_id: digest})
    held_before = await _bodies(db, held_id)
    mismatch_before = await _bodies(db, mismatched_id)

    async def loader(_url):
        return pdf

    report = await apply_confirmed_reextract(
        statute_ids=[held_id, mismatched_id],
        expected_sections={mismatched_id: ["1", "2", "9"]},
        backup_dir=tmp_path,
        recorded_loader=loader,
        extract_text=lambda _data: text,
    )
    by_id = {row["statute_id"]: row for row in report["rows"]}
    assert by_id[str(held_id)]["status"] == "skipped"
    assert by_id[str(held_id)]["reason"]
    assert by_id[str(held_id)]["writes"] == 0
    assert by_id[str(mismatched_id)]["status"] == "skipped"
    assert by_id[str(mismatched_id)]["reason"] == "expected_sections_mismatch"
    assert await _bodies(db, held_id) == held_before
    assert await _bodies(db, mismatched_id) == mismatch_before
    assert report["writes"] == 0
    assert list(tmp_path.glob("*.jsonl")) == []


@pytest.mark.asyncio
async def test_a_failed_replace_leaves_the_sections_and_rewinds_the_backup(db, tmp_path, monkeypatch):
    from scraper.tasks import bundled_gazette_reextract as reextract

    text = _FIXTURE.read_text(encoding="utf-8")
    pdf = _pdf(text)
    target_id = await _add_statute(
        db,
        name="Replace blows up",
        url=PAT_FEEDER_URL,
        sections=[{"section_number": "1", "section_text": "ORIGINAL survives a failed replace."}],
    )
    _pin_ids(monkeypatch, {target_id: hashlib.sha256(pdf).hexdigest()})
    before = await _bodies(db, target_id)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("replace failed")

    monkeypatch.setattr(reextract, "_replace_sections", boom)
    with pytest.raises(RuntimeError, match="replace failed"):
        await reextract.apply_confirmed_reextract(
            statute_ids=[target_id],
            backup_dir=tmp_path,
            recorded_loader=lambda _url: pdf,
            extract_text=lambda _data: text,
        )
    assert await _bodies(db, target_id) == before
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == ""


HIDDEN_BLEED_ID = UUID("d7d3187e-9ebd-4782-aa9a-228b40d2dd96")
APPROVED_PAT_FEEDER_ID = UUID("6c73a686-c038-4611-8f05-8a429168c8a7")
_APPROVED_EIGHT = frozenset(
    {
        UUID("5636b724-6a47-42a7-93f5-4d78e07ddea8"),
        UUID("92aeaf99-7603-41d6-a541-f2c38fe27d04"),
        UUID("21f60373-b62a-4df8-9f0f-947dcf8a352e"),
        UUID("06a5058e-511e-4039-befa-947f1fcdfd68"),
        UUID("6c73a686-c038-4611-8f05-8a429168c8a7"),
        UUID("10cc6efa-f845-42e5-83ee-11eeabd0bc63"),
        UUID("0181ad58-1a79-479c-bdd8-dad584cfd683"),
        UUID("7f0a008c-6775-4a44-87b0-4e0e5c1b5b02"),
    }
)


def test_apply_allowlist_is_the_eight_approved_ids():
    from scraper.tasks.bundled_gazette_acts import APPROVED_REEXTRACT_IDS, CONFIRMED_BLEED_STATUTE_IDS

    assert APPROVED_REEXTRACT_IDS == _APPROVED_EIGHT
    assert len(APPROVED_REEXTRACT_IDS) == 8
    assert HIDDEN_BLEED_ID in CONFIRMED_BLEED_STATUTE_IDS
    assert HIDDEN_BLEED_ID not in APPROVED_REEXTRACT_IDS


def test_an_unapproved_id_exits_2_and_writes_no_backup(tmp_path):
    from scraper.tasks.bundled_gazette_acts import main

    apply_dir = tmp_path / "apply"
    lift_dir = tmp_path / "lift"
    apply_code = main(
        ["--apply-reextract", "--statute-id", str(HIDDEN_BLEED_ID), "--backup-dir", str(apply_dir)]
    )
    lift_code = main(
        ["--lift-quarantine", "--statute-id", str(HIDDEN_BLEED_ID), "--backup-dir", str(lift_dir)]
    )
    assert apply_code == 2
    assert lift_code == 2
    assert list(apply_dir.glob("*.jsonl")) == []
    assert list(lift_dir.glob("*.jsonl")) == []


@pytest.mark.asyncio
async def test_keep_hidden_id_is_refused_before_backup_and_an_approved_id_still_applies(db, tmp_path, monkeypatch):
    import scraper.tasks.bundled_gazette_acts as acts
    from scraper.tasks.bundled_gazette_reextract import apply_confirmed_reextract

    text = _FIXTURE.read_text(encoding="utf-8")
    pdf = _pdf(text)
    digest = hashlib.sha256(pdf).hexdigest()
    hidden_before = await _bodies(
        db,
        await _add_statute(
            db,
            statute_id=HIDDEN_BLEED_ID,
            name="Keep hidden lpbc2nd",
            url="https://example.test/hidden-lpbc.pdf|LIV of 1976",
            sections=[{"section_number": "1", "section_text": "HIDDEN SECTION must stay untouched."}],
        ),
    )
    await _add_statute(
        db,
        statute_id=APPROVED_PAT_FEEDER_ID,
        name="Approved Pat Feeder reextract",
        url=PAT_FEEDER_URL,
        sections=[{"section_number": "9", "section_text": "OLD APPROVED SECTION that the splitter replaces."}],
    )
    pins = dict(acts.CONFIRMED_PDF_SHA256)
    pins[APPROVED_PAT_FEEDER_ID] = digest
    monkeypatch.setattr(acts, "CONFIRMED_PDF_SHA256", pins)

    async def recorded(url):
        if "hidden-lpbc" in url:
            raise AssertionError("a refused id was read before the refusal")
        return pdf

    async def fetch_pdf(_url):
        raise AssertionError("apply-reextract must not fetch")

    monkeypatch.setattr(acts, "fetch_gazette_pdf", fetch_pdf)

    report = await apply_confirmed_reextract(
        statute_ids=[HIDDEN_BLEED_ID, APPROVED_PAT_FEEDER_ID],
        expected_sections={APPROVED_PAT_FEEDER_ID: ["1", "2"]},
        backup_dir=tmp_path,
        recorded_loader=recorded,
        extract_text=lambda _data: text,
    )
    by_id = {row["statute_id"]: row for row in report["rows"]}
    assert report["refused"] is True
    assert report["writes"] == 1
    assert by_id[str(HIDDEN_BLEED_ID)]["status"] == "refused"
    assert by_id[str(HIDDEN_BLEED_ID)]["reason"] == "not_approved"
    assert by_id[str(HIDDEN_BLEED_ID)]["writes"] == 0
    assert by_id[str(APPROVED_PAT_FEEDER_ID)]["status"] == "applied"
    assert await _bodies(db, HIDDEN_BLEED_ID) == hidden_before
    backup = Path(report["backup_path"]).read_text(encoding="utf-8")
    assert str(HIDDEN_BLEED_ID) not in backup
    assert str(APPROVED_PAT_FEEDER_ID) in backup
    assert [number for _id, number, _text in await _bodies(db, APPROVED_PAT_FEEDER_ID)] == ["1", "2"]


@pytest.mark.asyncio
async def test_lift_refuses_keep_hidden_id_before_backup_and_lifts_an_approved_id(db, tmp_path):
    from scraper.tasks.bundled_gazette_reextract import lift_confirmed_quarantine

    hidden_id = await _add_statute(
        db,
        statute_id=HIDDEN_BLEED_ID,
        name="Keep hidden lift",
        url="https://example.test/hidden-lpbc.pdf|LIV of 1976",
        sections=[{"section_number": "1", "section_text": "HIDDEN LIFT SECTION stays."}],
    )
    approved_id = await _add_statute(
        db,
        statute_id=APPROVED_PAT_FEEDER_ID,
        name="Approved lift Pat Feeder",
        url=PAT_FEEDER_URL,
        sections=[{"section_number": "1", "section_text": "APPROVED LIFT SECTION stays."}],
    )
    db.add(DocumentQuarantine(doc_type="statute", doc_id=hidden_id, reason="bundled_gazette_act_bleed", run_id="hidden-run"))
    db.add(DocumentQuarantine(doc_type="statute", doc_id=approved_id, reason="bundled_gazette_act_bleed", run_id="approved-run"))
    await db.commit()
    hidden_before = await _bodies(db, hidden_id)
    approved_before = await _bodies(db, approved_id)

    report = await lift_confirmed_quarantine(
        statute_ids=[HIDDEN_BLEED_ID, APPROVED_PAT_FEEDER_ID],
        backup_dir=tmp_path,
    )
    by_id = {row["statute_id"]: row for row in report["rows"]}
    assert report["refused"] is True
    assert report["writes"] == 1
    assert by_id[str(HIDDEN_BLEED_ID)]["status"] == "refused"
    assert by_id[str(HIDDEN_BLEED_ID)]["reason"] == "not_approved"
    assert by_id[str(HIDDEN_BLEED_ID)]["writes"] == 0
    assert by_id[str(APPROVED_PAT_FEEDER_ID)]["status"] == "lifted"
    hidden_quarantine = await _quarantine(db, hidden_id)
    assert hidden_quarantine is not None
    assert hidden_quarantine.run_id == "hidden-run"
    assert await _quarantine(db, approved_id) is None
    assert await _bodies(db, hidden_id) == hidden_before
    assert await _bodies(db, approved_id) == approved_before
    backup = Path(report["backup_path"]).read_text(encoding="utf-8")
    assert str(HIDDEN_BLEED_ID) not in backup
    assert str(APPROVED_PAT_FEEDER_ID) in backup
