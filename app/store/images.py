"""Pictures attached to a chat.

A picture is not a document. There is no text to read page by page, nothing to
cut into passages or embed, and nothing to cite: it is handed to the model as
it is, beside the question it went out with, and only a model that can see is
given it. So it is stored here, on its own, and never in `lit_files`, where
holding one would make the chat answer only from its files.

A picture is attached before its question is sent, like a file, under the chat
id that question will carry (`store.attachments`). When the question goes out
the run claims every picture not yet sent (`claim`), and that is the question
it belongs to from then on.

Ownership is checked on every read, by profile, as for files.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite

from app.store.db import now

# What the providers accept, by magic bytes. Anything else is refused before it
# is stored: a HEIC from a phone would be stored, shown as broken, and rejected
# by every model it was sent to.
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

# The browser scales larger pictures down before uploading (attachments.js), so
# this is only ever reached by a client that does not.
MAX_BYTES = 10 * 1024 * 1024
# Per chat. Every picture of a question is sent with it, and twenty is already
# more than a model looks at carefully.
MAX_IMAGES = 20
# Per question. Providers cap it too, some at five.
MAX_PER_QUESTION = 5
ORPHAN_AFTER = timedelta(days=1)

UNSUPPORTED = "Only PNG, JPEG, WebP and GIF images can be attached."


class ImageError(ValueError):
    """A picture that cannot be attached, with a sentence the person can act on."""


def mime_of(data: bytes) -> str | None:
    """The image type of these bytes, or None when they are not one we take."""
    for signature, mime in _SIGNATURES:
        if data.startswith(signature):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def looks_like_image(data: bytes) -> bool:
    return mime_of(data) is not None


def _public(row: dict[str, Any]) -> dict[str, Any]:
    """What the client is told about a picture: never the bytes."""
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": "image",
        "mime": row["mime"],
        "size": row["size"],
        "created_at": row["created_at"],
        "message_id": row["message_id"],
        "url": f"/api/chats/{row['chat_id']}/images/{row['id']}",
    }


async def add(
    conn: aiosqlite.Connection, chat_id: str, user: str, name: str, data: bytes
) -> tuple[dict[str, Any], bool]:
    """Store a picture under a chat. Returns it and whether it is new: the same
    picture twice is the one already there."""
    mime = mime_of(data)
    if mime is None:
        raise ImageError(UNSUPPORTED)
    if len(data) > MAX_BYTES:
        raise ImageError(f"The image is larger than {MAX_BYTES // (1024 * 1024)} MB.")
    sha = hashlib.sha256(data).hexdigest()
    async with conn.execute(
        "SELECT * FROM chat_images WHERE chat_id = ? AND user_id = ? AND sha256 = ?",
        (chat_id, user, sha),
    ) as cursor:
        row = await cursor.fetchone()
    if row is not None:
        # Attached again: it goes out with the next question once more.
        await conn.execute("UPDATE chat_images SET message_id = NULL WHERE id = ?", (row["id"],))
        await conn.commit()
        return _public({**dict(row), "message_id": None}), False
    async with conn.execute(
        "SELECT COUNT(*) FROM chat_images WHERE chat_id = ? AND user_id = ?", (chat_id, user)
    ) as cursor:
        (count,) = await cursor.fetchone()
    if count >= MAX_IMAGES:
        raise ImageError(f"A chat holds at most {MAX_IMAGES} images.")
    image_id = uuid.uuid4().hex
    await conn.execute(
        """
        INSERT INTO chat_images (id, chat_id, user_id, message_id, name, mime, sha256, size,
                                 data, created_at)
        VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
        """,
        (image_id, chat_id, user, (name or "image")[:200], mime, sha, len(data), data, now()),
    )
    await conn.commit()
    return await get(conn, chat_id, image_id, user), True


async def get(
    conn: aiosqlite.Connection, chat_id: str, image_id: str, user: str
) -> dict[str, Any] | None:
    async with conn.execute(
        "SELECT * FROM chat_images WHERE id = ? AND chat_id = ? AND user_id = ?",
        (image_id, chat_id, user),
    ) as cursor:
        row = await cursor.fetchone()
    return _public(dict(row)) if row else None


async def data_of(
    conn: aiosqlite.Connection, chat_id: str, image_id: str, user: str
) -> tuple[bytes, str] | None:
    """The bytes and type of one picture, for showing it."""
    async with conn.execute(
        "SELECT data, mime FROM chat_images WHERE id = ? AND chat_id = ? AND user_id = ?",
        (image_id, chat_id, user),
    ) as cursor:
        row = await cursor.fetchone()
    return (bytes(row["data"]), row["mime"]) if row else None


async def listed(conn: aiosqlite.Connection, chat_id: str, user: str) -> list[dict[str, Any]]:
    """A chat's pictures, oldest first, the order they were attached in."""
    async with conn.execute(
        "SELECT * FROM chat_images WHERE chat_id = ? AND user_id = ? ORDER BY created_at",
        (chat_id, user),
    ) as cursor:
        return [_public(dict(row)) for row in await cursor.fetchall()]


