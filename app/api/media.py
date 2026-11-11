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

import logging
from urllib.parse import unquote, urlsplit

import httpx
from fastapi import APIRouter, Path, Query, Response

from app import net
from app.api import proxy
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

FETCH_TIMEOUT_S = 6.0
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_FLAG_BYTES = 64 * 1024

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
    key = f"{FLAG_CACHE_PREFIX}{code}"
    icon = await proxy.cached(redis, key)
    if icon is None:
        icon, _content_type = await _get(FLAG_URL.format(code=code), MAX_FLAG_BYTES)
        await proxy.remember(redis, key, icon, ttl=FLAG_TTL_S)
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

    for candidate in (_commons_thumbnail(target), target):
        if not candidate:
            continue
        body, content_type = await _get(candidate, MAX_IMAGE_BYTES)
        if body:
            return _image(body, content_type)
    return _blank()


# Wikimedia renders thumbnails only at fixed widths and answers 400 to any
# other; 960 is one of them, and wide enough for every card in the app.
COMMONS_THUMB_WIDTH = 960
_COMMONS_HOST = "upload.wikimedia.org"
# Formats whose thumbnail keeps the original's name. An SVG's is rendered to
# PNG and gets ".png" appended; a TIFF or a PDF page has a naming scheme of
# its own and is left to the original.
_COMMONS_RASTER = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def _commons_thumbnail(url: str) -> str:
    """The Commons thumbnail for an original file, or "" when there is none.

    Search engines hand over the original upload, and a portrait on Commons is
    routinely a 6 MB photograph: over the size cap, so the proxy answered with
    its blank pixel and the profile card dropped the portrait it had chosen.
    The thumbnail is the same picture at a size a card can use, from the same
    host, so nothing about provenance changes. The original stays the fallback.
    """
    parsed = urlsplit(url)
    if (parsed.hostname or "").lower() != _COMMONS_HOST:
        return ""
    parts = parsed.path.split("/")
    # /wikipedia/<project>/<a>/<ab>/<file>; a thumbnail path has "thumb" after
    # the project and is already the light version.
    if len(parts) != 6 or parts[1] != "wikipedia" or parts[3] == "thumb":
        return ""
    project, first, pair, name = parts[2], parts[3], parts[4], parts[5]
    lower = name.lower()
    if lower.endswith(".svg"):
        thumb = f"{COMMONS_THUMB_WIDTH}px-{name}.png"
    elif lower.endswith(_COMMONS_RASTER):
        thumb = f"{COMMONS_THUMB_WIDTH}px-{name}"
    else:
        return ""
    return f"https://{_COMMONS_HOST}/wikipedia/{project}/thumb/{first}/{pair}/{name}/{thumb}"


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


async def _get(url: str, limit: int) -> tuple[bytes, str]:
    """The image at `url` and its type, or `(b"", "")` when it is not one that
    may be shown: unreachable, not an image, empty or over `limit`."""
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


def _image(body: bytes, content_type: str) -> Response:
    return Response(
        content=body,
        media_type=content_type or "image/png",
        headers={"Cache-Control": "public, max-age=86400", **_SVG_HEADERS},
    )


def _blank() -> Response:
    """The stand-in for a picture that could not be fetched. Never cached: a
    failure may be a timeout or a limit this server has since lifted, and a
    browser holding the blank pixel for an image URL keeps showing nothing
    for it long after the server would answer with the picture."""
    return Response(
        content=proxy.BLANK,
        media_type="image/gif",
        headers={"Cache-Control": "no-store"},
    )
