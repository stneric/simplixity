"""Projects: a folder for chats, and the corpus made out of them.

Two jobs, and the second is the point. Grouping chats is filing. What makes a
project worth having is that the answers already written in it are searched
when you ask the next question there. So a week of research on one subject
accumulates instead of being re-derived, and the run can cite what it reused.

Deliberately not a second knowledge base: there is no ingest, no chunking table
and no embedding store. A project's corpus is the `messages` rows that are
already there, retrieved with the same relevance scoring the web results get
(`search/quality.py`). That keeps one ranking model in the codebase and means a
project is useful the moment a chat is moved into it, with nothing to rebuild.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from app.store.db import DEFAULT_USER

# How much of a past answer is carried as evidence. Long enough to hold the
# substance of a section, short enough that five of them do not crowd the web
# results out of the context window.
SNIPPET_CHARS = 1200

# Never offer more than this many past answers to one question, however large
# the project. They are ranked before the cut, so this is the tail being
# dropped, not a random sample.
MAX_CORPUS_RESULTS = 8


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _owned(sql: str, params: list[Any], user: str | None) -> tuple[str, list[Any]]:
    """Narrow a statement ending in `WHERE id = ?` to one profile's rows.

    `None` is every profile, for callers that already hold a chat's own project
    id (a run reading its corpus) rather than one a browser sent.
    """
    if user is None:
        return sql, params
    return sql + " AND user_id = ?", [*params, user]


async def create(
    conn: aiosqlite.Connection, name: str, *, user: str = DEFAULT_USER
) -> dict[str, Any]:
    project_id = uuid.uuid4().hex
    now = _now()
    await conn.execute(
        "INSERT INTO projects (id, name, created_at, updated_at, user_id) VALUES (?, ?, ?, ?, ?)",
        (project_id, name.strip() or "Untitled project", now, now, user),
    )
    await conn.commit()
    return {
        "id": project_id,
        "name": name.strip(),
        "created_at": now,
        "updated_at": now,
        "chats": 0,
    }


async def rename(
    conn: aiosqlite.Connection, project_id: str, name: str, *, user: str | None = None
) -> bool:
    cursor = await conn.execute(
        *_owned(
            "UPDATE projects SET name = ?, updated_at = ? WHERE id = ?",
            [name.strip() or "Untitled project", _now(), project_id],
            user,
        )
    )
    await conn.commit()
    return cursor.rowcount > 0


async def delete(conn: aiosqlite.Connection, project_id: str, *, user: str | None = None) -> bool:
    """Remove the project. Its chats survive, unfiled.

    A project is a view onto chats, not their owner. Deleting one because the
    grouping stopped being useful must not be a way to lose a month of work,
    and a confirmation dialog is not a safety property.
    """
    cursor = await conn.execute(*_owned("DELETE FROM projects WHERE id = ?", [project_id], user))
    if cursor.rowcount == 0:
        await conn.commit()
        return False
    await conn.execute("UPDATE chats SET project_id = NULL WHERE project_id = ?", (project_id,))
    # Reading lists filed here survive the same way, unfiled.
    await conn.execute(
        "UPDATE lit_collections SET project_id = NULL WHERE project_id = ?", (project_id,)
    )
    await conn.commit()
    return True


async def get(
    conn: aiosqlite.Connection, project_id: str, *, user: str | None = None
) -> dict[str, Any] | None:
    async with conn.execute(
        *_owned(
            "SELECT id, name, created_at, updated_at FROM projects WHERE id = ?",
            [project_id],
            user,
        )
    ) as cursor:
        row = await cursor.fetchone()
    return dict(row) if row else None


async def list_projects(
    conn: aiosqlite.Connection, *, user: str | None = None
) -> list[dict[str, Any]]:
    """Every project, most recently used first, with how many chats it holds.

    And what makes it a literature project: how many reading lists are filed
    under it, how many of their papers are marked relevant, and how many of
    those have a full text. The composer reads the first to decide which
    controls it shows, the badge above it the other two.
    """
    sql = """
        SELECT p.id, p.name, p.created_at, p.updated_at,
               (SELECT COUNT(*) FROM chats c WHERE c.project_id = p.id) AS chats,
               (SELECT COUNT(*) FROM lit_collections l WHERE l.project_id = p.id) AS lists,
               (SELECT COUNT(*) FROM lit_papers lp
                  JOIN lit_collections l ON l.id = lp.collection_id
                 WHERE l.project_id = p.id AND lp.decision = 'relevant') AS relevant,
               (SELECT COUNT(DISTINCT f.paper_id) FROM lit_files f
                  JOIN lit_collections l ON l.id = f.collection_id
                  JOIN lit_papers lp ON lp.id = f.paper_id
                 WHERE l.project_id = p.id AND lp.decision = 'relevant') AS fulltexts
        FROM projects p
    """
    params: list[Any] = []
    if user is not None:
        sql += " WHERE p.user_id = ?"
        params.append(user)
    sql += " ORDER BY p.updated_at DESC"
    async with conn.execute(sql, params) as cursor:
        return [dict(row) for row in await cursor.fetchall()]


async def assign(conn: aiosqlite.Connection, chat_id: str, project_id: str | None) -> bool:
    """File a chat under a project, or unfile it with None."""
    cursor = await conn.execute(
        "UPDATE chats SET project_id = ? WHERE id = ?", (project_id, chat_id)
    )
    if project_id:
        # Ordering the sidebar by "last used" is what makes the project you are
        # working in the one at the top.
        await conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (_now(), project_id))
    await conn.commit()
    return cursor.rowcount > 0


async def corpus(
    conn: aiosqlite.Connection, project_id: str, *, exclude_chat_id: str | None = None
) -> list[dict[str, Any]]:
    """Every finished answer in the project, as retrievable documents.

    Only completed messages: a failed run's empty answer is not evidence, and a
    half-written one is worse than nothing because it reads like a conclusion.

    The chat being asked in is excluded: its own history is already in the
    prompt as conversation, and offering it back as a numbered source would let
    an answer cite itself.
    """
    sql = """
        SELECT m.message_id, m.query, m.answer, m.created_at,
               c.id AS chat_id, c.title AS chat_title
        FROM messages m
        JOIN chats c ON c.id = m.chat_id
        WHERE c.project_id = ?
          AND m.status = 'completed'
          AND m.answer IS NOT NULL AND length(trim(m.answer)) > 0
    """
    params: list[Any] = [project_id]
    if exclude_chat_id:
        sql += " AND c.id != ?"
        params.append(exclude_chat_id)
    sql += " ORDER BY m.created_at DESC"

    async with conn.execute(sql, params) as cursor:
        return [dict(row) for row in await cursor.fetchall()]
