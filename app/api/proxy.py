"""What the image proxies share: the stand-in pixel and a small Redis cache.

Favicons and flags are both tiny, bounded sets fetched from third parties on
the reader's behalf (`favicons.py`, `media.py`), so both are kept in Redis, and
a miss is kept too: re-fetching an icon that did not exist yesterday, on every
render, is the whole cost these endpoints avoid.
"""

from __future__ import annotations

import base64
import logging

from redis.asyncio import Redis

log = logging.getLogger(__name__)

# 1x1 transparent GIF. Served for every failure, so a layout that has already
# been drawn never shifts and the browser never retries.
BLANK = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")

MISS = "-"
MISS_TTL_S = 86400


async def cached(redis: Redis, key: str) -> bytes | None:
    """`None` = not looked up yet, `b""` = looked up and there is none.

    Never raises: a cache that cannot be read is a cache miss."""
    try:
        raw = await redis.get(key)
    except Exception:
        return None
    if raw is None:
        return None
    if raw == MISS:
        return b""
    try:
        return base64.b64decode(raw)
    except Exception:
        return b""


async def remember(redis: Redis, key: str, body: bytes, *, ttl: int) -> None:
    """Keep what a fetch returned, or that it returned nothing. Never raises."""
    try:
        if body:
            await redis.set(key, base64.b64encode(body).decode(), ex=ttl)
        else:
            await redis.set(key, MISS, ex=MISS_TTL_S)
    except Exception as exc:
        log.debug("could not cache %s: %s", key, exc)
