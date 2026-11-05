"""Attaching a file to a chat: read it, store it, index it.

The same steps as a full text on the literature page (`tools.fulltext`), minus
the one that makes no sense here, matching the file to a paper: an attached
file is what it is called. Everything after reading goes through the same
store and the same indexer, so an attached PDF is searched, cited and opened
exactly like a paper's.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import aiosqlite

from app.scholar import fulltext
from app.store import attachments as store
from app.store import images, library
from app.tools import documents
from app.tools.fulltext import current_tag, index


async def upload(
    conn: aiosqlite.Connection, chat_id: str, name: str, data: bytes, *, user: str
) -> dict[str, Any]:
    """Store a file under a chat and index it. Raises `FulltextError` with a
    sentence the person can act on when the file cannot be used.

    A picture is stored as one (`store.images`) and not indexed: it has no text
    to search, and is shown to the model with the question instead."""
    if images.looks_like_image(data):
        try:
            image, new = await images.add(conn, chat_id, user, name, data)
        except images.ImageError as exc:
            raise fulltext.FulltextError(str(exc)) from exc
        return {"file": image, "new": new, "index": None}
    document = await asyncio.to_thread(documents.read, name, data)
    collection = await store.collection_of(conn, chat_id, user, create=True)
    assert collection is not None
    existing = await store.files(conn, chat_id, user)
    sha = hashlib.sha256(data).hexdigest()
    if len(existing) >= store.MAX_FILES and all(f["sha256"] != sha for f in existing):
        raise fulltext.FulltextError(
            f"A chat holds at most {store.MAX_FILES} files. For more, put them in a "
            "literature project, whose search is built for a whole library."
        )
    extracted = document.extracted
    pieces = fulltext.chunks(extracted.pages)
    file, new = await library.add_file(
        conn,
        collection["id"],
        name=(name or "file")[:200],
        sha256=sha,
        data=data,
        pages=extracted.pages,
        text_pages=extracted.text_pages,
        warnings=list(extracted.warnings),
        chunks=[(c.page, c.text) for c in pieces],
        kind=document.kind,
    )
    indexed = await index(conn, collection, user=user) if new else None
    return {
        "file": await file_of(conn, chat_id, file["id"], user=user),
        "new": new,
        "index": indexed,
    }


async def file_of(
    conn: aiosqlite.Connection, chat_id: str, file_id: str, *, user: str
) -> dict[str, Any] | None:
    for file in await listed(conn, chat_id, user=user):
        if file["id"] == file_id:
            return file
    return None


async def listed(conn: aiosqlite.Connection, chat_id: str, *, user: str) -> list[dict[str, Any]]:
    """A chat's files, each counted as indexed against the current model, and
    its pictures, in the order they were attached."""
    documents_ = await store.files(conn, chat_id, user, await current_tag(conn, user))
    pictures = await images.listed(conn, chat_id, user)
    return sorted([*documents_, *pictures], key=lambda f: str(f.get("created_at") or ""))


async def remove(conn: aiosqlite.Connection, chat_id: str, file_id: str, *, user: str) -> bool:
    if await images.remove(conn, chat_id, file_id, user):
        return True
    collection = await store.collection_of(conn, chat_id, user)
    if collection is None:
        return False
    return await library.delete_file(conn, collection["id"], file_id)
