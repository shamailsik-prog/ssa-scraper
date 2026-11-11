"""TLS trust for pakistancode.gov.pk.

The origin certificate (CN=pakistancode.gov.pk) is signed by GoGetSSL RSA DV CA,
but the handshake does not send that intermediate. It sends a different GoGetSSL
certificate that does not sign the leaf, so a normal certifi/system verify fails
with "unable to get local issuer certificate".

This module appends the pinned public intermediate, fetched from the leaf's AIA
issuer URL, to the system and certifi roots. Verification stays on: hostname
checks and CERT_REQUIRED are unchanged, and the bundle is used only for
pakistancode.gov.pk / www.pakistancode.gov.pk.
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

_CERT_PATH = Path(__file__).resolve().parents[1] / "certs" / "GoGetSSL_RSA_DV_CA.pem"


def intermediate_path() -> Path:
    return _CERT_PATH


@lru_cache(maxsize=1)
def pakistancode_ssl_context() -> ssl.SSLContext:
    """System roots, plus certifi, plus the pinned GoGetSSL intermediate.

    ``load_verify_locations`` adds to the store created by ``create_default_context``.
    The context keeps certificate and hostname verification enabled.
    """
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=certifi.where())
    ctx.load_verify_locations(cafile=str(intermediate_path()))
    if ctx.verify_mode != ssl.CERT_REQUIRED or not ctx.check_hostname:
        raise RuntimeError("PakistanCode TLS context must verify certificates and hostnames")
    return ctx


def verify_for_url(url: str) -> Union[ssl.SSLContext, bool]:
    """SSL material for one URL. The extra intermediate is only for PakistanCode hosts."""
    host = (urlsplit(url).hostname or "").lower()
    if host in PAKISTANCODE_HOSTS:
        return pakistancode_ssl_context()
    return True


def _host_labels(values) -> set[str]:
    hosts: set[str] = set()
    for raw in values or []:
        label = str(raw or "").strip().lower().split(":")[0]
        if label:
            hosts.add(label)
    return hosts


def verify_for_source(source) -> Union[ssl.SSLContext, bool]:
    """Client verify value when every host the source may contact is PakistanCode.

    A source pointed at a fixture host keeps the default verifier. The augmented
    bundle is not installed on a client that also talks to other sites.
    """
    hosts = _host_labels(getattr(source, "allow_list", None)) | _host_labels(getattr(source, "document_cdn_hosts", None))
    if hosts and hosts <= PAKISTANCODE_HOSTS:
        return pakistancode_ssl_context()
    return True
