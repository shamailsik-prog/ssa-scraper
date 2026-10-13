"""The reading agent: reads unreported judgments in the background and records the law, the points decided,
the interpretation and what the decision turns on, each point tied to words that are really in the judgment."""

from __future__ import annotations

from sqlalchemy import select

from scraper.config import settings
from scraper.models import Judgment, JudgmentReading
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
        {"statute": "Contract Act 1872", "sections": ["73"], "quote": "damages for breach under section 73"},  # not in the text
    ],
    "points": [
        {"point": "Bail where the offence is outside the prohibitory clause", "decision": "Bail granted: further inquiry", "quote": "The case of the applicant calls for further inquiry"},
        {"point": "Invented point", "decision": "Invented", "quote": "the court overruled PLD 2010 SC 1"},
    ],
    "interpretation": "Section 497(2) applies where the material leaves room for further inquiry.",
    "turns_on": "Offence outside the prohibitory clause; further inquiry.",
    "subjects": ["Bail", "Dishonoured cheque (489-F PPC)"],
    "outcome": "allowed",
}


def test_tags_without_their_words_in_the_judgment_are_dropped():
    kept, dropped = keep_only_evidenced(MODEL_ANSWER, TEXT)
    assert [law["statute"] for law in kept["laws"]] == ["Pakistan Penal Code", "Code of Criminal Procedure"]
    assert [p["point"] for p in kept["points"]] == ["Bail where the offence is outside the prohibitory clause"]
    assert {d["kind"] for d in dropped} == {"law", "point"} and len(dropped) == 2


def test_quotes_are_matched_regardless_of_spacing_and_case():
    kept, _ = keep_only_evidenced({"laws": [{"statute": "PPC", "sections": [], "quote": "UNDER  SECTION\n489-F of the pakistan penal code"}], "points": []}, TEXT)
    assert len(kept["laws"]) == 1


async def _unreported(db, n=1):
    rows = []
    for i in range(n):
        j = Judgment(canonical_citation=f"UNREPORTED SHC Cr. Bail Application No. {i} of 2024 (2024-05-01)", full_text_hash=f"{i:064d}", full_text=TEXT, court_name="High Court of Sindh", source_name="SindhHighCourt", unreported=True, proceeding_type="bail", access_method="public")
        db.add(j)
        rows.append(j)
    db.add(Judgment(canonical_citation="PLD 2020 SC 1", full_text_hash="f" * 64, full_text=TEXT, court_name="Supreme Court of Pakistan", reporter="PLD", access_method="public"))
    await db.commit()
    return rows


async def test_the_agent_reads_unreported_judgments_once_and_keeps_only_evidenced_tags(db, monkeypatch):
    monkeypatch.setattr(settings, "READER_MODEL", "test-model")
    monkeypatch.setattr(settings, "READER_BASE_URL", "http://reader.test/v1")
    calls = []

    async def fake_model(text):
        calls.append(text)
        return MODEL_ANSWER

    monkeypatch.setattr(unreported_reader, "call_reader_model", fake_model)
    (j,) = await _unreported(db)

    out = await read_unreported(db, limit=10)
    assert out == {"read": 1, "failed": 0}
    assert len(calls) == 1  # the reported judgment is not read
    r = (await db.execute(select(JudgmentReading).where(JudgmentReading.judgment_id == j.id))).scalars().one()
    assert r.status == "read" and r.model == "test-model" and r.outcome == "allowed"
    assert [law["statute"] for law in r.laws] == ["Pakistan Penal Code", "Code of Criminal Procedure"]
    assert len(r.points) == 1 and len(r.dropped) == 2
    assert r.subjects == ["Bail", "Dishonoured cheque (489-F PPC)"]

    assert await read_unreported(db, limit=10) == {"read": 0, "failed": 0}  # never read twice
    assert len(calls) == 1


async def test_without_a_model_the_agent_does_nothing_and_says_so(db, monkeypatch):
    monkeypatch.setattr(settings, "READER_MODEL", "")
    monkeypatch.setattr(settings, "TREATMENT_MODEL", "")
    await _unreported(db)
    assert await read_unreported(db, limit=10) == {"skipped": "not_configured"}
    assert (await db.execute(select(JudgmentReading))).scalars().all() == []


