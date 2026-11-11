"""Reading lists: collections of papers and the decisions made about them.

A collection is the unit of work on the literature page. It holds the intake
conversation, the brief that conversation produced, a log of every search round,
and every paper any round has shown, with what the person decided about it.

Nothing is ever forgotten by deciding. An excluded paper keeps its row, because
the row is what stops the next round from offering it again and what "unlike
these" is computed from; deleting it would make the list worse at the one thing
excluding was for.

Ownership follows the projects module: every function takes `user`, `None` is
unscoped and for internal callers only, and another profile's collection reads
as missing.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import aiosqlite

from app.store.db import DEFAULT_USER, now, owned

RELEVANT = "relevant"
MAYBE = "maybe"
EXCLUDED = "excluded"
# "" is undecided: shown, and not yet screened.
DECISIONS = ("", RELEVANT, MAYBE, EXCLUDED)

DEFAULT_NAME = "Untitled reading list"

# A conversation that runs long is still one collection's setup, but every turn
# is sent back to the model with the next one. Older turns fall off the front.
MAX_TALK_TURNS = 40


def _collection(row: aiosqlite.Row) -> dict[str, Any]:
    data = dict(row)
    for name, empty in (("brief", {}), ("talk", []), ("rounds", [])):
        try:
            data[name] = json.loads(data.get(name) or "null") or empty
        except ValueError:
            # A damaged blob must cost that field, not the collection.
            data[name] = empty
    return data


async def create(
    conn: aiosqlite.Connection,
    name: str,
    *,
    project_id: str | None = None,
    user: str = DEFAULT_USER,
) -> dict[str, Any]:
    collection_id = uuid.uuid4().hex
    created = now()
    await conn.execute(
        """
        INSERT INTO lit_collections (id, user_id, project_id, name, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (collection_id, user, project_id, name.strip() or DEFAULT_NAME, created, created),
    )
    await conn.commit()
    return await get(conn, collection_id, user=user)  # type: ignore[return-value]


async def get(
    conn: aiosqlite.Connection, collection_id: str, *, user: str | None = None
) -> dict[str, Any] | None:
    # Never a chat's attachments: they share the table and none of the rest,
    # and every literature route reaches a collection through here.
    async with conn.execute(
        *owned(
            "SELECT * FROM lit_collections WHERE id = ? AND chat_id IS NULL", [collection_id], user
        )
    ) as cursor:
        row = await cursor.fetchone()
    return _collection(row) if row else None


async def list_collections(
    conn: aiosqlite.Connection, *, user: str | None = None, project_id: str | None = None
) -> list[dict[str, Any]]:
    """Every collection, most recently used first, with its tallies."""
    sql = """
        SELECT c.id, c.name, c.project_id, c.created_at, c.updated_at,
               (SELECT COUNT(*) FROM lit_papers p WHERE p.collection_id = c.id) AS total,
               (SELECT COUNT(*) FROM lit_papers p
                 WHERE p.collection_id = c.id AND p.decision = 'relevant') AS relevant,
               (SELECT COUNT(*) FROM lit_papers p
                 WHERE p.collection_id = c.id AND p.decision = '') AS open
        FROM lit_collections c WHERE c.chat_id IS NULL
    """
    params: list[Any] = []
    if user is not None:
        sql += " AND c.user_id = ?"
        params.append(user)
    if project_id is not None:
        sql += " AND c.project_id = ?"
        params.append(project_id)
    sql += " ORDER BY c.updated_at DESC"
    async with conn.execute(sql, params) as cursor:
        return [dict(row) for row in await cursor.fetchall()]


async def update(
    conn: aiosqlite.Connection,
    collection_id: str,
    *,
    user: str | None = None,
    **fields: Any,
) -> bool:
    """Set any of name, project_id, brief, talk, rounds. JSON fields are
    serialised here so callers hand over plain values."""
    allowed = {"name", "project_id", "brief", "talk", "rounds"}
    sets, params = [], []
    for name, value in fields.items():
        if name not in allowed:
            raise ValueError(f"cannot update {name}")
        if name == "talk":
            value = value[-MAX_TALK_TURNS:]
        if name in ("brief", "talk", "rounds"):
            value = json.dumps(value, ensure_ascii=False)
        sets.append(f"{name} = ?")
        params.append(value)
    sets.append("updated_at = ?")
    params.append(now())
    cursor = await conn.execute(
        *owned(
            f"UPDATE lit_collections SET {', '.join(sets)} WHERE id = ?",
            [*params, collection_id],
            user,
        )
    )
    await conn.commit()
    return cursor.rowcount > 0


async def delete(
    conn: aiosqlite.Connection, collection_id: str, *, user: str | None = None
) -> bool:
    cursor = await conn.execute(
        *owned("DELETE FROM lit_collections WHERE id = ?", [collection_id], user)
    )
    await conn.commit()
    return cursor.rowcount > 0


# -------------------------------------------------------------------- papers


