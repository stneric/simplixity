"""Files attached to a chat.

A chat's attachments are a collection in `lit_collections` with `chat_id` set:
no papers, no brief, no rounds, never listed on the literature page. That is
the whole trick, and the reason for it: an attached PDF then goes through the
code a reading list's full text goes through, stored once by hash, read page by
page, cut into passages that never cross a page, embedded, searched by
`search.library.Library`, cited by page and opened in the reader with the cited
words marked. A second store for "files in a chat" would be a second copy of
all of that, drifting from the first.

The collection is keyed by chat id, not by chat row. A file is attached before
the first question is sent, when the chat does not exist yet; the client picks
the chat's id and sends it with that question (`ChatRequest.chat_id`). An id
whose question was never sent leaves a collection behind, and `reap_orphans`
clears those at boot.

Ownership is checked here, on every read, by profile: a chat id is only a
random string, and knowing another profile's must not lend its files to a run.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite

from app.store import images, library

# How long a chat id may hold files before its first question. Generous: a
# person attaches, gets distracted, comes back after lunch.
ORPHAN_AFTER = timedelta(days=1)
# Per chat. A chat is a conversation about some documents, not a library; a
# person with forty PDFs wants a literature project, and the search that ranks
# their pages was tuned on a few dozen papers, not on a few hundred.
MAX_FILES = 20


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def collection_of(
    conn: aiosqlite.Connection, chat_id: str, user: str, *, create: bool = False
) -> dict[str, Any] | None:
    """The collection holding a chat's files, made on first use with `create`.

    None when there is none, or when it belongs to another profile.
    """
    async with conn.execute(
        "SELECT * FROM lit_collections WHERE chat_id = ? AND user_id = ?", (chat_id, user)
    ) as cursor:
        row = await cursor.fetchone()
    if row is not None or not create:
        return dict(row) if row else None
    now = _now()
    collection_id = uuid.uuid4().hex
    await conn.execute(
        """
        INSERT INTO lit_collections (id, user_id, project_id, chat_id, name, created_at, updated_at)
        VALUES (?, ?, NULL, ?, '', ?, ?)
        """,
        (collection_id, user, chat_id, now, now),
    )
    await conn.commit()
    return await collection_of(conn, chat_id, user)


async def taken(conn: aiosqlite.Connection, chat_id: str, user: str) -> bool:
    """Whether another profile already attached files or pictures under this
    chat id."""
    async with conn.execute(
        "SELECT 1 FROM lit_collections WHERE chat_id = ? AND user_id != ? LIMIT 1",
        (chat_id, user),
    ) as cursor:
        if await cursor.fetchone() is not None:
            return True
    return await images.taken(conn, chat_id, user)


async def files(
    conn: aiosqlite.Connection, chat_id: str, user: str, tag: str | None = None
) -> list[dict[str, Any]]:
    """A chat's files, oldest first, the order they were attached in."""
    collection = await collection_of(conn, chat_id, user)
    if collection is None:
        return []
    return list(reversed(await library.list_files(conn, collection["id"], tag)))


async def passages(conn: aiosqlite.Connection, chat_id: str, user: str) -> list[dict[str, Any]]:
    """Every passage of every file attached to the chat, shaped like
    `library.project_passages` rows, with the file standing in for a paper:
    its name is the title, and `attachment` says what it is."""
    async with conn.execute(
        """
        SELECT k.id, k.file_id, k.page, k.text, k.vector, k.model, f.name, f.kind, f.pages
        FROM lit_chunks k
        JOIN lit_files f ON f.id = k.file_id
        JOIN lit_collections c ON c.id = f.collection_id
        WHERE c.chat_id = ? AND c.user_id = ?
        ORDER BY f.created_at, k.id
        """,
        (chat_id, user),
    ) as cursor:
        rows = await cursor.fetchall()
    out = []
    for row in rows:
        data = dict(row)
        data["paper"] = as_paper(data["file_id"], data["name"], data["kind"], data["pages"])
        data["paper_key"] = data["paper"]["key"]
        out.append(data)
    return out


def as_paper(file_id: str, name: str, kind: str, pages: int) -> dict[str, Any]:
    """What a search over papers needs to know about an attached file."""
    return {
        "title": name,
        "key": f"file:{file_id}",
        "attachment": True,
        "kind": kind,
        "pages": pages,
        "file_id": file_id,
    }


async def has_files(conn: aiosqlite.Connection, chat_id: str, user: str) -> bool:
    async with conn.execute(
        """
        SELECT 1 FROM lit_files f JOIN lit_collections c ON c.id = f.collection_id
        WHERE c.chat_id = ? AND c.user_id = ? LIMIT 1
        """,
        (chat_id, user),
    ) as cursor:
        return await cursor.fetchone() is not None


async def delete_for_chat(conn: aiosqlite.Connection, chat_id: str) -> None:
    """Remove a chat's files with the chat. The files, pages and passages go by
    cascade; the bytes once nothing else holds the same PDF. Its pictures too."""
    await images.delete_for_chat(conn, chat_id)
    cursor = await conn.execute("DELETE FROM lit_collections WHERE chat_id = ?", (chat_id,))
    if cursor.rowcount:
        await library.delete_orphan_blobs(conn)
    await conn.commit()


async def reap_orphans(conn: aiosqlite.Connection) -> int:
    """Clear the files attached under a chat id whose question was never sent."""
    cutoff = (datetime.now(UTC) - ORPHAN_AFTER).isoformat()
    cursor = await conn.execute(
        """
        DELETE FROM lit_collections
        WHERE chat_id IS NOT NULL AND created_at < ?
          AND NOT EXISTS (SELECT 1 FROM chats WHERE chats.id = lit_collections.chat_id)
        """,
        (cutoff,),
    )
    reaped = cursor.rowcount
    if reaped:
        await library.delete_orphan_blobs(conn)
    await conn.commit()
    return reaped + await images.reap_orphans(conn)
