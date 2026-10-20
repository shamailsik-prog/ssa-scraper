from scraper.parsers.pld_citation_relabel import relabel_pld_citation

_LHC = "IN THE LAHORE HIGH COURT, LAHORE\nBefore X, J\n" + "y" * 200
_SC = "IN THE SUPREME COURT OF PAKISTAN\nBefore Y, J\n(On appeal from the judgment of the Lahore High Court)\n" + "z" * 200
_SINDH = "IN THE HIGH COURT OF SINDH AT KARACHI\nBefore Z, J\n" + "a" * 200
_PESH = "IN THE PESHAWAR HIGH COURT, PESHAWAR\nBefore A, J\n" + "b" * 200
_BALO = "IN THE BALOCHISTAN HIGH COURT, QUETTA\nBefore B, J\n" + "c" * 200
_IHC = "IN THE ISLAMABAD HIGH COURT, ISLAMABAD\nBefore C, J\n" + "d" * 200


def test_pld_relabel_lahore_from_header_only():
    assert relabel_pld_citation("2026 PLD 511", _LHC) == "PLD 2026 Lahore 511"


def test_pld_relabel_sc_not_lahore_from_appeal_footnote():
    assert relabel_pld_citation("2026 PLD 511", _SC) == "PLD 2026 SC 511"


def test_pld_relabel_sindh_high_court_of_sindh():
    assert relabel_pld_citation("2025 PLD 100", _SINDH) == "PLD 2025 Sindh 100"


def test_pld_relabel_peshawar():
    assert relabel_pld_citation("2024 PLD 88", _PESH) == "PLD 2024 Peshawar 88"


def test_pld_relabel_balochistan():
    assert relabel_pld_citation("2023 PLD 77", _BALO) == "PLD 2023 Balochistan 77"


def test_pld_relabel_islamabad():
    assert relabel_pld_citation("2022 PLD 66", _IHC) == "PLD 2022 Islamabad 66"


_AJK_SC = "IN THE SUPREME COURT OF AZAD JAMMU AND KASHMIR\nBefore C, J\n" + "d" * 200


def test_pld_relabel_ajk_supreme_court_not_pakistan_sc():
    out = relabel_pld_citation("2024 PLD 50", _AJK_SC)
    assert out == "PLD 2024 SC (AJ&K) 50"
    assert "PLD 2024 SC 50" != out
