"""Forensic regression tests for the unreported-judgment reading agent."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select

from scraper.config import settings
from scraper.models import Judgment, JudgmentReading, JudgmentReadingVersion, Notification
from scraper.tasks import unreported_reader
from scraper.tasks.unreported_reader import keep_only_evidenced, read_unreported

TEXT = (
    "ORDER. The applicant seeks post-arrest bail in a case under section 489-F of the Pakistan Penal Code. "
    "The offence does not fall within the prohibitory clause of section 497 of the Code of Criminal Procedure. "
    "The case of the applicant calls for further inquiry within the meaning of section 497(2) Cr.P.C. "
    "The application is allowed and the applicant is admitted to bail."
)

MODEL_ANSWER = {
    "laws": [
        {"statute": "Pakistan Penal Code", "sections": ["489-F"], "quote": "a case under section 489-F of the Pakistan Penal Code"},
        {"statute": "Code of Criminal Procedure", "sections": ["497(2)"], "quote": "further inquiry within the meaning of section 497(2) Cr.P.C."},
        {"statute": "Contract Act 1872", "sections": ["73"], "quote": "damages for breach under section 73"},
    ],
    "points": [
        {"point": "Bail where the offence is outside the prohibitory clause", "decision": "Bail granted: further inquiry", "quote": "The case of the applicant calls for further inquiry"},
        {"point": "Invented point", "decision": "Invented", "quote": "the court overruled PLD 2010 SC 1"},
    ],
    "interpretation": [
        {
            "statement": "Section 497(2) applies because the matter calls for further inquiry.",
            "quote": "The case of the applicant calls for further inquiry within the meaning of section 497(2) Cr.P.C.",
        }
    ],
    "turns_on": [
        {
            "statement": "The decision turns on further inquiry and the offence being outside the prohibitory clause.",
            "quote": "The offence does not fall within the prohibitory clause of section 497 of the Code of Criminal Procedure.",
        }
    ],
    "subjects": ["Bail", "dishonoured cheques (489-F PPC)", "Cheque bouncing law"],
    "outcome": {"value": "allowed", "quote": "The application is allowed and the applicant is admitted to bail."},
}


def _model_answer():
    import copy

    return copy.deepcopy(MODEL_ANSWER)


def test_tags_without_their_words_in_the_judgment_are_dropped():
    kept, dropped = keep_only_evidenced(MODEL_ANSWER, TEXT)
    assert [law["statute"] for law in kept["laws"]] == ["Pakistan Penal Code", "Code of Criminal Procedure"]
    assert [p["point"] for p in kept["points"]] == ["Bail where the offence is outside the prohibitory clause"]
    assert {d["kind"] for d in dropped} == {"law", "point"} and len(dropped) == 2


def test_quotes_are_matched_regardless_of_spacing_and_case():
    kept, _ = keep_only_evidenced(
        {"laws": [{"statute": "PPC", "sections": [], "quote": "UNDER  SECTION\n489-F of the pakistan penal code"}], "points": []},
        TEXT,
    )
    assert len(kept["laws"]) == 1


async def _unreported(db, n=1):
    rows = []
    for i in range(n):
        j = Judgment(
            canonical_citation=f"UNREPORTED SHC Cr. Bail Application No. {i} of 2024 (2024-05-01)",
            full_text_hash=f"{i:064d}",
            full_text=TEXT,
            court_name="High Court of Sindh",
            source_name="SindhHighCourt",
            unreported=True,
            proceeding_type="bail",
            access_method="public",
        )
        db.add(j)
        rows.append(j)
    db.add(
        Judgment(
            canonical_citation="PLD 2020 SC 1",
            full_text_hash="f" * 64,
            full_text=TEXT,
            court_name="Supreme Court of Pakistan",
            reporter="PLD",
            access_method="public",
        )
    )
    await db.commit()
    return rows


async def test_agent_reads_once_and_persists_grounded_current_and_version(db, monkeypatch):
    monkeypatch.setattr(settings, "READER_MODEL", "test-model")
    monkeypatch.setattr(settings, "READER_BASE_URL", "http://reader.test/v1")
    calls = []

    async def fake_model(text):
        calls.append(text)
        ans = _model_answer()
        ans["_reader_meta"] = {
            "provider": "reader.test",
            "input_tokens": 100,
            "output_tokens": 20,
            "latency_ms": 17,
            "parse_repair_used": False,
        }
        return ans

    monkeypatch.setattr(unreported_reader, "call_reader_model", fake_model)
    (j,) = await _unreported(db)

    out = await read_unreported(db, limit=10)
    assert out == {"read": 1, "failed": 0, "locked": 0}
    assert len(calls) == 1
    r = await db.get(JudgmentReading, j.id)
    assert r.status == "read" and r.model == "test-model" and r.outcome == "allowed"
    assert r.prompt_version == unreported_reader.PROMPT_VERSION
    assert r.taxonomy_version == unreported_reader.SUBJECT_TAXONOMY_VERSION
    assert r.judgment_text_hash == j.full_text_hash
    assert r.interpretation_evidence and r.turns_on_evidence and r.outcome_evidence["value"] == "allowed"
    assert [law["statute"] for law in r.laws] == ["Pakistan Penal Code", "Code of Criminal Procedure"]
    assert len(r.points) == 1
    assert r.subjects == ["Bail", "Dishonoured cheques (489-F PPC)"]
    assert any(d["kind"] == "subject" and d["item"]["subject"] == "Cheque bouncing law" for d in r.dropped)

    versions = (
        await db.execute(select(JudgmentReadingVersion).where(JudgmentReadingVersion.judgment_id == j.id))
    ).scalars().all()
    assert len(versions) == 1
    v = versions[0]
    assert v.status == "read" and v.is_current is True and v.attempt_no == 1
    assert (v.input_tokens, v.output_tokens, v.latency_ms, v.chunk_count) == (100, 20, 17, 1)

    assert await read_unreported(db, limit=10) == {"read": 0, "failed": 0, "locked": 0}
    assert len(calls) == 1


async def test_without_a_model_agent_does_nothing(db, monkeypatch):
    monkeypatch.setattr(settings, "READER_MODEL", "")
    monkeypatch.setattr(settings, "TREATMENT_MODEL", "")
    await _unreported(db)
    assert await read_unreported(db, limit=10) == {"skipped": "not_configured"}
    assert (await db.execute(select(JudgmentReading))).scalars().all() == []
    assert (await db.execute(select(JudgmentReadingVersion))).scalars().all() == []


async def test_failed_first_read_is_versioned_and_not_hot_looped(db, monkeypatch):
    monkeypatch.setattr(settings, "READER_MODEL", "test-model")
    monkeypatch.setattr(settings, "READER_BASE_URL", "http://reader.test/v1")

    async def broken(_text):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(unreported_reader, "call_reader_model", broken)
    (j,) = await _unreported(db)
    assert await read_unreported(db, limit=10) == {"read": 0, "failed": 1, "locked": 0}
    assert await db.get(JudgmentReading, j.id) is None
    v = (
        await db.execute(select(JudgmentReadingVersion).where(JudgmentReadingVersion.judgment_id == j.id))
    ).scalars().one()
    assert v.status == "failed" and "model unavailable" in v.error and v.retry_after_at is not None
    assert await read_unreported(db, limit=10) == {"read": 0, "failed": 0, "locked": 0}


async def test_database_publish_failure_rolls_back_and_records_attempt_without_corrupting_current(db, monkeypatch):
    monkeypatch.setattr(settings, "READER_MODEL", "test-model")
    monkeypatch.setattr(settings, "READER_BASE_URL", "http://reader.test/v1")
    (j,) = await _unreported(db)
    jid = j.id
    old = JudgmentReading(
        judgment_id=jid,
        status="read",
        model="old-model",
        prompt_version="unreported-reader-1",
        laws=[],
        points=[],
        subjects=["Bail"],
        dropped=[],
    )
    db.add(old)
    await db.commit()

    async def fake_model(_text):
        return _model_answer()

    async def broken_publish(*_args, **_kwargs):
        raise RuntimeError("database commit failed")

    monkeypatch.setattr(unreported_reader, "call_reader_model", fake_model)
    monkeypatch.setattr(unreported_reader, "_publish_success", broken_publish)

    assert await read_unreported(db, limit=10) == {"read": 0, "failed": 1, "locked": 0}
    # Rollback must leave the previous serving snapshot untouched.
    current = await db.get(JudgmentReading, jid)
    assert current is not None and current.status == "read"
    assert current.model == "old-model" and current.prompt_version == "unreported-reader-1"
    failure = (
        await db.execute(
            select(JudgmentReadingVersion).where(
                JudgmentReadingVersion.judgment_id == jid,
                JudgmentReadingVersion.status == "failed",
            )
        )
    ).scalars().one()
    assert failure.error_kind == "database"
    assert "database commit failed" in failure.error
    assert failure.is_current is False


async def test_failed_upgrade_preserves_last_good_reading(db, monkeypatch):
    """Regression: v2/v3 reclassification failure must never hide or overwrite the prior valid legal reading."""
    monkeypatch.setattr(settings, "READER_MODEL", "test-model")
    monkeypatch.setattr(settings, "READER_BASE_URL", "http://reader.test/v1")
    (j,) = await _unreported(db)
    old = JudgmentReading(
        judgment_id=j.id,
        status="read",
        model="old-model",
        prompt_version="unreported-reader-1",
        taxonomy_version=None,
        judgment_text_hash=j.full_text_hash,
        laws=[{"statute": "Old Law", "sections": [], "quote": "old"}],
        points=[],
        interpretation="old interpretation",
        interpretation_evidence=[],
        turns_on="old basis",
        turns_on_evidence=[],
        subjects=["Bail"],
        outcome="allowed",
        outcome_evidence=None,
        dropped=[],
        read_at=datetime.now(timezone.utc),
    )
    db.add(old)
    await db.commit()

    async def broken(_text):
        raise RuntimeError("upgrade failed")

    monkeypatch.setattr(unreported_reader, "call_reader_model", broken)
    assert await read_unreported(db, limit=10) == {"read": 0, "failed": 1, "locked": 0}

    await db.refresh(old)
    assert old.status == "read"
    assert old.prompt_version == "unreported-reader-1"
    assert old.model == "old-model"
    assert old.interpretation == "old interpretation"
    failed = (
        await db.execute(
            select(JudgmentReadingVersion).where(
                JudgmentReadingVersion.judgment_id == j.id,
                JudgmentReadingVersion.status == "failed",
            )
        )
    ).scalars().one()
    assert failed.prompt_version == unreported_reader.PROMPT_VERSION
    assert failed.is_current is False


async def test_successful_upgrade_keeps_immutable_history_and_switches_current(db, monkeypatch):
    monkeypatch.setattr(settings, "READER_MODEL", "test-model")
    monkeypatch.setattr(settings, "READER_BASE_URL", "http://reader.test/v1")
    (j,) = await _unreported(db)
    old = JudgmentReading(
        judgment_id=j.id,
        status="read",
        model="old",
        prompt_version="unreported-reader-1",
        laws=[],
        points=[],
        subjects=["Bail"],
        dropped=[],
    )
    db.add(old)
    db.add(
        JudgmentReadingVersion(
            judgment_id=j.id,
            status="read",
            model="old",
            prompt_version="unreported-reader-1",
            attempt_no=1,
            is_current=True,
        )
    )
    await db.commit()

    async def fake_model(_text):
        return _model_answer()

    monkeypatch.setattr(unreported_reader, "call_reader_model", fake_model)
    assert await read_unreported(db, limit=10) == {"read": 1, "failed": 0, "locked": 0}
    versions = (
        await db.execute(
            select(JudgmentReadingVersion)
            .where(JudgmentReadingVersion.judgment_id == j.id)
            .order_by(JudgmentReadingVersion.attempt_no)
        )
    ).scalars().all()
    assert [(v.attempt_no, v.prompt_version, v.is_current) for v in versions] == [
        (1, "unreported-reader-1", False),
        (2, unreported_reader.PROMPT_VERSION, True),
    ]


def test_subject_taxonomy_records_overflow_and_never_invents_other():
    subjects, dropped = unreported_reader.fixed_subjects(
        ["Service law", "Bail", "Limitation", "Arbitration", "Something new"]
    )
    assert subjects == ["Service law", "Bail", "Limitation"]
    assert {"kind": "subject", "item": {"subject": "Arbitration"}, "why": "maximum three subjects exceeded"} in dropped
    assert any(d["item"]["subject"] == "Something new" for d in dropped)
    assert unreported_reader.fixed_subjects([])[0] == []
    assert unreported_reader.fixed_subjects(["Invented heading"])[0] == []
    assert unreported_reader.fixed_subjects(["Other"])[0] == ["Other"]
    assert len(unreported_reader.SUBJECTS) == len(set(unreported_reader.SUBJECTS))


async def test_invalid_only_subject_output_is_a_validation_failure(monkeypatch):
    async def fake_model(_text):
        ans = _model_answer()
        ans["subjects"] = ["Invented heading"]
        return ans

    monkeypatch.setattr(unreported_reader, "call_reader_model", fake_model)
    with pytest.raises(unreported_reader.ReaderValidationError):
        await unreported_reader.read_full_judgment(TEXT)


async def test_long_judgment_reads_the_middle_instead_of_head_tail_truncation(monkeypatch):
    monkeypatch.setattr(settings, "READER_MAX_CHARS", 2000)
    monkeypatch.setattr(settings, "READER_CHUNK_OVERLAP_CHARS", 200)
    middle = "MIDDLE_MARKER_SECTION_42 " * 80
    long_text = ("A" * 2600) + "\n" + middle + "\n" + ("Z" * 2600)
    seen = []

    async def fake_model(chunk):
        seen.append(chunk)
        return {
            "laws": [],
            "points": [],
            "interpretation": [],
            "turns_on": [],
            "subjects": ["Other"],
            "outcome": None,
        }

    monkeypatch.setattr(unreported_reader, "call_reader_model", fake_model)
    result = await unreported_reader.read_full_judgment(long_text)
    assert len(seen) >= 3
    assert any("MIDDLE_MARKER_SECTION_42" in chunk for chunk in seen)
    assert result["_reader_meta"]["chunk_count"] == len(seen)


async def test_ungrounded_interpretation_turns_on_and_outcome_are_refused(monkeypatch):
    async def fake_model(_text):
        ans = _model_answer()
        ans["interpretation"] = [{"statement": "Invented rule", "quote": "not in the judgment at all"}]
        ans["turns_on"] = [{"statement": "Invented basis", "quote": "also absent from source"}]
        ans["outcome"] = {"value": "dismissed", "quote": "petition dismissed with costs"}
        return ans

    monkeypatch.setattr(unreported_reader, "call_reader_model", fake_model)
    result = await unreported_reader.read_full_judgment(TEXT)
    assert result["interpretation"] is None and result["interpretation_evidence"] == []
    assert result["turns_on"] is None and result["turns_on_evidence"] == []
    assert result["outcome"] is None and result["outcome_evidence"] is None
    assert {"interpretation", "turns_on", "outcome"} <= {d["kind"] for d in result["dropped"]}


async def test_model_call_sends_data_reads_json_and_tracks_usage(monkeypatch):
    import json as _json

    monkeypatch.setattr(settings, "READER_MODEL", "test-model")
    monkeypatch.setattr(settings, "READER_BASE_URL", "http://reader.test/v1")
    monkeypatch.setattr(settings, "READER_HTTP_RETRIES", 0)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = _json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Here: " + _json.dumps(_model_answer())}}],
                "usage": {"prompt_tokens": 123, "completion_tokens": 45},
            },
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(unreported_reader.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    out = await unreported_reader.call_reader_model(TEXT)
    assert out["outcome"]["value"] == "allowed"
    assert out["_reader_meta"]["input_tokens"] == 123 and out["_reader_meta"]["output_tokens"] == 45
    assert out["_reader_meta"]["parse_repair_used"] is True
    assert seen["url"] == "http://reader.test/v1/chat/completions"
    assert seen["body"]["model"] == "test-model"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert "DATA" in seen["body"]["messages"][0]["content"] and "489-F" in seen["body"]["messages"][1]["content"]


async def test_auth_failure_records_operator_notification(db, monkeypatch):
    monkeypatch.setattr(settings, "READER_MODEL", "test-model")
    (j,) = await _unreported(db)
    await unreported_reader._record_failure(
        db,
        judgment_id=j.id,
        judgment_text_hash=j.full_text_hash,
        attempt_no=1,
        started_at=datetime.now(timezone.utc),
        exc=unreported_reader.ReaderError("bad reader key", kind="auth", permanent=True),
    )
    note = (
        await db.execute(select(Notification).where(Notification.code == "READER_PROVIDER_CONFIGURATION"))
    ).scalars().one()
    assert note.level == "error"
    assert note.source_name == "UnreportedReader"
    assert note.details["error_kind"] == "auth"


def test_reader_http_failure_classification():
    auth = unreported_reader._reader_http_error(httpx.Response(401, text="bad key"))
    assert auth.kind == "auth" and auth.permanent is True
    limited = unreported_reader._reader_http_error(httpx.Response(429, headers={"Retry-After": "17"}, text="slow down"))
    assert limited.kind == "rate_limit" and limited.retry_after_seconds == 17
    transient = unreported_reader._reader_http_error(httpx.Response(503, text="down"))
    assert transient.kind == "transient" and transient.permanent is False
    config = unreported_reader._reader_http_error(httpx.Response(400, text="unsupported response format"))
    assert config.kind == "configuration" and config.permanent is True


def test_reader_model_off_stops_even_with_treatment_model(monkeypatch):
    monkeypatch.setattr(settings, "READER_MODEL", "off")
    monkeypatch.setattr(settings, "TREATMENT_MODEL", "some-model")
    assert unreported_reader.model_name() == "" and not unreported_reader.configured()


def test_reader_configuration_requires_credential_for_custom_or_openai_endpoint(monkeypatch):
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "READER_MODEL", "test-model")
    monkeypatch.setattr(settings, "TREATMENT_MODEL", "")
    monkeypatch.setattr(settings, "READER_BASE_URL", "http://reader.test/v1")
    monkeypatch.setattr(settings, "READER_API_KEY", SecretStr(""))
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    assert unreported_reader.configured() is False

    monkeypatch.setattr(settings, "READER_API_KEY", SecretStr("reader-key"))
    assert unreported_reader.configured() is True

    monkeypatch.setattr(settings, "READER_BASE_URL", "")
    monkeypatch.setattr(settings, "READER_API_KEY", SecretStr(""))
    monkeypatch.setattr(settings, "OPENAI_API_KEY", SecretStr(""))
    assert unreported_reader.configured() is False

    monkeypatch.setattr(settings, "OPENAI_API_KEY", SecretStr("openai-key"))
    assert unreported_reader.configured() is True


async def test_answer_without_a_reading_is_failure(monkeypatch):
    monkeypatch.setattr(settings, "READER_MODEL", "test-model")
    monkeypatch.setattr(settings, "READER_BASE_URL", "http://reader.test/v1")
    monkeypatch.setattr(settings, "READER_HTTP_RETRIES", 0)
    real = httpx.AsyncClient
    for response_content in ("", "I cannot help with that.", '{"note": "nothing"}'):
        handler = lambda request, c=response_content: httpx.Response(200, json={"choices": [{"message": {"content": c}}]})
        monkeypatch.setattr(unreported_reader.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
        with pytest.raises(unreported_reader.ReaderValidationError):
            await unreported_reader.call_reader_model(TEXT)


async def test_per_judgment_lock_allows_parallel_workers_without_duplicate_spend(db, monkeypatch):
    from sqlalchemy import text as sql_text

    from scraper.database import engine

    monkeypatch.setattr(settings, "READER_MODEL", "test-model")
    monkeypatch.setattr(settings, "READER_BASE_URL", "http://reader.test/v1")
    (j,) = await _unreported(db)
    calls = []

    async def fake_model(_text):
        calls.append(1)
        return _model_answer()

    monkeypatch.setattr(unreported_reader, "call_reader_model", fake_model)
    key = unreported_reader._judgment_lock_key(j.id)
    async with engine.connect() as holder:
        holder = await holder.execution_options(isolation_level="AUTOCOMMIT")
        await holder.execute(sql_text("SELECT pg_advisory_lock(:k)"), {"k": key})
        try:
            assert await read_unreported(db, limit=10) == {"read": 0, "failed": 0, "locked": 1}
        finally:
            await holder.execute(sql_text("SELECT pg_advisory_unlock(:k)"), {"k": key})
    assert calls == []


def test_genuine_passage_paired_with_invented_law_is_dropped():
    answer = {
        "laws": [
            {"statute": "Contract Act 1872", "sections": ["73"], "quote": "The application is allowed and the applicant is admitted to bail."},
            {"statute": "Pakistan Penal Code", "sections": [], "quote": "a case under section 489-F of the Pakistan Penal Code"},
            {"statute": "Code of Criminal Procedure", "sections": ["497(2)", "498"], "quote": "within the meaning of section 497(2) Cr.P.C."},
        ],
        "points": [],
    }
    kept, dropped = keep_only_evidenced(answer, TEXT)
    assert [law["statute"] for law in kept["laws"]] == ["Pakistan Penal Code", "Code of Criminal Procedure"]
    assert kept["laws"][1]["sections"] == ["497(2)"]  # unsupported 498 must not ride along with a genuine quote
    assert any(d["kind"] == "law_section" and d["item"]["section"] == "498" for d in dropped)
    assert any(d["kind"] == "law" and "does not name any listed section" in d["why"] for d in dropped)


def test_viewer_shows_grounded_reading_and_audit_metadata(client, admin_headers):
    from scraper.database import SessionLocal, run_async

    async def seed():
        async with SessionLocal() as db:
            (j,) = await _unreported(db)
            db.add(
                JudgmentReading(
                    judgment_id=j.id,
                    status="read",
                    model="m",
                    prompt_version=unreported_reader.PROMPT_VERSION,
                    taxonomy_version=unreported_reader.SUBJECT_TAXONOMY_VERSION,
                    judgment_text_hash=j.full_text_hash,
                    laws=MODEL_ANSWER["laws"][:2],
                    points=MODEL_ANSWER["points"][:1],
                    interpretation="i",
                    interpretation_evidence=[{"statement": "i", "quote": "The case of the applicant calls for further inquiry"}],
                    turns_on="t",
                    turns_on_evidence=[{"statement": "t", "quote": "The offence does not fall within the prohibitory clause"}],
                    subjects=["Bail"],
                    outcome="allowed",
                    outcome_evidence={"value": "allowed", "quote": "The application is allowed and the applicant is admitted to bail."},
                    dropped=[],
                )
            )
            db.add(
                JudgmentReadingVersion(
                    judgment_id=j.id,
                    status="read",
                    model="m",
                    provider="reader.test",
                    prompt_version=unreported_reader.PROMPT_VERSION,
                    taxonomy_version=unreported_reader.SUBJECT_TAXONOMY_VERSION,
                    judgment_text_hash=j.full_text_hash,
                    attempt_no=1,
                    input_tokens=100,
                    output_tokens=10,
                    latency_ms=50,
                    chunk_count=2,
                    is_current=True,
                )
            )
            await db.commit()
            return str(j.id)

    jid = run_async(seed())
    d = client.get(f"/admin/corpus/judgments/{jid}", headers=admin_headers).json()
    assert d["reading"]["machine_reading"] is True
    assert d["reading"]["subjects"] == ["Bail"]
    assert d["reading"]["outcome_evidence"]["value"] == "allowed"
    assert d["reading"]["audit"]["chunk_count"] == 2
    assert d["reading"]["audit"]["input_tokens"] == 100
    s = client.get("/admin/corpus/unreported", headers=admin_headers).json()
    assert s["read"] == 1 and s["subjects"][0] == {"subject": "Bail", "count": 1}
    filtered = client.get("/admin/corpus/judgments?section=unreported&subject=Bail", headers=admin_headers).json()
    assert filtered["total"] == 1


def test_section_summary_counts_only_judgments_still_unreported(client, admin_headers):
    from scraper.database import SessionLocal, run_async

    async def seed():
        async with SessionLocal() as db:
            (j,) = await _unreported(db)
            db.add(JudgmentReading(judgment_id=j.id, status="read", model="m", prompt_version="v", laws=[], points=[], subjects=["Bail"], dropped=[]))
            await db.commit()
            j.unreported = False
            await db.commit()

    run_async(seed())
    s = client.get("/admin/corpus/unreported", headers=admin_headers).json()
    assert s["read"] == 0 and s["subjects"] == []
