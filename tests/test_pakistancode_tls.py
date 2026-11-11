"""PakistanCode presents a leaf signed by GoGetSSL RSA DV CA and omits that intermediate.

The pinned public intermediate (AIA http://crt.usertrust.com/GoGetSSLRSADVCA.crt) is appended
to the system and certifi roots. Verification stays on, and only pakistancode.gov.pk uses it.
"""

from __future__ import annotations

import ssl
import warnings
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509.oid import NameOID
from sqlalchemy import select

from scraper.fetchers import HttpFetcher
from scraper.harvest_mode import cadence_for_source, update_interval_hours
from scraper.models import ScraperSource
from scraper.pakistancode_tls import (
    INTERMEDIATE_NOT_AFTER,
    INTERMEDIATE_SHA256,
    INTERMEDIATE_SUBJECT_CN,
    ISSUER_URL,
    PAKISTANCODE_HOSTS,
    intermediate_path,
    pakistancode_ssl_context,
    verify_for_source,
    verify_for_url,
)

LEAF = Path(__file__).resolve().parent / "fixtures" / "pakistancode_leaf.pem"
ROOT_CN = "USERTrust RSA Certification Authority"


def _load(path: Path) -> x509.Certificate:
    data = path.read_bytes()
    pem = data[data.index(b"-----BEGIN CERTIFICATE-----") :]
    return x509.load_pem_x509_certificate(pem)


def _cn(cert: x509.Certificate) -> str:
    values = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return values[0].value if values else ""


def _certifi_by_cn(common_name: str) -> x509.Certificate:
    from cryptography.utils import CryptographyDeprecationWarning

    blob = Path(certifi.where()).read_bytes()
    # A few roots in the public bundle have a non-positive serial. Skip those while searching.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", CryptographyDeprecationWarning)
        for chunk in blob.split(b"-----END CERTIFICATE-----"):
            if b"BEGIN CERTIFICATE" not in chunk:
                continue
            pem = chunk[chunk.index(b"-----BEGIN CERTIFICATE-----") :] + b"-----END CERTIFICATE-----\n"
            try:
                cert = x509.load_pem_x509_certificate(pem)
            except ValueError:
                continue
            if _cn(cert) == common_name:
                return cert
    raise AssertionError(f"{common_name} is not in the certifi bundle")


def _assert_signed_by(subject: x509.Certificate, issuer: x509.Certificate) -> None:
    issuer.public_key().verify(
        subject.signature,
        subject.tbs_certificate_bytes,
        padding.PKCS1v15(),
        subject.signature_hash_algorithm,
    )


def test_pinned_intermediate_matches_the_published_fingerprint_and_expiry():
    cert = _load(intermediate_path())
    assert ISSUER_URL == "http://crt.usertrust.com/GoGetSSLRSADVCA.crt"
    assert _cn(cert) == INTERMEDIATE_SUBJECT_CN
    assert cert.fingerprint(hashes.SHA256()).hex() == INTERMEDIATE_SHA256
    assert cert.not_valid_after_utc == INTERMEDIATE_NOT_AFTER
    assert cert.not_valid_after_utc.tzinfo == timezone.utc


def test_leaf_chains_through_the_pinned_intermediate_to_a_certifi_root():
    leaf = _load(LEAF)
    intermediate = _load(intermediate_path())
    root = _certifi_by_cn(ROOT_CN)
    assert _cn(leaf) == "pakistancode.gov.pk"
    assert leaf.issuer == intermediate.subject
    assert intermediate.issuer == root.subject
    _assert_signed_by(intermediate, root)
    _assert_signed_by(leaf, intermediate)


def test_ssl_context_keeps_verification_and_trusts_the_root_and_intermediate():
    ctx = pakistancode_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    names = {
        value
        for cert in ctx.get_ca_certs()
        for rdn in cert.get("subject", ())
        for key, value in rdn
        if key == "commonName"
    }
    assert INTERMEDIATE_SUBJECT_CN in names
    assert ROOT_CN in names
    assert ctx is pakistancode_ssl_context()


def test_extra_intermediate_is_only_used_for_pakistancode_hosts():
    pak = verify_for_url("https://pakistancode.gov.pk/english/index.php")
    www = verify_for_url("https://www.pakistancode.gov.pk/robots.txt")
    # na.gov.pk has its own pinned intermediate. This bundle stays on PakistanCode hosts.
    other = verify_for_url("https://example.com/robots.txt")
    assert pak is www
    assert isinstance(pak, ssl.SSLContext)
    assert other is True
    pinned = verify_for_source(
        SimpleNamespace(
            allow_list=["pakistancode.gov.pk", "www.pakistancode.gov.pk"],
            document_cdn_hosts=[],
        )
    )
    assert isinstance(pinned, ssl.SSLContext)
    fixture = verify_for_source(
        SimpleNamespace(allow_list=["127.0.0.1", "localhost"], document_cdn_hosts=[])
    )
    assert fixture is True
    mixed = verify_for_source(
        SimpleNamespace(
            allow_list=["pakistancode.gov.pk", "example.com"],
            document_cdn_hosts=[],
        )
    )
    assert mixed is True
    assert PAKISTANCODE_HOSTS == frozenset({"pakistancode.gov.pk", "www.pakistancode.gov.pk"})


def test_fetcher_and_robots_code_do_not_disable_certificate_verification():
    root = Path(__file__).resolve().parents[1]
    for relative in ("scraper/pakistancode_tls.py", "scraper/fetchers.py", "scraper/security.py"):
        text = (root / relative).read_text(encoding="utf-8")
        assert "verify=False" not in text
        assert "CERT_NONE" not in text
        assert "InsecureRequestWarning" not in text


async def test_pakistancode_client_uses_the_pinned_context():
    source = SimpleNamespace(
        source_name="PakistanCode",
        allow_list=["pakistancode.gov.pk", "www.pakistancode.gov.pk"],
        document_cdn_hosts=[],
        respect_robots=False,
        request_delay_min_ms=0,
        request_delay_max_ms=0,
    )
    async with HttpFetcher(source) as fetcher:
        assert isinstance(fetcher._verify, ssl.SSLContext)
        assert fetcher._verify.verify_mode == ssl.CERT_REQUIRED
        assert fetcher._verify.check_hostname is True


def test_updates_cadence_uses_scrape_frequency_hours_when_config_omits_it():
    source = SimpleNamespace(config_json={}, scrape_frequency_hours=48)
    assert update_interval_hours(source) == 48
    assert cadence_for_source(source, "updates") == 48 * 60
    overridden = SimpleNamespace(config_json={"update_frequency_hours": 6}, scrape_frequency_hours=48)
    assert cadence_for_source(overridden, "updates") == 6 * 60
    unset = SimpleNamespace(config_json={}, scrape_frequency_hours=0)
    assert update_interval_hours(unset) == 6


async def test_seeded_pakistancode_runs_every_48_hours(db):
    source = (
        await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanCode"))
    ).scalars().one()
    assert source.scrape_frequency_hours == 48
    assert "update_frequency_hours" not in (source.config_json or {})
    assert cadence_for_source(source, "updates") == 48 * 60
    assert update_interval_hours(source) == 48
