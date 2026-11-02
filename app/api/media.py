"""Images and flags, fetched by the server rather than by the page.

Two reasons, the same as for favicons. The CSP would have to be opened to every
host on the web for the page to load a search result's image directly; and the
reader's browser would announce itself to whichever site an image engine happened
to rank: sites they never chose to visit, for a picture they may not even scroll
to. Proxying keeps the page talking to one origin.

**Flags are cached, images are not.** A flag is two kilobytes from a set of about
250, so Redis holds them happily. A retrieved image is arbitrary and unbounded,
and this deployment's Redis runs `--maxmemory 256mb --maxmemory-policy
noeviction` because silently dropping a live run's event stream is worse than an
error. Filling it with photographs would take the app down rather than slow it.
So images stream through under a size cap and lean on the browser's own cache.

Both endpoints answer a failure with a transparent pixel rather than a 404. A
widget that has already been laid out must not reflow because a host was slow.
"""

from __future__ import annotations

import base64
import logging
from urllib.parse import unquote, urlsplit

import httpx
from fastapi import APIRouter, Path, Query, Response
from redis.asyncio import Redis

from app import net
from app.api.deps import RedisDep

router = APIRouter()

log = logging.getLogger(__name__)

# ISO 3166-1 alpha-2, lower case. Two letters and nothing else: this goes into
# an outbound URL, so it is validated rather than trusted.
FLAG_PATTERN = r"^[a-z]{2}$"
# flagcdn serves every ISO code as a plain PNG at a fixed width, which is exactly
# the bounded, predictable set this wants. Swapping it means changing one line.
FLAG_URL = "https://flagcdn.com/w80/{code}.png"

FLAG_CACHE_PREFIX = "flag:"
FLAG_TTL_S = 86400 * 30
MISS = "-"
MISS_TTL_S = 86400

FETCH_TIMEOUT_S = 6.0
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_FLAG_BYTES = 64 * 1024

BLANK = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")

# What a browser will render as a picture.
_ALLOWED_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/avif",
        "image/x-icon",
        # SVG is included, and it is the one that needs justifying. Excluding it
        # would make Wikimedia Commons useless: nearly every flag and diagram
        # there is an SVG, and Commons is the best-provenance image source in the
        # app. It is safe in the place it is used: an SVG loaded through `<img>`
        # cannot run script, by specification. The risk is a reader navigating
        # to this endpoint directly, where the browser would treat it as a
        # document, which `_SVG_HEADERS` closes.
        "image/svg+xml",
    }
)

# Applied to every proxied image, and load-bearing for SVG: nothing external,
# no script, no plugins, and sandboxed into an opaque origin, so a hostile SVG
# opened directly is inert and has no same-origin foothold here.
_SVG_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    "X-Content-Type-Options": "nosniff",
}


@router.get("/api/flag/{code}", include_in_schema=False)
async def flag(redis: RedisDep, code: str = Path(pattern=FLAG_PATTERN)) -> Response:
    """A country flag by ISO code.

    By code rather than by URL: it is the one image in the app a model
    has any influence over, and a code either names a country or names nothing.
    There is no argument here that could point at an arbitrary host.
    """
    cached = await _cached(redis, code)
    if cached is not None:
        return _image(cached, "image/png") if cached else _blank()

    icon = await _get(FLAG_URL.format(code=code), MAX_FLAG_BYTES)
    await _store(redis, code, icon)
    return _image(icon, "image/png") if icon else _blank()


@router.get("/api/image", include_in_schema=False)
async def image(url: str = Query(max_length=2000)) -> Response:
    """One retrieved image, by its https URL.

    The URL comes from a search result the run registered (the widget layer
    resolves a source number to it, so a model never names one), but this
    endpoint is reachable on its own and therefore trusts nothing: https only,
    an image content type only, and a hard size cap.
    """
    target = unquote(url).strip()
    if not _fetchable(target):
        return _blank()

    body, content_type = await _get_typed(target, MAX_IMAGE_BYTES)
    return _image(body, content_type) if body else _blank()


def _fetchable(url: str) -> bool:
    """https, a real hostname, and not something inside this network.

    The endpoint takes a URL from the client, so it is a request forger's
    doorway unless it refuses to go anywhere interesting. Public DNS names can
    still resolve inward, which no cheap check catches: the container reaches
    only redis and searxng by name, and both would answer a GET with something
    that is not an image and be discarded.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if host in ("localhost", "redis", "searxng", "simplixity") or host.endswith(".local"):
        return False
    return not (host[0].isdigit() or ":" in host)


async def _get_typed(url: str, limit: int) -> tuple[bytes, str]:
    try:
        response = await net.client().get(url, timeout=FETCH_TIMEOUT_S)
        response.raise_for_status()
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("image fetch %s failed: %s", url[:80], type(exc).__name__)
        return b"", ""

    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in _ALLOWED_TYPES:
        return b"", ""
    body = response.content
    if not body or len(body) > limit:
        return b"", ""
    return body, content_type


async def _get(url: str, limit: int) -> bytes:
    body, _content_type = await _get_typed(url, limit)
    return body


async def _cached(redis: Redis, code: str) -> bytes | None:
    """`None` = not looked up yet, `b""` = looked up and there is none."""
    try:
        raw = await redis.get(f"{FLAG_CACHE_PREFIX}{code}")
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


async def _store(redis: Redis, code: str, icon: bytes) -> None:
    try:
        if icon:
            await redis.set(
                f"{FLAG_CACHE_PREFIX}{code}", base64.b64encode(icon).decode(), ex=FLAG_TTL_S
            )
        else:
            await redis.set(f"{FLAG_CACHE_PREFIX}{code}", MISS, ex=MISS_TTL_S)
    except Exception as exc:
        log.debug("could not cache the flag for %s: %s", code, exc)


def _image(body: bytes, content_type: str) -> Response:
    return Response(
        content=body,
        media_type=content_type or "image/png",
        headers={"Cache-Control": "public, max-age=86400", **_SVG_HEADERS},
    )


def _blank() -> Response:
    return Response(
        content=BLANK,
        media_type="image/gif",
        headers={"Cache-Control": "public, max-age=3600"},
    )
