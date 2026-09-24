"""
007 — Remove the saved PakistanLawSite credential columns from browser_session_slots.

Specification 0.5: credentials are never in the database. The human types the username and
password into the streamed browser; the service keeps only the encrypted storage state.
Migration 006 had added encrypted username/password columns for an unattended sign-in that the
specification does not allow; this drops them, and the ciphertext they held, for good.
"""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    for column in (
        "login_username_encrypted",
        "login_password_encrypted",
        "login_credentials_updated_at",
        "login_credentials_updated_by",
    ):
        await conn.execute(text(f"ALTER TABLE browser_session_slots DROP COLUMN IF EXISTS {column}"))
