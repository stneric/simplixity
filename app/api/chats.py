"""Chat history: list, open, rename, delete, and the files attached to a chat."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field

from app import runs
from app.api.deps import ID_PATTERN, DbDep, RedisDep, UserDep
from app.scholar import fulltext
from app.store import attachments as attachment_store
from app.store import chats, projects
from app.tools import attachments

router = APIRouter()

ChatId = Annotated[str, Path(pattern=ID_PATTERN)]
FileId = Annotated[str, Path(pattern=ID_PATTERN)]


class RenameRequest(BaseModel):
    """Both fields are optional and independent: the sidebar renames, the
    project menu files, and neither has to send the other's value."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    # `None` means "not stated"; filing a chat *out* of a project is the empty
    # string, which is the one case a nullable field cannot express on its own.
    project_id: str | None = Field(default=None, max_length=64)


@router.get("/api/chats")
async def list_chats(
    db: DbDep,
    user: UserDep,
    search: str | None = Query(default=None, max_length=200),
    project_id: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict:
    return {
        "chats": await chats.list_chats(
            db, limit=limit, search=search, project_id=project_id or None, user=user
        )
    }


@router.get("/api/chats/{chat_id}")
async def get_chat(chat_id: ChatId, db: DbDep, user: UserDep) -> dict:
    """The whole transcript, blocks included: reopening a chat renders exactly
    what was streamed the first time."""
    chat = await chats.get_chat(db, chat_id, user=user)
    if chat is None:
        raise HTTPException(status_code=404, detail="Unknown chat")
    return {**chat, "files": await attachments.listed(db, chat_id, user=user)}


@router.patch("/api/chats/{chat_id}")
async def update_chat(chat_id: ChatId, body: RenameRequest, db: DbDep, user: UserDep) -> dict:
    # Ownership only: `get_chat` would decode the whole transcript to rename it.
    if await chats.owner(db, chat_id) != user:
        raise HTTPException(status_code=404, detail="Unknown chat")
    if body.title is not None:
        await chats.set_title(db, chat_id, body.title)
    if body.project_id is not None:
        target = body.project_id or None
        if target and await projects.get(db, target, user=user) is None:
            raise HTTPException(status_code=404, detail="Unknown project")
        await projects.assign(db, chat_id, target)
    return {"ok": True}


@router.delete("/api/chats/{chat_id}")
async def delete_chat(chat_id: ChatId, db: DbDep, redis: RedisDep, user: UserDep) -> dict:
    # A run still answering in it is stopped first. Closing the tab never
    # cancels a run, by design, so without this a deleted chat went on spending
    # the API budget and holding a slot, reading files deleted under it, to
    # write an answer into rows that no longer exist.
    if await chats.owner(db, chat_id) == user:
        for run_id in await chats.running_runs(db, chat_id):
            await runs.cancel(redis, run_id)
    if not await chats.delete_chat(db, chat_id, user=user):
        raise HTTPException(status_code=404, detail="Unknown chat")
    return {"ok": True}


# ------------------------------------------------------------------ attachments
#
# Addressed by chat id, which for a new chat is one the browser made up: a file
# is attached before the first question, when no chat exists yet, and the
# question then names the same id. So "no such chat" is a normal state here,
# and what is refused is only an id another profile already holds.


async def _attachable(db, chat_id: str, user: str) -> None:
    if (await chats.owner(db, chat_id)) not in (None, user) or await attachment_store.taken(
        db, chat_id, user
    ):
        raise HTTPException(status_code=404, detail="Unknown chat")


@router.get("/api/chats/{chat_id}/files")
async def list_files(chat_id: ChatId, db: DbDep, user: UserDep) -> dict:
    await _attachable(db, chat_id, user)
    return {"files": await attachments.listed(db, chat_id, user=user)}


@router.post("/api/chats/{chat_id}/files")
async def attach_file(chat_id: ChatId, request: Request, db: DbDep, user: UserDep) -> dict:
    """One file as the raw request body, its name in `X-Filename`, as on the
    literature page and for the same reason: multipart would be a dependency
    for one field."""
    await _attachable(db, chat_id, user)
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="The upload was empty.")
    if len(data) > fulltext.MAX_BYTES:
        raise HTTPException(status_code=413, detail="The file is too large.")
    name = unquote(request.headers.get("x-filename", "")) or "file"
    try:
        return await attachments.upload(db, chat_id, name, data, user=user)
    except fulltext.FulltextError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.delete("/api/chats/{chat_id}/files/{file_id}")
async def detach_file(chat_id: ChatId, file_id: FileId, db: DbDep, user: UserDep) -> dict:
    """Take a file off the chat. Answers already written keep citing it, and
    those citations stop opening: the file is gone, as asked."""
    await _attachable(db, chat_id, user)
    if not await attachments.remove(db, chat_id, file_id, user=user):
        raise HTTPException(status_code=404, detail="Unknown file")
    return {"ok": True}
