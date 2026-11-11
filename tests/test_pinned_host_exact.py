"""Pinned TLS hosts are an exact match.

``evil-na.gov.pk``, ``na.gov.pk.evil.com`` and a trailing-dot FQDN must stay on
the default verifier, and the same for pakistancode.gov.pk. ``_suffix_match_mutation``
accepts every one of those names, so these assertions fail if that check replaces
exact membership.
"""

from __future__ import annotations

import ssl
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from scraper.pakistancode_tls import pakistancode_ssl_context, verify_for_source, verify_for_url


def _suffix_match_mutation(host: str, pin: str) -> bool:
    """The weakened check these tests exist to reject.

    A trailing DNS root dot is ignored, and the pinned name may be a prefix or
    a suffix of the host. Exact membership does none of that.
    """
    bare = (host or "").lower().rstrip(".")
    needle = pin.lower().rstrip(".")
    return bare == needle or bare.endswith(needle) or bare.startswith(needle)


LOOKALIKES = (
    ("https://evil-na.gov.pk/robots.txt", "na.gov.pk"),
    ("https://na.gov.pk.evil.com/robots.txt", "na.gov.pk"),
    ("https://na.gov.pk./robots.txt", "na.gov.pk"),
    ("https://attacker.na.gov.pk/robots.txt", "na.gov.pk"),
    ("https://evil-www.na.gov.pk/robots.txt", "www.na.gov.pk"),
    ("https://www.na.gov.pk.evil.com/robots.txt", "www.na.gov.pk"),
    ("https://www.na.gov.pk./robots.txt", "www.na.gov.pk"),
    ("https://evil-pakistancode.gov.pk/robots.txt", "pakistancode.gov.pk"),
    ("https://pakistancode.gov.pk.evil.com/robots.txt", "pakistancode.gov.pk"),
    ("https://pakistancode.gov.pk./robots.txt", "pakistancode.gov.pk"),
    ("https://attacker.pakistancode.gov.pk/robots.txt", "pakistancode.gov.pk"),
    ("https://evil-www.pakistancode.gov.pk/robots.txt", "www.pakistancode.gov.pk"),
    ("https://www.pakistancode.gov.pk.evil.com/robots.txt", "www.pakistancode.gov.pk"),
    ("https://www.pakistancode.gov.pk./robots.txt", "www.pakistancode.gov.pk"),
)


@pytest.mark.parametrize("url,pin", LOOKALIKES)
def test_lookalike_host_does_not_use_the_pinned_bundle(url, pin):
    host = urlsplit(url).hostname or ""
    assert _suffix_match_mutation(host, pin)
    assert verify_for_url(url) is True


def test_exact_pinned_hosts_still_use_their_own_bundles():
    na = verify_for_url("https://na.gov.pk/robots.txt")
    pak = verify_for_url("https://pakistancode.gov.pk/english/index.php")
    assert isinstance(na, ssl.SSLContext)
    assert isinstance(pak, ssl.SSLContext)
    assert na is verify_for_url("https://www.na.gov.pk/robots.txt")
    assert pak is pakistancode_ssl_context()
    assert pak is verify_for_url("https://www.pakistancode.gov.pk/robots.txt")
    assert na is not pak
    assert na.verify_mode == ssl.CERT_REQUIRED and na.check_hostname is True
    assert pak.verify_mode == ssl.CERT_REQUIRED and pak.check_hostname is True


def test_lookalike_allow_list_does_not_install_a_pinned_bundle():
    na_lookalikes = verify_for_source(
        SimpleNamespace(
            allow_list=["evil-na.gov.pk", "na.gov.pk.evil.com", "na.gov.pk."],
            document_cdn_hosts=[],
        )
    )
    pak_lookalikes = verify_for_source(
        SimpleNamespace(
            allow_list=["evil-pakistancode.gov.pk", "pakistancode.gov.pk.evil.com", "pakistancode.gov.pk."],
            document_cdn_hosts=[],
        )
    )
    assert na_lookalikes is True
    assert pak_lookalikes is True
    exact = verify_for_source(
        SimpleNamespace(allow_list=["na.gov.pk", "www.na.gov.pk"], document_cdn_hosts=[])
    )
    assert isinstance(exact, ssl.SSLContext)
