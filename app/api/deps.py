"""Shared request-scoped concerns: resources, identifiers, rate limiting."""

from __future__ import annotations

import logging
import time
from typing import Annotated

import aiosqlite
from fastapi import Depends, HTTPException, Path, Request
from redis.asyncio import Redis

from app.config import settings
from app.store import chats, profiles

log = logging.getLogger(__name__)

# Ids are generated as uuid4 hex, but they arrive from the client, so they are
# validated rather than trusted.
ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"


def get_redis(request: Request) -> Redis:
    return request.app.state.redis


def get_db(request: Request) -> aiosqlite.Connection:
    return request.app.state.db


RedisDep = Annotated[Redis, Depends(get_redis)]
DbDep = Annotated[aiosqlite.Connection, Depends(get_db)]

# Lowercase so "Eric" and "eric" are one profile, and without dots, which is
# what separates the name from its signature in the session cookie.
SESSION_COOKIE = "simplixity_session"
USER_PATTERN = r"^[a-z0-9_-]{1,32}$"


async def current_user(request: Request, db: DbDep) -> str:
    """Whose chats, projects and settings this request sees.

    A signed session cookie set by `POST /api/profiles/login`. A cookie rather
    than a header because `EventSource` cannot set headers and sends cookies by
    itself. No valid session is a 401, which the frontend answers by showing
    the profile picker; falling back to some default profile instead would
    make a password on that profile mean nothing.
    """
    token = request.cookies.get(SESSION_COOKIE, "")
    user = await profiles.verify_session(db, token) if token else None
    if user is None:
        raise HTTPException(status_code=401, detail="Choose a profile first")
    return user


UserDep = Annotated[str, Depends(current_user)]


async def owned_run(
    run_id: Annotated[str, Path(pattern=ID_PATTERN)], db: DbDep, user: UserDep
) -> str:
    """A run id the signed-in profile may read or stop, else 404.

    Run ids are unguessable, but that made them bearer tokens: with no session
    at all, anyone holding one could read the question and the answer, or stop
    the run. Another profile's run reads as missing, like its chat does.
    """
    if await chats.run_owner(db, run_id) != user:
        raise HTTPException(status_code=404, detail="Unknown run")
    return run_id


OwnedRunId = Annotated[str, Depends(owned_run)]


def client_ip(request: Request) -> str:
    """The caller's address, as uvicorn resolved it.

    `X-Forwarded-For` is applied once, by uvicorn, and only for a peer listed
    in `FORWARDED_ALLOW_IPS` (`app/__main__.py`). Reading the header again here
    took it from anyone: a client on the published port could name a new
    address on every request and never meet the rate limit.
    """
    return request.client.host if request.client else "unknown"


async def rate_limit(request: Request) -> None:
    """A fixed-window cap on run creation.

    This is not a security boundary; it is a stop on runaway clients. It
    exists because a reload loop or a runaway script can otherwise spend an API
    budget overnight, and a 429 is a much better outcome than an invoice.
    """
    limit = settings.rate_limit_runs_per_minute
    if limit <= 0:
        return

    redis: Redis = request.app.state.redis
    window = int(time.time() // 60)
    key = f"ratelimit:runs:{client_ip(request)}:{window}"
    try:
        async with redis.pipeline(transaction=False) as pipe:
            pipe.incr(key)
            pipe.expire(key, 120)
            count, _ = await pipe.execute()
    except Exception:
        log.warning("rate limit check failed; allowing the request")
        return

    if count > limit:
        raise HTTPException(
            status_code=429,
            detail=f"More than {limit} runs in a minute. Wait a moment and try again.",
            headers={"Retry-After": "30"},
        )
