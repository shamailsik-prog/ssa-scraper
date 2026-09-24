"""
008 — Restore the saved PakistanLawSite credential columns on browser_session_slots.

Release #123 (deployed 24 September 2026, 03:01 UTC) shipped a migration 007 that dropped these
columns; the operator then chose automatic, unattended operation on the firm's two logins and #123
was reverted. The server's schema_migrations still records 007, so the columns come back here.
The credentials that 007 deleted are not recoverable: the operator saves them again on the
dashboard's Human login tab. Idempotent (a database that never ran 007 is unchanged).
"""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    await conn.execute(text("ALTER TABLE browser_session_slots ADD COLUMN IF NOT EXISTS login_username_encrypted TEXT"))
    await conn.execute(text("ALTER TABLE browser_session_slots ADD COLUMN IF NOT EXISTS login_password_encrypted TEXT"))
    await conn.execute(text("ALTER TABLE browser_session_slots ADD COLUMN IF NOT EXISTS login_credentials_updated_at TIMESTAMPTZ"))
    await conn.execute(text("ALTER TABLE browser_session_slots ADD COLUMN IF NOT EXISTS login_credentials_updated_by VARCHAR(200)"))
