"""
Which sources this server harvests (DEPLOY_ROLE).

  all     one server harvests everything (the original single-server set-up; the default)
  pls     PakistanLawSite only: the login-session sources
  public  the free sites only: courts' websites, PakistanCode, legislatures, gazettes

The two roles run on two servers with their own database, queues and workers, so trouble on one (a
stuck PakistanLawSite login, a slow court website) can never hold up the other. Both hand documents on
in the shared corpus record format (scraper/corpus_record.py).
"""

from __future__ import annotations

from typing import Any, Dict

from scraper.config import settings

# Beat entries that only make sense where PakistanLawSite is harvested: they sign in, keep the login
# alive, watch PakistanLawSite for stalls, walk its search grid, or re-fetch through the login session.
LOGIN_ONLY_BEAT_ENTRIES = frozenset(
    {"recover-login-slots", "pls-keepalive", "pls-stall-watchdog", "spot-check-judgments", "pls-search-harvest", "pls-caseid-walk", "pls-caseid-walk-p1"}
)
# Beat entries that only make sense where the free sites are harvested (statutes come from them).
PUBLIC_ONLY_BEAT_ENTRIES = frozenset({"spot-check-statutes", "reconcile-instrument-relations"})


def deploy_role() -> str:
    return settings.DEPLOY_ROLE


def harvests_login_sources() -> bool:
    return deploy_role() in ("all", "pls")


def harvests_public_sources() -> bool:
    return deploy_role() in ("all", "public")


def harvests(access_method: str) -> bool:
    """Whether this server runs a source with this access method."""
    return harvests_login_sources() if access_method == "login_session" else harvests_public_sources()


def beat_schedule_for_role(schedule: Dict[str, Any]) -> Dict[str, Any]:
    """The Beat schedule without the entries that belong to the other server."""
    drop = set()
    if not harvests_login_sources():
        drop |= LOGIN_ONLY_BEAT_ENTRIES
    if not harvests_public_sources():
        drop |= PUBLIC_ONLY_BEAT_ENTRIES
    return {name: entry for name, entry in schedule.items() if name not in drop}
