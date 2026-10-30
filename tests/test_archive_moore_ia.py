"""Moore's Indian Appeals (MIA) archive.org OCR split. Fixture text only — no volume download."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scraper.config import KNOWN_REPORTERS, _JOURNAL_CODE_RE, canonical_reporter
from scraper.tasks.archive_moore_ia import (
    BAILII_SECONDARY_URLS,
    DECOY_ITEM_IDS,
    MOORE_IA_VOLUMES,
    ArchiveRateLimiter,
    VolumeNotOnArchive,
    dedupe_judgments,
    download_url,
    fetch_volume_ocr,
    main,
    ocr_filename_from_metadata,
    split_moore_ia_volume,
)
from scraper.tasks.celery_app import app as celery_app

FIXTURES = Path(__file__).parent / "fixtures" / "archive_moore_ia"
SYNTHETIC = FIXTURES / "synthetic_volume.txt"
EXCERPT = FIXTURES / "vol6_nusserwanjee_excerpt.txt"


@pytest.mark.parametrize(
    "raw",
    [
        "MIA",
        "Moore IA",
        "Moo Ind App",
        "M.I.A.",
        "Moore's Indian Appeals",
        "Moo. Ind. App.",
        "9 Moo Ind App 307",
    ],
)
def test_moore_ia_aliases_canonicalise_to_letters_only_mia(raw):
    assert canonical_reporter(raw) == "MIA"
    assert _JOURNAL_CODE_RE.fullmatch("MIA")
    assert _JOURNAL_CODE_RE.fullmatch("Moore IA") is None


def test_mia_is_not_a_pakistanlawsite_reporter():
    """KNOWN_REPORTERS is the PLS search-harvest fallback. Moore IA is not on that site."""
    assert "MIA" not in KNOWN_REPORTERS
    assert canonical_reporter("P.Cr.L.J.") == "PCrLJ"


def test_catalog_lists_every_volume_and_excludes_moore_pc_decoys():
    assert set(MOORE_IA_VOLUMES) == set(range(1, 15))
    assert MOORE_IA_VOLUMES[6].item_id == "reportsofcaseshe06grea"
    assert MOORE_IA_VOLUMES[6].ocr_filename == "reportsofcaseshe06grea_djvu.txt"
    assert MOORE_IA_VOLUMES[9].item_id == "in.ernet.dli.2015.531521"
    assert MOORE_IA_VOLUMES[9].ocr_filename == "2015.531521.Reports-Of_djvu.txt"
    assert MOORE_IA_VOLUMES[11].item_id == "reportscaseshea01moorgoog"
    assert MOORE_IA_VOLUMES[14].ocr_filename == "2015.469863.Reports-Of_djvu.txt"
    assert MOORE_IA_VOLUMES[13].item_id is None
    ids = set()
    for entry in MOORE_IA_VOLUMES.values():
        if entry.item_id:
            ids.add(entry.item_id)
        ids.update(entry.alternates)
    assert ids.isdisjoint(DECOY_ITEM_IDS)
    assert "reportscaseshea00commgoog" in DECOY_ITEM_IDS
    assert "reportscaseshea00moorgoog" in DECOY_ITEM_IDS
    assert "reportscaseshea01commgoog" in DECOY_ITEM_IDS


def test_synthetic_volume_splits_into_two_judgments():
    rows = split_moore_ia_volume(
        SYNTHETIC.read_text(encoding="utf-8"),
        volume=6,
        item_id="reportsofcaseshe06grea",
        year_from=1854,
        year_to=1857,
        ocr_source="archive.org:reportsofcaseshe06grea_djvu.txt",
    )
    assert [row["dedup_key"] for row in rows] == ["MIA:6:27", "MIA:6:134"]
    first, second = rows
    assert first["reporter"] == "MIA"
    assert first["volume"] == 6
    assert first["page"] == 27
    assert first["parties"] == "Musadee Mahomed Cazum Sherazee v. Meerza Ally Mahomed Shoostry"
    assert first["decision_date"] == "1854-12-04"
    assert first["court"] == "Privy Council"
    assert first["lower_court"] == "the Supreme Court at Bombay"
    assert first["source_url"] == "https://archive.org/details/reportsofcaseshe06grea"
    assert first["ocr_source"] == "archive.org:reportsofcaseshe06grea_djvu.txt"
    assert "order made in a cause" in first["full_text"]
    assert "question of an award" not in first["full_text"]
    assert "Digitized by" not in first["full_text"]
    assert "ON APPEAL FROM THE EAST INDIES" not in first["full_text"]
    assert second["page"] == 134
    assert second["parties"] == "Nusserwanjee Pestonjee v. Meer Mynoodeen Khan Bahadoor"
    assert second["decision_date"] == "1855-06-19"
    assert second["lower_court"] == "the Sudder Dewanny Adawlut at Bombay"
    assert "question of an award" in second["full_text"]
    assert "order made in a cause" not in second["full_text"]
    assert second["alt_source_url"] is None


def test_trimmed_vol6_ocr_maps_nusserwanjee_from_the_table_of_cases():
    """Running head OCR says 131; the Table of Cases says 134. The table wins."""
    rows = split_moore_ia_volume(
        EXCERPT.read_text(encoding="utf-8"),
        volume=6,
        item_id="reportsofcaseshe06grea",
        year_from=1854,
        year_to=1857,
        ocr_source="local:vol6_nusserwanjee_excerpt.txt",
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["reporter"] == "MIA"
    assert row["volume"] == 6
    assert row["page"] == 134
    assert row["dedup_key"] == "MIA:6:134"
    assert "Pestonjee" in row["parties"]
    assert "Mynoodeen" in row["parties"]
    assert row["court"] == "Privy Council"
    assert row["decision_date"] == "1855-06-19"
    assert "Bombay" in row["lower_court"]
    assert "award" in row["full_text"].lower()
    assert row["ocr_source"] == "local:vol6_nusserwanjee_excerpt.txt"


def test_bailii_url_is_attached_only_for_the_three_known_cites():
    text = """
