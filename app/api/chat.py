from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import runs
from app.api.deps import ID_PATTERN, DbDep, OwnedRunId, RedisDep, UserDep, rate_limit
from app.config import settings
from app.store import chats, projects
from app.store.stream import RunStream

router = APIRouter()


class ChatRequest(BaseModel):
    query: str = Field(min_length=1, max_length=8000)
    # Every field below may be omitted: the server falls back to the settings
    # the picker saved, so the UI never has to repeat itself.
    depth: str | None = None
    # Source corpora for this run. Unknown ids are dropped rather than rejected:
    # the resolver falls back to the default, so a stale client still searches.
    lenses: list[str] | None = Field(default=None, max_length=8)
    chat_id: str | None = Field(default=None, pattern=ID_PATTERN)
    model: str | None = None
    provider: str | None = None
    # Which project a *new* chat is filed under. Ignored for an existing chat,
    # which keeps whatever it was filed under.
    project_id: str | None = Field(default=None, max_length=64)
    # Only read in a literature project, whose chats search the person's papers
    # alone unless this is set. Off by default, so a client that does not know
    # about it gets the safe behaviour.
    web: bool = False
    # Whether the project's earlier chats are searched too. Off unless sent:
    # an earlier answer is the reader's own conclusion, and drawing on it is
    # something they choose per question, not something that happens to them.
    chats: bool = False


class ChatResponse(BaseModel):
    run_id: str
    chat_id: str
    message_id: str
    depth: str
    model: str
    lenses: list[str]


@router.post(
    "/api/chat",
    response_model=ChatResponse,
    dependencies=[Depends(rate_limit)],
)
async def create_chat(body: ChatRequest, redis: RedisDep, db: DbDep, user: UserDep) -> ChatResponse:
    """Start a run and return immediately. All output arrives over SSE."""
    # Checked here, not in the store: `ensure_chat` appends to whatever chat id
    # it is given, and another profile's chat or project would otherwise lend
    # its history and its corpus to this run.
    if body.chat_id and (await chats.owner(db, body.chat_id)) not in (None, user):
        raise HTTPException(status_code=404, detail="Unknown chat")
    if body.project_id and await projects.get(db, body.project_id, user=user) is None:
        raise HTTPException(status_code=404, detail="Unknown project")
    try:
        handle = await runs.start(
            redis=redis,
            db=db,
            query=body.query.strip(),
            chat_id=body.chat_id,
            model=body.model,
            provider=body.provider,
            depth=body.depth,
            lenses=body.lenses,
            project_id=body.project_id or None,
            web=body.web,
            earlier_chats=body.chats,
            user=user,
        )
    except runs.AtCapacity as exc:
        raise HTTPException(
            status_code=503, detail=str(exc), headers={"Retry-After": "20"}
        ) from exc

    return ChatResponse(
        run_id=handle.run_id,
        chat_id=handle.chat_id,
        message_id=handle.message_id,
        depth=handle.depth,
        model=handle.model,
        lenses=handle.lenses,
    )


@router.get("/api/runs/{run_id}")
async def get_run(run_id: OwnedRunId, redis: RedisDep) -> dict:
    """State plus current block snapshots, so a late joiner can render without
    replaying the whole event stream."""
    stream = RunStream(redis, run_id, ttl=settings.run_ttl_seconds)
    if not await stream.exists():
        raise HTTPException(status_code=404, detail="Unknown run")
    return {
        "run_id": run_id,
        "state": await stream.get_state(),
        "blocks": await stream.snapshot(),
    }


@router.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: OwnedRunId, redis: RedisDep) -> dict:
    if not await runs.cancel(redis, run_id):
        raise HTTPException(status_code=404, detail="Unknown run")
    return {"ok": True}
