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

import httpx
from fastapi import APIRouter, Path, Response

from app import net
from app.api import proxy
from app.api.deps import RedisDep

router = APIRouter()

# A hostname, conservatively: at least two labels of letters, digits and
# hyphens, lowercase. This value goes into an outbound URL, so it is validated
# rather than trusted, and validated by pattern, whose engine has no
# look-around, so the overall length is capped by `max_length` instead.
DOMAIN_PATTERN = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"

CACHE_PREFIX = "favicon:"
CACHE_TTL_S = 86400 * 7

FETCH_TIMEOUT_S = 4.0
MAX_BYTES = 100 * 1024


@router.get("/api/favicon/{domain}", include_in_schema=False)
async def favicon(
    redis: RedisDep, domain: str = Path(pattern=DOMAIN_PATTERN, max_length=253)
) -> Response:
    key = f"{CACHE_PREFIX}{domain}"
    icon = await proxy.cached(redis, key)
    if icon is None:
        icon = await _fetch(domain)
        await proxy.remember(redis, key, icon, ttl=CACHE_TTL_S)
    return _image(icon) if icon else _blank()


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
        content=proxy.BLANK,
        media_type="image/gif",
        headers={"Cache-Control": "public, max-age=3600"},
    )
