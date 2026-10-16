"""The reading agent never sends login_session text to its external model (16 October 2026).

The privacy-boundary reviewer found that the only access_method check between PakistanLawSite text and
the reading agent's OpenAI-compatible call was one predicate in the selection query; removing it would
have passed every existing test. The sink now refuses any text that is not from a public source, and
these tests pin both layers: the query never selects a login_session judgment, and call_reader_model
refuses one before any network call.
"""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select

from scraper.config import settings
from scraper.models import Judgment, JudgmentReading
from scraper.tasks import unreported_reader
from scraper.tasks.unreported_reader import ExternalModelRefused, read_unreported

PUBLIC_TEXT = (
    "ORDER. The applicant seeks post-arrest bail in a case under section 489-F of the Pakistan Penal Code. "
    "The case of the applicant calls for further inquiry within the meaning of section 497(2) Cr.P.C."
)
LOGIN_TEXT = "PAKISTANLAWSITE LOGIN-SESSION TEXT: this full text must never leave the firm."
ANSWER = {"laws": [], "points": [], "interpretation": "i", "turns_on": "t", "subjects": ["Bail"], "outcome": "allowed"}


def _configure(monkeypatch):
    monkeypatch.setattr(settings, "READER_MODEL", "test-model")
    monkeypatch.setattr(settings, "READER_BASE_URL", "http://reader.test/v1")


def _no_network(monkeypatch):
    def refuse(**_kw):
        raise AssertionError("no HTTP client may be opened for refused text")

    monkeypatch.setattr(unreported_reader.httpx, "AsyncClient", refuse)


@pytest.mark.parametrize("access_method", ["login_session", "LOGIN_SESSION", "licensed_api", "import_folder", None, ""])
async def test_the_model_call_refuses_text_that_is_not_public(monkeypatch, access_method):
    _configure(monkeypatch)
    _no_network(monkeypatch)
    with pytest.raises(ExternalModelRefused):
        await unreported_reader.call_reader_model(LOGIN_TEXT, access_method=access_method)


async def test_a_login_session_unreported_judgment_is_never_sent(db, monkeypatch):
    _configure(monkeypatch)
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.content.decode())
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(ANSWER)}}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(unreported_reader.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    pls = Judgment(canonical_citation="UNREPORTED PLS 1 (2024-05-01)", full_text_hash="a" * 64, full_text=LOGIN_TEXT, court_name="High Court of Sindh", source_name="PakistanLawSite", unreported=True, access_method="login_session")
    public = Judgment(canonical_citation="UNREPORTED SHC 1 (2024-05-01)", full_text_hash="b" * 64, full_text=PUBLIC_TEXT, court_name="High Court of Sindh", source_name="SindhHighCourt", unreported=True, access_method="public")
    db.add_all([pls, public])
    await db.commit()

    assert await read_unreported(db, limit=10) == {"read": 1, "failed": 0}
    assert len(sent) == 1 and "489-F" in sent[0]
    assert not any("LOGIN-SESSION" in body for body in sent)
    readings = (await db.execute(select(JudgmentReading))).scalars().all()
    assert [r.judgment_id for r in readings] == [public.id]  # the login_session judgment is not even selected
