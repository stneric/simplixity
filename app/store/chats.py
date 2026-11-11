"""Chat and message history.

A message row is written when a run starts and completed when it ends, so a
crashed run leaves a visible "this one failed" entry rather than a hole. The
`blocks` column stores the final transcript exactly as the UI rendered it,
which is what makes reopening an old chat identical to watching it live.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import aiosqlite

from app.store import attachments
from app.store.db import DEFAULT_USER, now, owned

log = logging.getLogger(__name__)

TITLE_LIMIT = 70
# Answers can be long; history is for context, not for replaying the whole thing.
HISTORY_ANSWER_CHARS = 2000


def fallback_title(query: str) -> str:
    """A title available the instant a chat is created. The model-written one
    replaces it a few seconds later."""
    text = " ".join(query.split())
    return text[: TITLE_LIMIT - 1] + "…" if len(text) > TITLE_LIMIT else text or "New chat"


# ------------------------------------------------------------------- writing


async def ensure_chat(
    conn: aiosqlite.Connection,
    chat_id: str,
    query: str,
    project_id: str | None = None,
    *,
    user: str = DEFAULT_USER,
) -> bool:
    """Create the chat if it is new. Returns True when it was created, which is
    the signal to generate a real title later.

    `project_id` files a *new* chat under a project. An existing one keeps
    whatever it was filed under: a run started from inside a project view must
    not silently move a chat that lives somewhere else.
    """
    async with conn.execute("SELECT 1 FROM chats WHERE id = ?", (chat_id,)) as cursor:
        if await cursor.fetchone() is not None:
            await conn.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (now(), chat_id))
            await conn.commit()
            return False

    created = now()
    await conn.execute(
        """
        INSERT INTO chats (id, title, created_at, updated_at, project_id, user_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (chat_id, fallback_title(query), created, created, project_id, user),
    )
    await conn.commit()
    return True


async def owner(conn: aiosqlite.Connection, chat_id: str) -> str | None:
    """The profile a chat belongs to, or None if it does not exist yet. A new
    chat's id is chosen by the browser, so "not mine" and "not created yet" have
    to be told apart before a run appends to it."""
    async with conn.execute("SELECT user_id FROM chats WHERE id = ?", (chat_id,)) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


async def project_of(conn: aiosqlite.Connection, chat_id: str) -> str | None:
    """The project a chat is filed under, or None. Its own query: `get_chat`
    decodes every block of the whole transcript, and starting a run only ever
    needed this one column."""
    async with conn.execute("SELECT project_id FROM chats WHERE id = ?", (chat_id,)) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


async def running_runs(conn: aiosqlite.Connection, chat_id: str) -> list[str]:
    """The runs still answering in a chat, the ones deleting it has to stop."""
    async with conn.execute(
        "SELECT run_id FROM messages WHERE chat_id = ? AND status = 'running'", (chat_id,)
    ) as cursor:
        return [row[0] for row in await cursor.fetchall()]


