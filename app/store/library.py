"""The full texts behind reading lists: uploaded PDFs, their pages and passages.

A file belongs to a collection, and optionally to one paper in it. Assigning is
separate from uploading because matching can be undecided (see
`scholar.fulltext.match`), and an unassigned file is a normal state the
literature page shows, not an error.

One paper has at most one full text. Assigning a second PDF to a paper releases
the first one back to the unassigned list rather than deleting it, nothing the
person uploaded disappears without them deleting it.

Ownership is the collection's: callers check it before calling in here, exactly
as for papers.
"""

from __future__ import annotations

import json
import uuid
from array import array
from typing import Any

import aiosqlite

from app.store.db import now


def _file(row: aiosqlite.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["warnings"] = json.loads(data.get("warnings") or "[]")
    except ValueError:
        data["warnings"] = []
    return data


def pack(vector: list[float]) -> bytes:
    """Float32, which halves the size of a float64 and loses nothing a cosine
    similarity can tell apart."""
    return array("f", vector).tobytes()


def unpack(blob: bytes) -> array:
    values = array("f")
    values.frombytes(blob)
    return values


async def add_file(
    conn: aiosqlite.Connection,
    collection_id: str,
    *,
    name: str,
    sha256: str,
    data: bytes,
    pages: list[str],
    text_pages: int,
    warnings: list[str],
    chunks: list[tuple[int, str]],
    kind: str = "pdf",
) -> tuple[dict[str, Any], bool]:
    """Store a PDF with its pages and passages, unassigned. Returns the file and
    whether it is new; the same PDF uploaded to the same list again is the
    file already there."""
    async with conn.execute(
        "SELECT * FROM lit_files WHERE collection_id = ? AND sha256 = ?", (collection_id, sha256)
    ) as cursor:
        existing = await cursor.fetchone()
    if existing:
        return _file(existing), False

    file_id = uuid.uuid4().hex
    await conn.execute(
        "INSERT OR IGNORE INTO lit_blobs (sha256, data) VALUES (?, ?)", (sha256, data)
    )
    await conn.execute(
        """
        INSERT INTO lit_files
            (id, collection_id, name, sha256, size, pages, text_pages, warnings, kind,
             created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            file_id,
            collection_id,
            name,
            sha256,
            len(data),
            len(pages),
            text_pages,
            json.dumps(warnings),
            kind,
            now(),
        ),
    )
    await conn.executemany(
        "INSERT INTO lit_pages (file_id, page, text) VALUES (?, ?, ?)",
        [(file_id, number, text) for number, text in enumerate(pages, start=1)],
    )
    await conn.executemany(
        "INSERT INTO lit_chunks (file_id, page, text) VALUES (?, ?, ?)",
        [(file_id, page, text) for page, text in chunks],
    )
    await conn.commit()
    return (await get_file(conn, collection_id, file_id)), True  # type: ignore[return-value]


async def get_file(
    conn: aiosqlite.Connection, collection_id: str, file_id: str
) -> dict[str, Any] | None:
    async with conn.execute(
        "SELECT * FROM lit_files WHERE collection_id = ? AND id = ?", (collection_id, file_id)
    ) as cursor:
        row = await cursor.fetchone()
    return _file(row) if row else None


async def owner_of(conn: aiosqlite.Connection, file_id: str) -> tuple[str, str] | None:
    """(collection id, profile) a file belongs to, for the routes that are
    addressed by file alone: the PDF itself and a cited page."""
    async with conn.execute(
        """
        SELECT f.collection_id, c.user_id FROM lit_files f
        JOIN lit_collections c ON c.id = f.collection_id WHERE f.id = ?
        """,
        (file_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return (row["collection_id"], row["user_id"]) if row else None


async def list_files(
    conn: aiosqlite.Connection, collection_id: str, tag: str | None = None
) -> list[dict[str, Any]]:
    """Every file of a collection with how far it was indexed: passages in
    total, and passages carrying a vector from which model.

    With `tag` (`embedder.vector_tag`), only vectors made that way count as
    indexed: a vector from another model, or from this one fed differently,
    is not comparable with the queries a search will embed, and a file
    showing "indexed" over such vectors was being searched by keyword alone.
    """
    current = "AND k.model = ?" if tag else ""
    params: tuple[Any, ...] = (tag, collection_id) if tag else (collection_id,)
    async with conn.execute(
        f"""
        SELECT f.*,
               (SELECT COUNT(*) FROM lit_chunks k WHERE k.file_id = f.id) AS chunks,
               (SELECT COUNT(*) FROM lit_chunks k
                 WHERE k.file_id = f.id AND k.vector IS NOT NULL {current}) AS embedded,
               (SELECT MAX(k.model) FROM lit_chunks k WHERE k.file_id = f.id) AS model
        FROM lit_files f WHERE f.collection_id = ?
        ORDER BY f.created_at DESC
        """,
        params,
    ) as cursor:
        return [_file(row) for row in await cursor.fetchall()]


async def assign(
    conn: aiosqlite.Connection,
    collection_id: str,
    file_id: str,
    paper_id: int | None,
    *,
    how: str,
    warnings: list[str] | None = None,
) -> bool:
    """File a PDF under a paper, or release it with None. A paper's previous
    full text is released, not deleted."""
    if paper_id is not None:
        await conn.execute(
            "UPDATE lit_files SET paper_id = NULL, match = '' "
            "WHERE collection_id = ? AND paper_id = ? AND id != ?",
            (collection_id, paper_id, file_id),
        )
        # Each passage is embedded under its paper's title (`passage_text`), so
        # a file filed under another paper needs its vectors made again.
        await conn.execute(
            "UPDATE lit_chunks SET vector = NULL, model = NULL WHERE file_id = ? "
            "AND EXISTS (SELECT 1 FROM lit_files f WHERE f.id = ? AND f.collection_id = ? "
            "AND f.paper_id IS NOT ?)",
            (file_id, file_id, collection_id, paper_id),
        )
    sets = ["paper_id = ?", "match = ?"]
    params: list[Any] = [paper_id, how if paper_id is not None else ""]
    if warnings is not None:
        sets.append("warnings = ?")
        params.append(json.dumps(warnings))
    cursor = await conn.execute(
        f"UPDATE lit_files SET {', '.join(sets)} WHERE collection_id = ? AND id = ?",
        [*params, collection_id, file_id],
    )
    await conn.commit()
    return cursor.rowcount > 0


async def delete_file(conn: aiosqlite.Connection, collection_id: str, file_id: str) -> bool:
    """Delete a file, and its bytes once no list holds that PDF any more."""
    file = await get_file(conn, collection_id, file_id)
    if file is None:
        return False
    await conn.execute("DELETE FROM lit_files WHERE id = ?", (file_id,))
    await conn.execute(
        "DELETE FROM lit_blobs WHERE sha256 = ? "
        "AND NOT EXISTS (SELECT 1 FROM lit_files WHERE sha256 = ?)",
        (file["sha256"], file["sha256"]),
    )
    await conn.commit()
    return True


async def pdf_of(conn: aiosqlite.Connection, file_id: str) -> tuple[str, bytes] | None:
    found = await original_of(conn, file_id)
    return (found[0], found[2]) if found else None


async def original_of(conn: aiosqlite.Connection, file_id: str) -> tuple[str, str, bytes] | None:
    """(name, kind, bytes) of a file as it was uploaded: a PDF, or the Word,
    Markdown or text file a chat attachment was read from."""
    async with conn.execute(
        """
        SELECT f.name, f.kind, b.data FROM lit_files f JOIN lit_blobs b ON b.sha256 = f.sha256
        WHERE f.id = ?
        """,
        (file_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return (row["name"], row["kind"], bytes(row["data"])) if row else None


async def delete_orphan_blobs(conn: aiosqlite.Connection) -> None:
    """Drop the bytes no file refers to any more, after whole collections went
    at once (a chat deleted with its attachments) and `delete_file`'s own
    clean-up never ran for each."""
    await conn.execute(
        "DELETE FROM lit_blobs WHERE NOT EXISTS "
        "(SELECT 1 FROM lit_files f WHERE f.sha256 = lit_blobs.sha256)"
    )
    await conn.commit()


async def pages_of(conn: aiosqlite.Connection, file_id: str) -> list[str]:
    async with conn.execute(
        "SELECT text FROM lit_pages WHERE file_id = ? ORDER BY page", (file_id,)
    ) as cursor:
        return [row["text"] for row in await cursor.fetchall()]


async def unembedded(
    conn: aiosqlite.Connection, collection_id: str, tag: str, *, limit: int = 256
) -> list[tuple[int, str, str]]:
    """Passages with no vector made as `tag` says (`embedder.vector_tag`),
    which includes every passage a different model, or an older way of
    feeding this one, embedded: those are re-embedded rather than compared.
    Each with the title of its paper, which goes in front of it when embedded,
    or, for a file attached to a chat, which has no paper, its name: that is
    the title `search.library` gives it, and both sides must agree."""
    async with conn.execute(
        """
        SELECT k.id, k.text, p.data AS paper, f.name, c.chat_id FROM lit_chunks k
        JOIN lit_files f ON f.id = k.file_id
        JOIN lit_collections c ON c.id = f.collection_id
        LEFT JOIN lit_papers p ON p.id = f.paper_id
        WHERE f.collection_id = ? AND (k.vector IS NULL OR k.model IS NOT ?)
        ORDER BY k.id LIMIT ?
        """,
        (collection_id, tag, limit),
    ) as cursor:
        return [
            (row["id"], row["text"], row["name"] if row["chat_id"] else _title(row["paper"]))
            for row in await cursor.fetchall()
        ]


def _title(raw: str | None) -> str:
    try:
        return str((json.loads(raw) if raw else {}).get("title") or "")
    except (ValueError, AttributeError):
        return ""


async def set_vectors(
    conn: aiosqlite.Connection, vectors: list[tuple[int, list[float]]], model: str
) -> None:
    await conn.executemany(
        "UPDATE lit_chunks SET vector = ?, model = ? WHERE id = ?",
        [(pack(vector), model, chunk_id) for chunk_id, vector in vectors],
    )
    await conn.commit()


# ----------------------------------------------------------- the project's corpus


async def project_has_library(conn: aiosqlite.Connection, project_id: str) -> bool:
    """Whether a project is a literature project: any reading list filed
    under it. That, and nothing else, is what makes its chats source-bound."""
    async with conn.execute(
        "SELECT 1 FROM lit_collections WHERE project_id = ? LIMIT 1", (project_id,)
    ) as cursor:
        return await cursor.fetchone() is not None


async def project_passages(conn: aiosqlite.Connection, project_id: str) -> list[dict[str, Any]]:
    """Every passage of every full text filed under a relevant paper in the
    project's reading lists, with the paper it belongs to.

    Relevant only: a paper the person marked maybe or excluded is not one they
    have vouched for, whatever PDF they happened to attach to it.
    """
    async with conn.execute(
        """
        SELECT k.id, k.file_id, k.page, k.text, k.vector, k.model,
               p.id AS paper_id, p.key AS paper_key, p.data AS paper
        FROM lit_chunks k
        JOIN lit_files f ON f.id = k.file_id
        JOIN lit_papers p ON p.id = f.paper_id
        JOIN lit_collections c ON c.id = f.collection_id
        WHERE c.project_id = ? AND p.decision = 'relevant'
        ORDER BY k.id
        """,
        (project_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    out = []
    for row in rows:
        data = dict(row)
        try:
            data["paper"] = json.loads(data["paper"])
        except ValueError:
            data["paper"] = {}
        out.append(data)
    return out


async def page_text(conn: aiosqlite.Connection, file_id: str, page: int) -> str:
    async with conn.execute(
        "SELECT text FROM lit_pages WHERE file_id = ? AND page = ?", (file_id, page)
    ) as cursor:
        row = await cursor.fetchone()
    return row["text"] if row else ""


async def project_full_texts(conn: aiosqlite.Connection, project_id: str) -> dict[int, str]:
    """Paper id → the file holding its searchable full text.

    Read two ways: a paper in here is searched by its pages, so its abstract is
    not offered beside them, and its citation opens on the first page of that
    file when it has no DOI link.

    Searchable: a file with at least one passage. A scan without a text layer
    has none, and counting it made its paper vanish from the library entirely,
    no pages to find and its abstract held back in favour of them, with
    nothing anywhere saying so.
    """
    async with conn.execute(
        """
        SELECT f.paper_id, f.id FROM lit_files f JOIN lit_collections c ON c.id = f.collection_id
        WHERE c.project_id = ? AND f.paper_id IS NOT NULL
          AND EXISTS (SELECT 1 FROM lit_chunks k WHERE k.file_id = f.id)
        """,
        (project_id,),
    ) as cursor:
        return {row["paper_id"]: row["id"] for row in await cursor.fetchall()}
