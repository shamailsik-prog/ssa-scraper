"""While a deploy waits for the PakistanLawSite job to finish it sets the Redis key deploy_hold_login
(scripts/pls_host_lib.sh). New PakistanLawSite work (login jobs, search-harvest and case-ID walk ticks,
slot recovery, keepalive, the stall watchdog and the judgment spot check) does not start while it is set; every other source, promotion and the mirror keep running, so a deploy no
longer has to stop celery-beat. The key expires on its own, and a Redis error never holds anything."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

LOGIN_HOLD_KEY = "deploy_hold_login"

# Raised whenever more entry points start honouring the hold. auto_deploy.sh uses the hold only when the
# running workers report at least the version it needs (scripts/pls_host_lib.sh), so a deploy never relies
# on a hold that the code still running would partly ignore.
HOLD_GUARDS_VERSION = 2


def login_work_held() -> bool:
    try:
        from scraper.tasks.chain import _redis

        return bool(_redis().exists(LOGIN_HOLD_KEY))
    except Exception as exc:
        logger.warning("deploy hold not readable (%s); not holding", exc)
        return False