async def run_owner(conn: aiosqlite.Connection, run_id: str) -> str | None:
    """The profile whose chat a run answers, or None for a run nobody owns.

    A run lives in Redis and knows nothing about profiles; its message row is
    the link, written before the run is spawned, so a run id a client has been
    handed always has an owner by the time it can be asked about.
    """
    async with conn.execute(
        "SELECT chats.user_id FROM messages JOIN chats ON chats.id = messages.chat_id "
        "WHERE messages.run_id = ?",
        (run_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


async def start_message(
    conn: aiosqlite.Connection,
    *,
    chat_id: str,
    run_id: str,
    query: str,
    depth: str,
    model: str,
) -> str:
    message_id = uuid.uuid4().hex
    await conn.execute(
        """
        INSERT INTO messages
            (chat_id, message_id, run_id, query, blocks, status, depth, model, created_at)
        VALUES (?, ?, ?, ?, '[]', 'running', ?, ?, ?)
        """,
        (chat_id, message_id, run_id, query, depth, model, now()),
    )
    await conn.commit()
    return message_id


async def finish_message(
    conn: aiosqlite.Connection,
    run_id: str,
    *,
    blocks: list[dict[str, Any]],
    answer: str,
    status: str,
) -> None:
    finished = now()
    await conn.execute(
        "UPDATE messages SET blocks = ?, answer = ?, status = ?, finished_at = ? WHERE run_id = ?",
        (json.dumps(blocks, separators=(",", ":")), answer, status, finished, run_id),
    )
    await conn.execute(
        """
        UPDATE chats SET updated_at = ?
        WHERE id = (SELECT chat_id FROM messages WHERE run_id = ?)
        """,
        (finished, run_id),
    )
    await conn.commit()


async def fail_stale_messages(conn: aiosqlite.Connection) -> int:
    """Close out turns a previous process left mid-run.

    A hard kill skips the finalizer, so the row keeps `status='running'` and the
    UI rejoins a run that no longer exists. Redis reaping covers that for the
    first 24 hours; once the stream has expired this is the only record left, so
    it has to be corrected here too. Runs are in-process: at startup, none of
    them are ours.
    """
    async with conn.execute(
        "UPDATE messages SET status = 'error' WHERE status = 'running'"
    ) as cursor:
        stale = cursor.rowcount
    await conn.commit()
    if stale:
        log.warning("marked %d unfinished message(s) from a previous process as failed", stale)
    return stale


async def set_title(conn: aiosqlite.Connection, chat_id: str, title: str) -> str:
    """Store a title. Returns it as stored, or "" when nothing was left of it."""
    cleaned = " ".join(title.split()).strip("\"'“”").strip()[:TITLE_LIMIT]
    if not cleaned:
        return ""
    await conn.execute(
        "UPDATE chats SET title = ?, updated_at = ? WHERE id = ?",
        (cleaned, now(), chat_id),
    )
    await conn.commit()
    return cleaned


async def delete_chat(conn: aiosqlite.Connection, chat_id: str, *, user: str | None = None) -> bool:
    async with conn.execute(*owned("DELETE FROM chats WHERE id = ?", [chat_id], user)) as cursor:
        deleted = cursor.rowcount > 0
    if not deleted:
        # Someone else's chat, or none at all: its messages are not ours to touch.
        return False
    # ON DELETE CASCADE needs the pragma, which `db.connect` sets; belt and
    # braces because an orphaned message row would show up in search results.
    await conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
    await conn.commit()
    # The files attached to it go with it: they were attached to ask this chat
    # about them, and nothing else can reach them once it is gone.
    await attachments.delete_for_chat(conn, chat_id)
    return deleted


# ------------------------------------------------------------------- reading


async def list_chats(
    conn: aiosqlite.Connection,
    *,
    limit: int = 200,
    search: str | None = None,
    project_id: str | None = None,
    user: str | None = None,
) -> list[dict[str, Any]]:
    """`user=None` lists every profile's chats; the API always passes one."""
    sql = """
        SELECT c.id, c.title, c.created_at, c.updated_at, c.project_id,
               (SELECT COUNT(*) FROM messages m WHERE m.chat_id = c.id) AS messages
        FROM chats c
    """
    params: list[Any] = []
    clauses: list[str] = []
    if user is not None:
        clauses.append("c.user_id = ?")
        params.append(user)
    if project_id:
        clauses.append("c.project_id = ?")
        params.append(project_id)
    if search:
        # Title first, then message text: searching only titles misses the
        # chat you remember by what you asked, not by what it was called.
        clauses.append(
            r"""(c.title LIKE ? ESCAPE '\'
                OR EXISTS (SELECT 1 FROM messages m
                           WHERE m.chat_id = c.id
                             AND (m.query LIKE ? ESCAPE '\' OR m.answer LIKE ? ESCAPE '\')))"""
        )
        # Escaped, so "50%" or "snake_case" mean what was typed rather than
        # matching any character.
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        params += [pattern, pattern, pattern]
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY c.updated_at DESC LIMIT ?"
    params.append(limit)

    async with conn.execute(sql, params) as cursor:
        return [dict(row) for row in await cursor.fetchall()]


async def get_chat(
    conn: aiosqlite.Connection, chat_id: str, *, user: str | None = None
) -> dict[str, Any] | None:
    """The chat and its transcript. With `user`, another profile's chat reads as
    missing, which is what makes a 404 the only answer the API can give."""
    async with conn.execute(
        *owned(
            "SELECT id, title, created_at, updated_at, project_id FROM chats WHERE id = ?",
            [chat_id],
            user,
        )
    ) as cursor:
        chat = await cursor.fetchone()
    if chat is None:
        return None

    async with conn.execute(
        """
        SELECT message_id, run_id, query, blocks, status, depth, model, created_at,
               finished_at
        FROM messages WHERE chat_id = ? ORDER BY id
        """,
        (chat_id,),
    ) as cursor:
        rows = await cursor.fetchall()

    return {
        **dict(chat),
        "messages": [
            {
                "message_id": row["message_id"],
                "run_id": row["run_id"],
                "query": row["query"],
                "blocks": _loads(row["blocks"]),
                "status": row["status"],
                "depth": row["depth"],
                "model": row["model"],
                "created_at": row["created_at"],
                "finished_at": row["finished_at"],
            }
            for row in rows
        ],
    }


async def history(
    conn: aiosqlite.Connection, chat_id: str, *, limit: int = 6
) -> list[dict[str, str]]:
    """Prior turns as chat messages, oldest first: what the LLM sees.

    Only completed turns: feeding a failed run's empty answer back in teaches
    the model that empty answers are acceptable.
    """
    async with conn.execute(
        """
        SELECT query, answer FROM messages
        WHERE chat_id = ? AND status = 'completed' AND answer IS NOT NULL AND answer != ''
        ORDER BY id DESC LIMIT ?
        """,
        (chat_id, limit),
    ) as cursor:
        rows = await cursor.fetchall()

    messages: list[dict[str, str]] = []
    for row in reversed(rows):
        messages.append({"role": "user", "content": row["query"] or ""})
        messages.append(
            {"role": "assistant", "content": (row["answer"] or "")[:HISTORY_ANSWER_CHARS]}
        )
    return messages


def _loads(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("unreadable blocks JSON in history; showing the message as empty")
        return []