async def remove(conn: aiosqlite.Connection, chat_id: str, image_id: str, user: str) -> bool:
    cursor = await conn.execute(
        "DELETE FROM chat_images WHERE id = ? AND chat_id = ? AND user_id = ?",
        (image_id, chat_id, user),
    )
    await conn.commit()
    return cursor.rowcount > 0


async def taken(conn: aiosqlite.Connection, chat_id: str, user: str) -> bool:
    """Whether another profile already attached pictures under this chat id."""
    async with conn.execute(
        "SELECT 1 FROM chat_images WHERE chat_id = ? AND user_id != ? LIMIT 1", (chat_id, user)
    ) as cursor:
        return await cursor.fetchone() is not None


def as_part(data: bytes, mime: str) -> dict[str, Any]:
    """A picture as an OpenAI-format content part. OpenRouter, Groq, Ollama and
    LM Studio all take a data URL here."""
    encoded = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


async def claim(
    conn: aiosqlite.Connection, chat_id: str, user: str, message_id: str
) -> list[dict[str, Any]]:
    """Give every picture not yet sent to this question, and return the parts
    the model is shown. At most `MAX_PER_QUESTION`, the newest: the ones left
    over stay unsent rather than being dropped without a word."""
    async with conn.execute(
        """
        SELECT id, data, mime FROM chat_images
        WHERE chat_id = ? AND user_id = ? AND message_id IS NULL
        ORDER BY created_at DESC LIMIT ?
        """,
        (chat_id, user, MAX_PER_QUESTION),
    ) as cursor:
        rows = list(reversed(await cursor.fetchall()))
    if not rows:
        return []
    await conn.executemany(
        "UPDATE chat_images SET message_id = ? WHERE id = ?",
        [(message_id, row["id"]) for row in rows],
    )
    await conn.commit()
    return [as_part(bytes(row["data"]), row["mime"]) for row in rows]


async def latest_sent(
    conn: aiosqlite.Connection, chat_id: str, user: str, *, window: int = 6
) -> list[dict[str, Any]]:
    """The pictures of the most recent answered question that had any.

    A follow-up ("and the one on the left?") is about the picture the person
    sent a moment ago, and the model has no other way of seeing it again.
    Only the last `window` answered questions are looked at, the same window
    the history is (`chats.history`), so a picture from an hour of
    conversation ago is not sent with every question after it.
    """
    async with conn.execute(
        """
        SELECT i.message_id FROM chat_images i
        WHERE i.chat_id = ? AND i.user_id = ? AND i.message_id IN (
            SELECT message_id FROM messages
            WHERE chat_id = ? AND status = 'completed'
            ORDER BY id DESC LIMIT ?
        )
        ORDER BY i.created_at DESC LIMIT 1
        """,
        (chat_id, user, chat_id, window),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return []
    async with conn.execute(
        """
        SELECT data, mime FROM chat_images
        WHERE chat_id = ? AND user_id = ? AND message_id = ?
        ORDER BY created_at LIMIT ?
        """,
        (chat_id, user, row["message_id"], MAX_PER_QUESTION),
    ) as cursor:
        return [as_part(bytes(r["data"]), r["mime"]) for r in await cursor.fetchall()]


async def delete_for_chat(conn: aiosqlite.Connection, chat_id: str) -> None:
    await conn.execute("DELETE FROM chat_images WHERE chat_id = ?", (chat_id,))
    await conn.commit()


async def reap_orphans(conn: aiosqlite.Connection) -> int:
    """Clear pictures attached under a chat id whose question was never sent."""
    cutoff = (datetime.now(UTC) - ORPHAN_AFTER).isoformat()
    cursor = await conn.execute(
        """
        DELETE FROM chat_images
        WHERE created_at < ?
          AND NOT EXISTS (SELECT 1 FROM chats WHERE chats.id = chat_images.chat_id)
        """,
        (cutoff,),
    )
    await conn.commit()
    return cursor.rowcount
