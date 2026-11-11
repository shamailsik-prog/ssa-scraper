"""TLS trust for origins that omit the intermediate which signed their leaf.

pakistancode.gov.pk is signed by GoGetSSL RSA DV CA. The handshake does not send
that intermediate, so a normal certifi/system verify fails with "unable to get
local issuer certificate".

na.gov.pk is the same class of failure with a different issuer. The leaf
(CN=na.gov.pk, also www.na.gov.pk) is signed by GoGetSSL RSA DV SSL CA 2. The
handshake sends other GoGetSSL and USERTrust certificates that do not sign the
leaf. The missing intermediate is the one named by the leaf AIA CA Issuers URL.

Each host gets its own bundle: system roots, plus certifi, plus that host's
pinned public intermediate. Verification stays on (hostname checks and
CERT_REQUIRED). A client whose allow-list is not entirely inside one of these
host sets keeps the default verifier.
"""

from __future__ import annotations

import ssl
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Union
from urllib.parse import urlsplit

import certifi

PAKISTANCODE_HOSTS = frozenset({"pakistancode.gov.pk", "www.pakistancode.gov.pk"})
ISSUER_URL = "http://crt.usertrust.com/GoGetSSLRSADVCA.crt"
INTERMEDIATE_SHA256 = "43cac31ef8e8ba1b4b16b8206e4c0a26c5badb2fc3aa09e90170e41b66c2fd64"
INTERMEDIATE_NOT_AFTER = datetime(2028, 9, 5, 23, 59, 59, tzinfo=timezone.utc)
INTERMEDIATE_SUBJECT_CN = "GoGetSSL RSA DV CA"

NA_HOSTS = frozenset({"na.gov.pk", "www.na.gov.pk"})
NA_ISSUER_URL = "http://crt.sectigo.com/GoGetSSLRSADVSSLCA2.crt"
NA_INTERMEDIATE_SHA256 = "b5286796dadf16521de41772ab2fdb5818729971b5274721146c6e8172befe07"
NA_INTERMEDIATE_NOT_AFTER = datetime(2035, 8, 19, 23, 59, 59, tzinfo=timezone.utc)
NA_INTERMEDIATE_SUBJECT_CN = "GoGetSSL RSA DV SSL CA 2"

_CERT_PATH = Path(__file__).resolve().parents[1] / "certs" / "GoGetSSL_RSA_DV_CA.pem"
_NA_CERT_PATH = Path(__file__).resolve().parents[1] / "certs" / "GoGetSSL_RSA_DV_SSL_CA_2.pem"


def intermediate_path() -> Path:
    return _CERT_PATH


def na_intermediate_path() -> Path:
    return _NA_CERT_PATH


def _ssl_context(cafile: Path, label: str) -> ssl.SSLContext:
    """System roots, plus certifi, plus one pinned intermediate.

    ``load_verify_locations`` adds to the store created by ``create_default_context``.
    The context keeps certificate and hostname verification enabled.
    """
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=certifi.where())
    ctx.load_verify_locations(cafile=str(cafile))
    if ctx.verify_mode != ssl.CERT_REQUIRED or not ctx.check_hostname:
        raise RuntimeError(f"{label} TLS context must verify certificates and hostnames")
    return ctx


@lru_cache(maxsize=1)
def pakistancode_ssl_context() -> ssl.SSLContext:
    return _ssl_context(intermediate_path(), "PakistanCode")


@lru_cache(maxsize=1)
def na_ssl_context() -> ssl.SSLContext:
    return _ssl_context(na_intermediate_path(), "NationalAssembly")


def verify_for_url(url: str) -> Union[ssl.SSLContext, bool]:
    """SSL material for one URL. Each pinned intermediate is only for its own hosts."""
    host = (urlsplit(url).hostname or "").lower()
    if host in PAKISTANCODE_HOSTS:
        return pakistancode_ssl_context()
    if host in NA_HOSTS:
        return na_ssl_context()
    return True


def _host_labels(values) -> set[str]:
    hosts: set[str] = set()
    for raw in values or []:
        label = str(raw or "").strip().lower().split(":")[0]
        if label:
            hosts.add(label)
    return hosts


def verify_for_source(source) -> Union[ssl.SSLContext, bool]:
    """Client verify value when every host the source may contact shares one pinned bundle.

    A source pointed at a fixture host, or at hosts from more than one pinned set,
    keeps the default verifier. The augmented bundle is not installed on a client
    that also talks to other sites.
    """
    hosts = _host_labels(getattr(source, "allow_list", None)) | _host_labels(getattr(source, "document_cdn_hosts", None))
    if hosts and hosts <= PAKISTANCODE_HOSTS:
        return pakistancode_ssl_context()
    if hosts and hosts <= NA_HOSTS:
        return na_ssl_context()
    return True
