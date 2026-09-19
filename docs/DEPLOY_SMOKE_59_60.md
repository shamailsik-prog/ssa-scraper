# Deploy smoke for #59 + #60 continuity controls

Thin operator smoke for:

- #59 harvest mode + source controls continuity.
- #60 encrypted PakistanLawSite dual-slot saved credentials continuity.

This checklist is safe to run without real PakistanLawSite credentials.

## 1) Inventory: knobs and where they are documented

| Area | Knobs / controls | Where documented |
|---|---|---|
| Harvest mode | `HARVEST_MODE` (`backfill` or `updates`), `HARVEST_AUTO_SWITCH`, `UPDATE_CADENCE_HOURS`, `BACKFILL_SOURCE_FREQUENCY_MINUTES`, `BACKFILL_LOGIN_DELAY_MIN`, `BACKFILL_LOGIN_DELAY_MAX`, `BACKFILL_PAGES_PER_HOUR`, `BACKFILL_PAGES_PER_DAY`, `BACKFILL_LOGIN_SESSION_CONCURRENCY` | `.env.example`, `README.md`, `docs/CLOUD_DEPLOYMENT.md` |
| Source controls | `allow_list`, `document_cdn_hosts`, per-mode enablement (`backfill_enabled`, `update_enabled`) via `/admin/sources/{name}/config` | `/admin/sources` API + dashboard scheduler/config panels |
| Dual-slot credentials | slot `1` + slot `2` saved credentials, encrypted at rest by `ENCRYPTION_KEY` | `README.md`, `docs/CLOUD_DEPLOYMENT.md`, `/admin/sessions` API |

## 2) Pre-flight on droplet

```bash
cd /opt/ssa-scraper
docker compose ps
curl -fsS http://127.0.0.1:8000/health
```

Set your local shell variables (do not commit these anywhere):

```bash
export BASE_URL="https://<your-domain-or-ip>"
export ADMIN_KEY="<admin-api-key>"
```

## 3) Harvest mode smoke (backfill vs updates)

```bash
curl -fsS -H "X-API-Key: $ADMIN_KEY" "$BASE_URL/admin/sources/harvest-mode"
```

Expected: `"mode"` is `backfill` or `updates`.

Flip mode to `backfill`, then back to `updates`:

```bash
curl -fsS -X POST -H "Content-Type: application/json" -H "X-API-Key: $ADMIN_KEY" \
  "$BASE_URL/admin/sources/harvest-mode" \
  -d '{"mode":"backfill","changed_by":"ops-smoke","reason":"deploy smoke #59"}'

curl -fsS -X POST -H "Content-Type: application/json" -H "X-API-Key: $ADMIN_KEY" \
  "$BASE_URL/admin/sources/harvest-mode" \
  -d '{"mode":"updates","changed_by":"ops-smoke","reason":"deploy smoke #59 restore"}'
```

Expected: both calls succeed and echo the chosen mode.

## 4) Source allow/deny controls smoke

Inspect one public source (example `SupremeCourt`):

```bash
curl -fsS -H "X-API-Key: $ADMIN_KEY" "$BASE_URL/admin/sources/SupremeCourt/status"
```

Patch host controls:

```bash
curl -fsS -X POST -H "Content-Type: application/json" -H "X-API-Key: $ADMIN_KEY" \
  "$BASE_URL/admin/sources/SupremeCourt/config" \
  -d '{"allow_list":["www.supremecourt.gov.pk"],"document_cdn_hosts":["cdn.supremecourt.gov.pk"],"backfill_enabled":true,"update_enabled":true}'
```

Expected: response shows normalized lowercase `allow_list` and scheduler flags.

To deny dispatch without deleting the source, disable both mode flags:

```bash
curl -fsS -X POST -H "Content-Type: application/json" -H "X-API-Key: $ADMIN_KEY" \
  "$BASE_URL/admin/sources/SupremeCourt/config" \
  -d '{"backfill_enabled":false,"update_enabled":false}'
```

Re-enable after smoke:

```bash
curl -fsS -X POST -H "Content-Type: application/json" -H "X-API-Key: $ADMIN_KEY" \
  "$BASE_URL/admin/sources/SupremeCourt/config" \
  -d '{"backfill_enabled":true,"update_enabled":true}'
```

## 5) Dual-slot saved credentials fail-closed smoke

No real credentials required.

1. Clear both slots:

```bash
curl -fsS -X POST -H "X-API-Key: $ADMIN_KEY" "$BASE_URL/admin/sessions/PakistanLawSite/credentials/1/clear"
curl -fsS -X POST -H "X-API-Key: $ADMIN_KEY" "$BASE_URL/admin/sessions/PakistanLawSite/credentials/2/clear"
```

2. Start human login with saved credentials enabled:

```bash
curl -fsS -X POST -H "Content-Type: application/json" -H "X-API-Key: $ADMIN_KEY" \
  "$BASE_URL/admin/sessions/PakistanLawSite/login/start" \
  -d '{"slot":1,"use_saved_credentials":true,"auto_complete_if_empty":true}'
```

Expected: `"saved_credentials_used": false` (empty slot must fail closed, not crash).

3. Malformed token simulation (operator-only SQL check):

```bash
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<'SQL'
UPDATE browser_session_slots
SET login_username_encrypted='bad-token',
    login_password_encrypted='bad-token'
WHERE source_name='PakistanLawSite' AND slot_number=1;
SQL
```

Repeat the `/login/start` call from step 2.

Expected: still `200`, `"saved_credentials_used": false`, no server error.

Clean up:

```bash
curl -fsS -X POST -H "X-API-Key: $ADMIN_KEY" "$BASE_URL/admin/sessions/PakistanLawSite/credentials/1/clear"
curl -fsS -X POST -H "X-API-Key: $ADMIN_KEY" "$BASE_URL/admin/sessions/PakistanLawSite/login/cancel"
```

## 6) In-repo automated thin smoke

Run only the focused checks:

```bash
pytest -q tests/test_deploy_smoke_59_60.py tests/test_settings_and_roles.py tests/test_auth_playwright.py tests/test_api.py
```

These tests verify env knob names, harvest mode parsing, and fail-closed credential load behavior.