async def test_a_failed_reading_is_recorded_and_retried_later_not_in_a_loop(db, monkeypatch):
    monkeypatch.setattr(settings, "READER_MODEL", "test-model")
    monkeypatch.setattr(settings, "READER_BASE_URL", "http://reader.test/v1")

    async def broken(_text):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(unreported_reader, "call_reader_model", broken)
    (j,) = await _unreported(db)
    assert await read_unreported(db, limit=10) == {"read": 0, "failed": 1}
    r = (await db.execute(select(JudgmentReading).where(JudgmentReading.judgment_id == j.id))).scalars().one()
    assert r.status == "failed" and "model unavailable" in r.error
    assert await read_unreported(db, limit=10) == {"read": 0, "failed": 0}  # not retried at once


def test_the_viewer_shows_the_reading_and_subjects(client, admin_headers, monkeypatch):
    from scraper.database import SessionLocal, run_async

    async def seed():
        async with SessionLocal() as db:
            (j,) = await _unreported(db)
            db.add(JudgmentReading(judgment_id=j.id, status="read", model="m", prompt_version="v", laws=MODEL_ANSWER["laws"][:2], points=MODEL_ANSWER["points"][:1], interpretation="i", turns_on="t", subjects=["Bail"], outcome="allowed", dropped=[]))
            await db.commit()
            return str(j.id)

    jid = run_async(seed())
    d = client.get(f"/admin/corpus/judgments/{jid}", headers=admin_headers).json()
    assert d["reading"]["machine_reading"] is True and d["reading"]["subjects"] == ["Bail"]
    s = client.get("/admin/corpus/unreported", headers=admin_headers).json()
    assert s["read"] == 1 and s["subjects"][0] == {"subject": "Bail", "count": 1}
    r = client.get("/admin/corpus/judgments?section=unreported&subject=Bail", headers=admin_headers).json()
    assert r["total"] == 1


async def test_the_model_call_sends_the_judgment_as_data_and_reads_the_json_back(monkeypatch):
    import json as _json

    import httpx

    monkeypatch.setattr(settings, "READER_MODEL", "test-model")
    monkeypatch.setattr(settings, "READER_BASE_URL", "http://reader.test/v1")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Here: " + _json.dumps(MODEL_ANSWER)}}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(unreported_reader.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    out = await unreported_reader.call_reader_model(TEXT)
    assert out["outcome"] == "allowed"
    assert seen["url"] == "http://reader.test/v1/chat/completions"
    assert seen["body"]["model"] == "test-model" and seen["body"]["response_format"] == {"type": "json_object"}
    assert "DATA" in seen["body"]["messages"][0]["content"] and "489-F" in seen["body"]["messages"][1]["content"]


def test_reader_model_off_stops_the_agent_even_with_a_treatment_model(monkeypatch):
    monkeypatch.setattr(settings, "READER_MODEL", "off")
    monkeypatch.setattr(settings, "TREATMENT_MODEL", "some-model")
    assert unreported_reader.model_name() == "" and not unreported_reader.configured()


async def test_an_answer_without_a_reading_is_a_failure_not_an_empty_reading(monkeypatch):
    import httpx
    import pytest

    monkeypatch.setattr(settings, "READER_MODEL", "test-model")
    monkeypatch.setattr(settings, "READER_BASE_URL", "http://reader.test/v1")
    real = httpx.AsyncClient
    for content in ("", "I cannot help with that.", '{"note": "nothing"}'):
        handler = lambda request, c=content: httpx.Response(200, json={"choices": [{"message": {"content": c}}]})
        monkeypatch.setattr(unreported_reader.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
        with pytest.raises(ValueError):
            await unreported_reader.call_reader_model(TEXT)


async def test_a_second_reading_run_skips_while_one_is_in_progress(db, monkeypatch):
    from sqlalchemy import text as sql_text

    from scraper.database import engine

    monkeypatch.setattr(settings, "READER_MODEL", "test-model")
    async with engine.connect() as holder:
        holder = await holder.execution_options(isolation_level="AUTOCOMMIT")
        await holder.execute(sql_text("SELECT pg_advisory_lock(:k)"), {"k": unreported_reader.READER_LOCK_KEY})
        try:
            assert await unreported_reader._run(10) == {"skipped": "reading_run_in_progress"}
        finally:
            await holder.execute(sql_text("SELECT pg_advisory_unlock(:k)"), {"k": unreported_reader.READER_LOCK_KEY})
