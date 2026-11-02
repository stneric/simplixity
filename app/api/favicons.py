"""Favicons for the source hover card, fetched by the server rather than the page.

The obvious implementation is `<img src="https://thatsite/favicon.ico">`, and it
is the wrong one twice over. It would need `img-src https:` in the CSP, undoing
the point of vendoring the icons and the markdown renderer; and it would have
the reader's browser announce itself to a dozen sites they have not visited,
every time a source list renders, including the ones the answer merely
considered. The proxy keeps both properties: the page still talks only to this
origin, and the sites see one request from the server, once per day.

Failure is not an error here. A site without a favicon, a site that is down, a
site serving 900KB of PNG. All of them resolve to the same transparent pixel,
because the hover card must not wait on any of it.
"""

from __future__ import annotations

import asyncio
import base64
import logging

import httpx
from fastapi import APIRouter, Path, Response
from redis.asyncio import Redis

from app import net
from app.api.deps import RedisDep

router = APIRouter()

log = logging.getLogger(__name__)

# A hostname, conservatively: at least two labels of letters, digits and
# hyphens, lowercase. This value goes into an outbound URL, so it is validated
# rather than trusted, and validated by pattern, whose engine has no
# look-around, so the overall length is capped by `max_length` instead.
DOMAIN_PATTERN = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"

CACHE_PREFIX = "favicon:"
CACHE_TTL_S = 86400 * 7
# A miss is cached too, and for almost as long: re-fetching a favicon that did
# not exist yesterday, on every render, is the whole cost this endpoint avoids.
MISS_TTL_S = 86400
MISS = "-"

FETCH_TIMEOUT_S = 4.0
MAX_BYTES = 100 * 1024

# 1x1 transparent GIF. Served for every failure so the card's layout never
# shifts and the browser never retries.
BLANK = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


@router.get("/api/favicon/{domain}", include_in_schema=False)
async def favicon(
    redis: RedisDep, domain: str = Path(pattern=DOMAIN_PATTERN, max_length=253)
) -> Response:
    cached = await _cached(redis, domain)
    if cached is not None:
        return _image(cached) if cached else _blank()

    icon = await _fetch(domain)
    await _store(redis, domain, icon)
    return _image(icon) if icon else _blank()


async def _cached(redis: Redis, domain: str) -> bytes | None:
    """`None` = not looked up yet, `b""` = looked up and there is none."""
    try:
        raw = await redis.get(f"{CACHE_PREFIX}{domain}")
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


async def _store(redis: Redis, domain: str, icon: bytes) -> None:
    try:
        if icon:
            await redis.set(
                f"{CACHE_PREFIX}{domain}", base64.b64encode(icon).decode(), ex=CACHE_TTL_S
            )
        else:
            await redis.set(f"{CACHE_PREFIX}{domain}", MISS, ex=MISS_TTL_S)
    except Exception as exc:
        log.debug("could not cache the favicon for %s: %s", domain, exc)


async def _fetch(domain: str) -> bytes:
    """Try the two conventional locations at once and take whichever answers.

    Concurrently rather than in sequence: this is on the path of a hover, and two
    four-second timeouts back to back is a card that appears after the pointer
    has moved on.
    """
    candidates = (
        f"https://{domain}/favicon.ico",
        f"https://{domain}/favicon.png",
    )
    results = await asyncio.gather(*(_get(url) for url in candidates), return_exceptions=True)
    for result in results:
        if isinstance(result, bytes) and result:
            return result
    return b""


async def _get(url: str) -> bytes:
    try:
        response = await net.client().get(url, timeout=FETCH_TIMEOUT_S)
        response.raise_for_status()
    except (httpx.HTTPError, ValueError):
        return b""

    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    # An HTML error page served with 200 is the common case for a missing icon.
    if content_type and not content_type.startswith("image/"):
        return b""
    body = response.content
    return body if body and len(body) <= MAX_BYTES else b""


def _image(icon: bytes) -> Response:
    return Response(
        content=icon,
        # Sniffing is off everywhere (X-Content-Type-Options), and these bytes
        # come from a third party, so they are served under one inert type
        # rather than whatever the remote server claimed.
        media_type="image/x-icon",
        headers={"Cache-Control": "public, max-age=86400"},
    )


def _blank() -> Response:
    return Response(
        content=BLANK,
        media_type="image/gif",
        headers={"Cache-Control": "public, max-age=3600"},
    )
