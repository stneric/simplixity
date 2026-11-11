"""Profiles: who is using a shared install, and optionally a password for each.

Enough to keep housemates out of each other's history on a local install, and
no more. Hashing is stdlib scrypt and a session is an HMAC over the profile's
name, both because the runtime dependency bar is pure Python and neither needs
a library to be done properly. Left out by design: rate limiting on
login, password rules, recovery. A forgotten password is a row to clear by hand
(`UPDATE profiles SET password_hash = NULL WHERE id = '…'`), which is the right
cost for an app whose operator has the database file anyway.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

import aiosqlite

from app.store.db import now

# Interactive-login parameters: about 50ms and 16MB per attempt, cheap enough
# not to be noticed, expensive enough that a copied database file is not a list
# of passwords by the end of the afternoon.
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}
_SECRET_KEY = "session_secret"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str | None) -> bool:
    """A profile without a password accepts any, which is what "no password" means."""
    if not stored:
        return True
    try:
        _, salt, digest = stored.split("$")
        attempt = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **_SCRYPT)
    except ValueError:
        return False
    return hmac.compare_digest(attempt.hex(), digest)


# ------------------------------------------------------------------- rows


async def get(conn: aiosqlite.Connection, name: str) -> dict[str, Any] | None:
    async with conn.execute(
        "SELECT id, password_hash, created_at FROM profiles WHERE id = ?", (name,)
    ) as cursor:
        row = await cursor.fetchone()
    return dict(row) if row else None


async def list_profiles(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    """For the picker: names and whether each asks for a password, never the hash."""
    async with conn.execute(
        "SELECT id, password_hash IS NOT NULL AS protected FROM profiles ORDER BY id"
    ) as cursor:
        return [
            {"name": row["id"], "protected": bool(row["protected"])}
            for row in await cursor.fetchall()
        ]


async def create(conn: aiosqlite.Connection, name: str, password: str = "") -> bool:
    """False if the name is taken; creating must never become a way to take one over."""
    cursor = await conn.execute(
        "INSERT OR IGNORE INTO profiles (id, password_hash, created_at) VALUES (?, ?, ?)",
        (name, hash_password(password) if password else None, now()),
    )
    await conn.commit()
    return cursor.rowcount > 0


async def set_password(conn: aiosqlite.Connection, name: str, password: str) -> None:
    """Empty removes the password. Either way every existing session of the
    profile stops verifying, since the hash is part of what a session signs."""
    await conn.execute(
        "UPDATE profiles SET password_hash = ? WHERE id = ?",
        (hash_password(password) if password else None, name),
    )
    await conn.commit()


# --------------------------------------------------------------- sessions


async def _secret(conn: aiosqlite.Connection) -> bytes:
    """Made on first use and kept in the database, so sessions survive a restart
    and an install needs no extra environment variable to have them."""
    async with conn.execute("SELECT value FROM settings WHERE key = ?", (_SECRET_KEY,)) as cursor:
        row = await cursor.fetchone()
    if row:
        return bytes.fromhex(row["value"])
    await conn.execute(
        "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        (_SECRET_KEY, secrets.token_hex(32), now()),
    )
    await conn.commit()
    return await _secret(conn)


def _sign(secret: bytes, profile: dict[str, Any]) -> str:
    message = f"{profile['id']}\n{profile['password_hash'] or ''}".encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


async def session_token(conn: aiosqlite.Connection, profile: dict[str, Any]) -> str:
    return f"{profile['id']}.{_sign(await _secret(conn), profile)}"


async def verify_session(conn: aiosqlite.Connection, token: str) -> str | None:
    """The profile a session cookie names, or None if it is forged, stale (the
    password changed since) or for a profile that no longer exists."""
    name, _, signature = token.rpartition(".")
    if not name or not signature:
        return None
    profile = await get(conn, name)
    if profile is None:
        return None
    expected = _sign(await _secret(conn), profile)
    # As bytes: on text, `compare_digest` raises for anything outside ASCII, and
    # a cookie is whatever the client chose to send.
    return name if hmac.compare_digest(signature.encode(), expected.encode()) else None
