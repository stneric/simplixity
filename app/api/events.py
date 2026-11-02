from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import OwnedRunId
from app.config import settings
from app.store.stream import TERMINAL_EVENT, RunStream

log = logging.getLogger(__name__)
router = APIRouter()

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # Traefik does not buffer SSE, but a proxy in front of it might.
    "X-Accel-Buffering": "no",
}


@router.get("/api/runs/{run_id}/events")
async def run_events(
    request: Request,
    run_id: OwnedRunId,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    cursor: str | None = Query(default=None, alias="last_event_id"),
) -> StreamingResponse:
    """Replay-then-follow the run's event stream.

    `EventSource` sends `Last-Event-ID` by itself on reconnect, so resume needs
    no client logic at all. The query parameter is a convenience for curl and
    any non-EventSource client.
    """
    stream = RunStream(request.app.state.redis, run_id, ttl=settings.run_ttl_seconds)
    if not await stream.exists():
        raise HTTPException(status_code=404, detail="Unknown run")

    start = last_event_id or cursor or "0"

    async def publish() -> AsyncIterator[str]:
        # Browsers default to a 3s reconnect; make it explicit.
        yield "retry: 3000\n\n"
        try:
            async for event in stream.read(last_id=start):
                if await request.is_disconnected():
                    break
                if event is None:
                    yield ": ping\n\n"
                    continue
                yield f"id: {event.id}\nevent: {event.event}\ndata: {event.raw}\n\n"
                if event.event == TERMINAL_EVENT:
                    break
        except Exception:
            # Deliberately do NOT synthesise a `done` here. A terminal event is
            # a statement about the *run*, and this layer only knows that the
            # *transport* broke. Ending the response lets EventSource retry and
            # replay from Last-Event-ID; faking `done` would close it forever.
            log.exception("SSE transport failed for run %s; client will retry", run_id)

    return StreamingResponse(publish(), media_type="text/event-stream", headers=SSE_HEADERS)
