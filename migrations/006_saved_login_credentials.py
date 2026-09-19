"""
006 — Encrypted PakistanLawSite username/password persistence per login slot.

Adds encrypted credential columns to browser_session_slots so operators can keep
two sticky account slots (primary + alternate) on the trusted host.
"""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    await conn.execute(text("ALTER TABLE browser_session_slots ADD COLUMN IF NOT EXISTS login_username_encrypted TEXT"))
    await conn.execute(text("ALTER TABLE browser_session_slots ADD COLUMN IF NOT EXISTS login_password_encrypted TEXT"))
    await conn.execute(text("ALTER TABLE browser_session_slots ADD COLUMN IF NOT EXISTS login_credentials_updated_at TIMESTAMPTZ"))
    await conn.execute(text("ALTER TABLE browser_session_slots ADD COLUMN IF NOT EXISTS login_credentials_updated_by VARCHAR(200)"))
