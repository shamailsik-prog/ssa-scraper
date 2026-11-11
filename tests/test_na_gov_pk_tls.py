"""na.gov.pk presents a leaf signed by GoGetSSL RSA DV SSL CA 2 and omits that intermediate.

The pinned public intermediate (AIA http://crt.sectigo.com/GoGetSSLRSADVSSLCA2.crt) is appended
to the system and certifi roots for na.gov.pk and its www alias only. Verification stays on.
Other hosts keep the default verifier.
"""

from __future__ import annotations

import ssl
import warnings
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509.oid import NameOID

from scraper.fetchers import HttpFetcher
from scraper.pakistancode_tls import (
    pakistancode_ssl_context,
    verify_for_source,
    verify_for_url,
)

LEAF = Path(__file__).resolve().parent / "fixtures" / "na_gov_pk_leaf.pem"
LEAF_SHA256 = "806594f88fdf08dfb1bb412edb5994be31fed0e1d4ffc5072af39efab3aa3467"
INTERMEDIATE_SHA256 = "b5286796dadf16521de41772ab2fdb5818729971b5274721146c6e8172befe07"
INTERMEDIATE_NOT_AFTER = datetime(2035, 8, 19, 23, 59, 59, tzinfo=timezone.utc)
ISSUER_URL = "http://crt.sectigo.com/GoGetSSLRSADVSSLCA2.crt"
INTERMEDIATE_CN = "GoGetSSL RSA DV SSL CA 2"
ROOT_CN = "Sectigo Public Server Authentication Root R46"
PAKISTANCODE_INTERMEDIATE_CN = "GoGetSSL RSA DV CA"


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


def _ca_names(ctx: ssl.SSLContext) -> set[str]:
    return {
        value
        for cert in ctx.get_ca_certs()
        for rdn in cert.get("subject", ())
        for key, value in rdn
        if key == "commonName"
    }


def test_na_gov_pk_uses_the_pinned_bundle_and_other_hosts_do_not():
    from scraper.pakistancode_tls import NA_HOSTS, na_ssl_context

    na = verify_for_url("https://na.gov.pk/robots.txt")
    www = verify_for_url("https://www.na.gov.pk/en/acts-tenure.php")
    upper = verify_for_url("https://NA.gov.pk/robots.txt")
    assert na is www is upper
    assert na is na_ssl_context()
    assert isinstance(na, ssl.SSLContext)
    assert na.verify_mode == ssl.CERT_REQUIRED
    assert na.check_hostname is True
    assert na is not pakistancode_ssl_context()

    for url in (
        "https://example.com/robots.txt",
        "https://opc.lhc.gov.pk/Relevant_Laws.aspx",
        "https://punjablaws.gov.pk/",
        "https://www.punjablaws.gov.pk/",
        "https://www.federalshariatcourt.gov.pk/en/judgments/",
        "https://pcp.gov.pk/",
        "https://senate.gov.pk/robots.txt",
    ):
        assert verify_for_url(url) is True, url

    assembly = verify_for_source(
        SimpleNamespace(allow_list=["na.gov.pk", "www.na.gov.pk"], document_cdn_hosts=[])
    )
    assert assembly is na
    lhc = verify_for_source(
        SimpleNamespace(
            allow_list=["opc.lhc.gov.pk", "sys.lhc.gov.pk", "lhc.gov.pk", "www.lhc.gov.pk"],
            document_cdn_hosts=[],
        )
    )
    assert lhc is True
    punjab = verify_for_source(
        SimpleNamespace(
            allow_list=["www.pap.gov.pk", "pap.gov.pk", "punjablaws.gov.pk", "www.punjablaws.gov.pk"],
            document_cdn_hosts=[],
        )
    )
    assert punjab is True
    mixed = verify_for_source(
        SimpleNamespace(allow_list=["na.gov.pk", "pakistancode.gov.pk"], document_cdn_hosts=[])
    )
    assert mixed is True
    assert NA_HOSTS == frozenset({"na.gov.pk", "www.na.gov.pk"})


def test_pinned_intermediate_matches_the_published_fingerprint_and_expiry():
    from scraper.pakistancode_tls import (
        NA_INTERMEDIATE_NOT_AFTER,
        NA_INTERMEDIATE_SHA256,
        NA_INTERMEDIATE_SUBJECT_CN,
        NA_ISSUER_URL,
        na_intermediate_path,
    )

    cert = _load(na_intermediate_path())
    assert NA_ISSUER_URL == ISSUER_URL
    assert NA_INTERMEDIATE_SUBJECT_CN == INTERMEDIATE_CN
    assert NA_INTERMEDIATE_SHA256 == INTERMEDIATE_SHA256
    assert NA_INTERMEDIATE_NOT_AFTER == INTERMEDIATE_NOT_AFTER
    assert _cn(cert) == INTERMEDIATE_CN
    assert cert.fingerprint(hashes.SHA256()).hex() == INTERMEDIATE_SHA256
    assert cert.not_valid_after_utc == INTERMEDIATE_NOT_AFTER


def test_leaf_chains_through_the_pinned_intermediate_to_a_certifi_root():
    from scraper.pakistancode_tls import na_intermediate_path

    leaf = _load(LEAF)
    intermediate = _load(na_intermediate_path())
    root = _certifi_by_cn(ROOT_CN)
    assert leaf.fingerprint(hashes.SHA256()).hex() == LEAF_SHA256
    assert _cn(leaf) == "na.gov.pk"
    assert leaf.issuer == intermediate.subject
    assert intermediate.issuer == root.subject
    _assert_signed_by(intermediate, root)
    _assert_signed_by(leaf, intermediate)


def test_na_context_trusts_only_its_intermediate_and_keeps_verification():
    from scraper.pakistancode_tls import na_ssl_context

    ctx = na_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    names = _ca_names(ctx)
    assert INTERMEDIATE_CN in names
    assert ROOT_CN in names
    assert PAKISTANCODE_INTERMEDIATE_CN not in names
    pak_names = _ca_names(pakistancode_ssl_context())
    assert PAKISTANCODE_INTERMEDIATE_CN in pak_names
    assert INTERMEDIATE_CN not in pak_names
    assert ctx is na_ssl_context()


def test_fetcher_code_does_not_disable_certificate_verification():
    root = Path(__file__).resolve().parents[1]
    for relative in ("scraper/pakistancode_tls.py", "scraper/fetchers.py", "scraper/security.py"):
        text = (root / relative).read_text(encoding="utf-8")
        assert "verify=False" not in text
        assert "CERT_NONE" not in text


async def test_national_assembly_client_uses_the_pinned_context():
    source = SimpleNamespace(
        source_name="NationalAssembly",
        allow_list=["na.gov.pk", "www.na.gov.pk"],
        document_cdn_hosts=[],
        respect_robots=False,
        request_delay_min_ms=0,
        request_delay_max_ms=0,
    )
    async with HttpFetcher(source) as fetcher:
        assert isinstance(fetcher._verify, ssl.SSLContext)
        assert fetcher._verify.verify_mode == ssl.CERT_REQUIRED
        assert fetcher._verify.check_hostname is True
        assert fetcher._verify is verify_for_url("https://na.gov.pk/robots.txt")
