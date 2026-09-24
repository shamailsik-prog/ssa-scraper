"""
008 — Restore the saved PakistanLawSite credential columns on browser_session_slots.

Migration 007 dropped them to match section 0.5 of the working specification. On 24 September
2026 the operator, after the human login had succeeded, instructed that the passwords be saved
again and used to sign in automatically (with the box below the password ticked). The columns
come back; the values are Fernet-encrypted with ENCRYPTION_KEY and never echoed by any API.
"""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    await conn.execute(text("ALTER TABLE browser_session_slots ADD COLUMN IF NOT EXISTS login_username_encrypted TEXT"))
    await conn.execute(text("ALTER TABLE browser_session_slots ADD COLUMN IF NOT EXISTS login_password_encrypted TEXT"))
    await conn.execute(text("ALTER TABLE browser_session_slots ADD COLUMN IF NOT EXISTS login_credentials_updated_at TIMESTAMPTZ"))
    await conn.execute(text("ALTER TABLE browser_session_slots ADD COLUMN IF NOT EXISTS login_credentials_updated_by VARCHAR(200)"))