def _paper(row: aiosqlite.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["data"] = json.loads(data["data"])
    except ValueError:
        data["data"] = {"title": "(unreadable record)"}
    try:
        data["summary"] = json.loads(data["summary"]) if data.get("summary") else None
    except ValueError:
        data["summary"] = None
    data["has_bibtex"] = bool(data.pop("bibtex", None))
    return data


async def add_papers(
    conn: aiosqlite.Connection,
    collection_id: str,
    papers: list[dict[str, Any]],
    *,
    round_no: int,
) -> int:
    """Insert the papers a round found. Each dict carries `key`, `data`,
    `origin` and `score`. A key already in the collection is skipped, the
    caller has filtered them, and the unique index is the guarantee."""
    added = now()
    cursor = await conn.executemany(
        """
        INSERT OR IGNORE INTO lit_papers
            (collection_id, key, data, round, origin, score, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                collection_id,
                p["key"],
                json.dumps(p["data"], ensure_ascii=False),
                round_no,
                p.get("origin", "search"),
                float(p.get("score", 0.0)),
                added,
            )
            for p in papers
        ],
    )
    await conn.execute(
        "UPDATE lit_collections SET updated_at = ? WHERE id = ?", (added, collection_id)
    )
    await conn.commit()
    return cursor.rowcount


async def list_papers(
    conn: aiosqlite.Connection, collection_id: str, *, decisions: tuple[str, ...] | None = None
) -> list[dict[str, Any]]:
    """Papers in the order they were found: by round, then by the rank the
    round gave them. Callers have already checked ownership of the collection."""
    sql = "SELECT * FROM lit_papers WHERE collection_id = ?"
    params: list[Any] = [collection_id]
    if decisions is not None:
        sql += f" AND decision IN ({', '.join('?' for _ in decisions)})"
        params += list(decisions)
    sql += " ORDER BY round ASC, score DESC, id ASC"
    async with conn.execute(sql, params) as cursor:
        return [_paper(row) for row in await cursor.fetchall()]


async def get_paper(
    conn: aiosqlite.Connection, collection_id: str, paper_id: int
) -> dict[str, Any] | None:
    async with conn.execute(
        "SELECT * FROM lit_papers WHERE collection_id = ? AND id = ?", (collection_id, paper_id)
    ) as cursor:
        row = await cursor.fetchone()
    return _paper(row) if row else None


async def paper_by_key(
    conn: aiosqlite.Connection, collection_id: str, key: str
) -> dict[str, Any] | None:
    async with conn.execute(
        "SELECT * FROM lit_papers WHERE collection_id = ? AND key = ?", (collection_id, key)
    ) as cursor:
        row = await cursor.fetchone()
    return _paper(row) if row else None


async def keys(conn: aiosqlite.Connection, collection_id: str) -> set[str]:
    async with conn.execute(
        "SELECT key FROM lit_papers WHERE collection_id = ?", (collection_id,)
    ) as cursor:
        return {row["key"] for row in await cursor.fetchall()}


async def decide(
    conn: aiosqlite.Connection,
    collection_id: str,
    paper_id: int,
    *,
    decision: str | None = None,
    note: str | None = None,
) -> bool:
    if decision is not None and decision not in DECISIONS:
        raise ValueError(f"unknown decision {decision!r}")
    sets, params = [], []
    if decision is not None:
        sets += ["decision = ?", "decided_at = ?"]
        params += [decision, now() if decision else None]
    if note is not None:
        sets.append("note = ?")
        params.append(note)
    if not sets:
        return True
    cursor = await conn.execute(
        f"UPDATE lit_papers SET {', '.join(sets)} WHERE collection_id = ? AND id = ?",
        [*params, collection_id, paper_id],
    )
    await conn.execute(
        "UPDATE lit_collections SET updated_at = ? WHERE id = ?", (now(), collection_id)
    )
    await conn.commit()
    return cursor.rowcount > 0


async def set_summary(
    conn: aiosqlite.Connection, collection_id: str, paper_id: int, summary: dict[str, Any]
) -> None:
    await conn.execute(
        "UPDATE lit_papers SET summary = ? WHERE collection_id = ? AND id = ?",
        (json.dumps(summary, ensure_ascii=False), collection_id, paper_id),
    )
    await conn.commit()


async def bibtex_of(conn: aiosqlite.Connection, collection_id: str) -> dict[int, str]:
    async with conn.execute(
        "SELECT id, bibtex FROM lit_papers WHERE collection_id = ? AND bibtex IS NOT NULL",
        (collection_id,),
    ) as cursor:
        return {row["id"]: row["bibtex"] for row in await cursor.fetchall()}


async def set_bibtex(
    conn: aiosqlite.Connection, collection_id: str, paper_id: int, entry: str
) -> None:
    """Cache a fetched registry entry, so a second export does not ask
    doi.org again. Only fetched entries are cached; built ones are cheap and
    should follow the record if the record is ever corrected."""
    await conn.execute(
        "UPDATE lit_papers SET bibtex = ? WHERE collection_id = ? AND id = ?",
        (entry, collection_id, paper_id),
    )
    await conn.commit()


async def relevant_for_project(conn: aiosqlite.Connection, project_id: str) -> list[dict[str, Any]]:
    """Papers marked relevant in any collection filed under a project, for
    the project corpus (`search/project.py`)."""
    async with conn.execute(
        """
        SELECT p.* FROM lit_papers p
        JOIN lit_collections c ON c.id = p.collection_id
        WHERE c.project_id = ? AND p.decision = 'relevant'
        ORDER BY p.decided_at DESC
        """,
        (project_id,),
    ) as cursor:
        return [_paper(row) for row in await cursor.fetchall()]
