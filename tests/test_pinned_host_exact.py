"""Pinned TLS hosts are an exact match.

``evil-na.gov.pk``, ``xna.gov.pk``, ``na.gov.pk.evil.com`` and a trailing-dot FQDN
must stay on the default verifier, and the same for pakistancode.gov.pk. Case,
``user@host`` and an explicit port do not change that. ``_suffix_match_mutation``
accepts every lookalike, so these assertions fail if that check replaces exact
membership. The real hosts, including those same case, userinfo and port forms,
keep their own bundles.
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
    # x-prefix, case, user@host, and an explicit port. A suffix check accepts each one.
    ("https://xna.gov.pk/robots.txt", "na.gov.pk"),
    ("https://XNA.GOV.PK/robots.txt", "na.gov.pk"),
    ("https://user@xna.gov.pk/robots.txt", "na.gov.pk"),
    ("https://user:pass@XNA.GOV.PK:443/robots.txt", "na.gov.pk"),
    ("https://xna.gov.pk:443/robots.txt", "na.gov.pk"),
    ("https://www.xna.gov.pk/robots.txt", "na.gov.pk"),
    ("https://EVIL-NA.GOV.PK/robots.txt", "na.gov.pk"),
    ("https://user@evil-na.gov.pk/robots.txt", "na.gov.pk"),
    ("https://NA.GOV.PK.EVIL.COM:8443/robots.txt", "na.gov.pk"),
    ("https://NA.GOV.PK./robots.txt", "na.gov.pk"),
    ("https://xwww.na.gov.pk/robots.txt", "www.na.gov.pk"),
    ("https://XWWW.NA.GOV.PK/robots.txt", "www.na.gov.pk"),
    ("https://user@xwww.na.gov.pk/robots.txt", "www.na.gov.pk"),
    ("https://xwww.na.gov.pk:443/robots.txt", "www.na.gov.pk"),
    ("https://EVIL-WWW.NA.GOV.PK/robots.txt", "www.na.gov.pk"),
    ("https://user@EVIL-WWW.NA.GOV.PK:8443/robots.txt", "www.na.gov.pk"),
    ("https://xpakistancode.gov.pk/robots.txt", "pakistancode.gov.pk"),
    ("https://XPAKISTANCODE.GOV.PK/robots.txt", "pakistancode.gov.pk"),
    ("https://user@xpakistancode.gov.pk/robots.txt", "pakistancode.gov.pk"),
    ("https://user:pass@XPAKISTANCODE.GOV.PK:443/robots.txt", "pakistancode.gov.pk"),
    ("https://xpakistancode.gov.pk:443/robots.txt", "pakistancode.gov.pk"),
    ("https://www.xpakistancode.gov.pk/robots.txt", "pakistancode.gov.pk"),
    ("https://EVIL-PAKISTANCODE.GOV.PK/robots.txt", "pakistancode.gov.pk"),
    ("https://user@evil-pakistancode.gov.pk/robots.txt", "pakistancode.gov.pk"),
    ("https://PAKISTANCODE.GOV.PK.EVIL.COM:8443/robots.txt", "pakistancode.gov.pk"),
    ("https://PAKISTANCODE.GOV.PK./robots.txt", "pakistancode.gov.pk"),
    ("https://xwww.pakistancode.gov.pk/robots.txt", "www.pakistancode.gov.pk"),
    ("https://XWWW.PAKISTANCODE.GOV.PK/robots.txt", "www.pakistancode.gov.pk"),
    ("https://user@xwww.pakistancode.gov.pk/robots.txt", "www.pakistancode.gov.pk"),
    ("https://xwww.pakistancode.gov.pk:443/robots.txt", "www.pakistancode.gov.pk"),
    ("https://EVIL-WWW.PAKISTANCODE.GOV.PK/robots.txt", "www.pakistancode.gov.pk"),
    ("https://user@EVIL-WWW.PAKISTANCODE.GOV.PK:8443/robots.txt", "www.pakistancode.gov.pk"),
)


# Case, userinfo and port on the real names still select that name's bundle.
EXACT_HOSTS = (
    ("https://na.gov.pk/robots.txt", "na"),
    ("https://NA.GOV.PK/robots.txt", "na"),
    ("https://Na.Gov.Pk/robots.txt", "na"),
    ("https://WWW.NA.GOV.PK/robots.txt", "na"),
    ("https://user@na.gov.pk/robots.txt", "na"),
    ("https://user:pass@www.na.gov.pk/robots.txt", "na"),
    ("https://na.gov.pk:443/robots.txt", "na"),
    ("https://www.na.gov.pk:8443/robots.txt", "na"),
    ("https://user:pass@NA.GOV.PK:443/robots.txt", "na"),
    ("https://user@WWW.NA.GOV.PK:8443/en", "na"),
    ("https://pakistancode.gov.pk/english/index.php", "pak"),
    ("https://PAKISTANCODE.GOV.PK/english/index.php", "pak"),
    ("https://WWW.PAKISTANCODE.GOV.PK/robots.txt", "pak"),
    ("https://user@pakistancode.gov.pk/robots.txt", "pak"),
    ("https://user:pass@www.pakistancode.gov.pk:443/robots.txt", "pak"),
    ("https://pakistancode.gov.pk:8443/robots.txt", "pak"),
    ("https://user@PAKISTANCODE.GOV.PK:443/english/index.php", "pak"),
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
            allow_list=[
                "evil-na.gov.pk",
                "na.gov.pk.evil.com",
                "na.gov.pk.",
                "xna.gov.pk",
                "XNA.GOV.PK",
                "user@xna.gov.pk",
                "xna.gov.pk:443",
            ],
            document_cdn_hosts=[],
        )
    )
    pak_lookalikes = verify_for_source(
        SimpleNamespace(
            allow_list=[
                "evil-pakistancode.gov.pk",
                "pakistancode.gov.pk.evil.com",
                "pakistancode.gov.pk.",
                "xpakistancode.gov.pk",
                "XPAKISTANCODE.GOV.PK",
                "user@xpakistancode.gov.pk",
                "xpakistancode.gov.pk:443",
            ],
            document_cdn_hosts=[],
        )
    )
    assert na_lookalikes is True
    assert pak_lookalikes is True
    exact = verify_for_source(
        SimpleNamespace(allow_list=["na.gov.pk", "www.na.gov.pk"], document_cdn_hosts=[])
    )
    assert isinstance(exact, ssl.SSLContext)
    exact_case_port = verify_for_source(
        SimpleNamespace(allow_list=["NA.GOV.PK", "WWW.NA.GOV.PK:443"], document_cdn_hosts=[])
    )
    assert isinstance(exact_case_port, ssl.SSLContext)
    assert exact_case_port.verify_mode == ssl.CERT_REQUIRED and exact_case_port.check_hostname is True
    pak_exact = verify_for_source(
        SimpleNamespace(
            allow_list=["PAKISTANCODE.GOV.PK:443", "WWW.PAKISTANCODE.GOV.PK"],
            document_cdn_hosts=[],
        )
    )
    assert pak_exact is pakistancode_ssl_context()


@pytest.mark.parametrize("url,which", EXACT_HOSTS)
def test_exact_real_hosts_keep_their_pin(url, which):
    got = verify_for_url(url)
    assert isinstance(got, ssl.SSLContext)
    assert got.verify_mode == ssl.CERT_REQUIRED and got.check_hostname is True
    if which == "na":
        assert got is verify_for_url("https://na.gov.pk/robots.txt")
    else:
        assert got is pakistancode_ssl_context()