TABLE OF THE CASES REPORTED IN THIS VOLUME.
Moonshee Buzl-un-Ruheem v. Luteefutoon-Nissa . . 379
ON APPEAL FROM THE EAST INDIES. 379
Moonshee Buzl-un-Ruheem - Appellant.
AND
Luteefutoon-Nissa - Respondent.
On appeal from the Supreme Court at Calcutta.
8th Dec. 1861.
The appeal was allowed.
"""
    rows = split_moore_ia_volume(
        text,
        volume=8,
        item_id="reportsofcaseshe08grea",
        year_from=1859,
        year_to=1861,
        ocr_source="archive.org:reportsofcaseshe08grea_djvu.txt",
    )
    assert len(rows) == 1
    assert rows[0]["dedup_key"] == "MIA:8:379"
    assert rows[0]["alt_source_url"] == BAILII_SECONDARY_URLS[(8, 379)]
    assert set(BAILII_SECONDARY_URLS) == {(8, 379), (9, 307), (10, 203)}


def test_same_volume_and_page_collapses_to_one_record():
    short = {"reporter": "MIA", "volume": 4, "page": 431, "dedup_key": "MIA:4:431", "full_text": "short"}
    longer = {"reporter": "MIA", "volume": 4, "page": 431, "dedup_key": "MIA:4:431", "full_text": "much longer text"}
    other = {"reporter": "MIA", "volume": 4, "page": 339, "dedup_key": "MIA:4:339", "full_text": "other"}
    unpaged = {"reporter": "MIA", "volume": 4, "page": None, "dedup_key": None, "full_text": "no page"}
    rows = dedupe_judgments([short, other, longer, unpaged])
    assert [row["dedup_key"] for row in rows] == ["MIA:4:431", "MIA:4:339", None]
    assert rows[0]["full_text"] == "much longer text"


def test_volume_13_is_refused_before_any_download():
    with pytest.raises(VolumeNotOnArchive, match="13"):
        fetch_volume_ocr(13)


def test_local_ocr_path_does_not_touch_the_network(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("archive.org was contacted")

    monkeypatch.setattr("scraper.tasks.archive_moore_ia.httpx.Client", boom)
    text = fetch_volume_ocr(6, ocr_path=str(SYNTHETIC))
    assert "Nusserwanjee" in text


def test_fetch_uses_the_catalog_txt_name_and_spaces_requests():
    calls = []
    sleeps = []
    clock = {"t": 0.0}

    class Resp:
        status_code = 200
        text = "ocr-body"

        def json(self):
            return {}

    class Client:
        def get(self, url):
            calls.append(url)
            return Resp()

    def sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds

    limiter = ArchiveRateLimiter(interval=1.0, sleep=sleep, clock=lambda: clock["t"])
    client = Client()
    assert fetch_volume_ocr(6, client=client, limiter=limiter) == "ocr-body"
    assert fetch_volume_ocr(14, client=client, limiter=limiter) == "ocr-body"
    assert calls == [
        download_url("reportsofcaseshe06grea", "reportsofcaseshe06grea_djvu.txt"),
        download_url("in.ernet.dli.2015.469863", "2015.469863.Reports-Of_djvu.txt"),
    ]
    assert sleeps == [1.0]


def test_a_missing_txt_name_is_resolved_from_item_metadata(monkeypatch):
    assert (
        ocr_filename_from_metadata(
            {"files": [{"name": "2015.469863.Reports-Of_djvu.txt", "format": "DjVuTXT"}]}
        )
        == "2015.469863.Reports-Of_djvu.txt"
    )
    calls = []

    class Resp:
        def __init__(self, status, text="", payload=None):
            self.status_code = status
            self.text = text
            self._payload = payload or {}

        def json(self):
            return self._payload

    class Client:
        def get(self, url):
            calls.append(url)
            if url.endswith("missing_djvu.txt"):
                return Resp(404)
            if "/metadata/" in url:
                return Resp(200, payload={"files": [{"name": "real_djvu.txt", "format": "DjVuTXT"}]})
            return Resp(200, text="from-metadata")

    from dataclasses import replace

    from scraper.tasks import archive_moore_ia as mod

    original = mod.MOORE_IA_VOLUMES[1]
    monkeypatch.setitem(mod.MOORE_IA_VOLUMES, 1, replace(original, ocr_filename="missing_djvu.txt"))
    limiter = ArchiveRateLimiter(interval=0, sleep=lambda _n: None, clock=lambda: 0.0)
    text = fetch_volume_ocr(1, client=Client(), limiter=limiter)
    assert text == "from-metadata"
    assert calls[0].endswith("/missing_djvu.txt")
    assert "/metadata/reportsofcaseshe01grea" in calls[1]
    assert calls[2].endswith("/real_djvu.txt")


def test_dry_run_reads_a_local_file_and_prints_cases(capsys):
    code = main(["--volume", "6", "--ocr-path", str(SYNTHETIC), "--dry-run"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["volume"] == 6
    assert payload["fetched"] is False
    assert payload["cases"] == 2
    assert payload["judgments"][0]["dedup_key"] == "MIA:6:27"
    assert payload["wrote_database"] is False


def test_dry_run_volume_13_reports_the_gap(capsys):
    code = main(["--volume", "13", "--dry-run"])
    assert code == 2
    err = capsys.readouterr().err
    assert "13" in err
    assert "archive.org" in err


def test_moore_ia_is_not_a_celery_job():
    includes = list(celery_app.conf.include or [])
    assert not any("archive_moore_ia" in name for name in includes)
