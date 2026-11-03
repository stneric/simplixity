"""Page content via r.jina.ai.

SearXNG hands back a two-line snippet written to be skimmed by a human. Reading
the page is what separates an answer that cites a source from one that merely
links to it. So this fetches markdown for the results the depth budget allows.

Reading pages is the slowest thing the app does, so it is bounded on three
axes: how many pages, how long each may take, and how much text each may
contribute. A page that fails simply keeps its snippet.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import httpx

from app import net
from app.config import settings
from app.search.base import SearchResult

log = logging.getLogger(__name__)

_gate = asyncio.Semaphore(settings.content_concurrency)

# r.jina.ai renders these, but the text is never worth the seconds it costs.
_SKIP_SUFFIXES = (".zip", ".exe", ".dmg", ".mp4", ".mp3", ".png", ".jpg", ".jpeg", ".gif", ".svg")


@dataclass(slots=True)
class Page:
    """A page as the reader returned it: its text, and what its header said."""

    text: str
    title: str = ""
    published: str = ""


async def fetch(url: str, *, max_chars: int | None = None) -> str:
    """Return page text as markdown, or "" if it could not be read."""
    page = await read_page(url, max_chars=max_chars)
    return page.text if page else ""


async def read_page(url: str, *, max_chars: int | None = None) -> Page | None:
    """Read one page with its title and date, or None if it could not be read.

    The title matters for a page the reader linked: it has no search result
    behind it, so the header is the only place its name comes from.
    """
    if url.lower().endswith(_SKIP_SUFFIXES):
        return None

    headers = {"X-Return-Format": "markdown", "Accept": "text/plain"}
    if settings.jina_api_key:
        headers["Authorization"] = f"Bearer {settings.jina_api_key}"

    try:
        async with _gate:
            response = await net.client().get(
                f"{settings.jina_reader_url.rstrip('/')}/{url}",
                headers=headers,
                timeout=settings.content_timeout_s,
            )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # 429 without a key is the common case and is not an error worth
        # shouting about: the snippet still carries the run.
        log.info("content fetch %s: HTTP %s", url, exc.response.status_code)
        return None
    except httpx.HTTPError as exc:
        log.info("content fetch %s failed: %s", url, type(exc).__name__)
        return None

    text = _clean(response.text, max_chars or settings.max_content_chars)
    if _unreadable(text):
        # HTTP 200 with a challenge screen in it, which is how the publishers
        # that matter most to a research question say no. Reported at info
        # because it is a routine outcome, not a fault in this app.
        log.info("content fetch %s: page was not served (wall or stub)", url)
        return None
    return Page(
        text=text,
        title=_header(response.text, "Title"),
        published=_header(response.text, "Published Time"),
    )


def _header(raw: str, name: str) -> str:
    """One field of the reader's header block (`Title: …`), or ""."""
    head, _separator, _body = raw.partition("Markdown Content:")
    for line in head.splitlines():
        if line.startswith(f"{name}:"):
            return " ".join(line[len(name) + 1 :].split())
    return ""


async def fetch_many(
    results: list[SearchResult], *, limit: int, max_chars: int | None = None
) -> int:
    """Fetch content for the first `limit` results, in place. Returns how many
    pages actually yielded text."""
    targets = [r for r in results if not r.content][:limit]
    if not targets:
        return 0

    texts = await asyncio.gather(
        *(fetch(r.url, max_chars=max_chars) for r in targets), return_exceptions=True
    )
    fetched = 0
    for result, text in zip(targets, texts, strict=True):
        if isinstance(text, str) and text:
            result.content = text
            fetched += 1
    return fetched


_LINK = re.compile(r"\[[^\]]*\]\([^)]*\)")
_LIST_ITEM = re.compile(r"^\s*([*+-]|\d+[.)])\s")

# Above this share of a line being markdown link, the line is a menu entry
# rather than a sentence that happens to cite something. Prose with two links
# in it lands around 0.2; a sidebar entry is 0.9 and up.
_LINK_DENSITY = 0.6
# Long enough that it is a paragraph, whatever its links: a real sentence with
# several citations must never be mistaken for a navigation row.
_PROSE_CHARS = 200

# What a page says when it was never served. A bot-verification interstitial, a
# "checking your browser" frame and a JavaScript shell all return HTTP 200 with
# a scrap of text in them, so nothing upstream treats them as failures: they
# become the source's content, get ranked like prose, and reach the model as if
# they were the paper. A deep run on a thesis question came back with four
# sub-questions reporting variations of "the provided source contains only a
# bot-verification interstitial", and the report was thin because three of its
# best publishers had answered with one of these.
#
# The phrases are matched only on a short body, which is what keeps a paper
# *about* CAPTCHAs from being thrown away: an interstitial is a sentence, an
# article is not.
_WALL = re.compile(
    r"verify(?:ing)?\s+(?:you|that you|yourself|your\s+browser)\b"
    r"|are\s+you\s+a\s+(?:human|robot)"
    r"|(?:i'?m|im)\s+not\s+a\s+robot"
    r"|just\s+a\s+moment"
    r"|enable\s+(?:javascript|cookies)"
    r"|turn\s+on\s+javascript"
    r"|javascript\s+is\s+(?:disabled|turned\s+off)"
    r"|checking\s+(?:if\s+)?(?:your\s+browser|the\s+site\s+connection)"
    r"|unusual\s+traffic"
    r"|access\s+(?:to\s+this\s+page\s+has\s+been\s+)?denied"
    r"|request\s+has\s+been\s+blocked"
    r"|captcha"
    r"|sicherheitsabfrage"
    r"|zugriff\s+verweigert"
    r"|bitte\s+aktivieren\s+sie\s+javascript",
    re.IGNORECASE,
)
_WALL_CHARS = 1200
# Below this there is no body at all, whatever the reason. Kept well under the
# shortest real article so that a stub abstract still counts as a page.
_EMPTY_CHARS = 80


def _unreadable(text: str) -> bool:
    """True when what came back is not the page: a wall, a stub, or nothing.

    Returning "" for these is what makes them harmless, `fetch_many` then leaves
    the result on its search snippet, which is a worse source than the page and
    an enormously better one than a challenge screen.
    """
    if len(text) < _EMPTY_CHARS:
        return True
    return len(text) < _WALL_CHARS and bool(_WALL.search(text))


def _link_density(line: str) -> float:
    """How much of a line is markdown links. Menus are almost all link."""
    stripped = line.strip()
    if not stripped:
        return 0.0
    return 1.0 - len(_LINK.sub("", stripped)) / len(stripped)


def _is_chrome(line: str) -> bool:
    """A navigation row: a short, link-dense list item, or a UI checkbox."""
    stripped = line.strip()
    if not stripped or len(stripped) >= _PROSE_CHARS:
        return False
    if stripped.startswith(("- [ ]", "- [x]", "* [ ]", "* [x]")):
        return True  # the reader's rendering of collapsible menus
    if _link_density(line) <= _LINK_DENSITY:
        return False
    return bool(_LIST_ITEM.match(line)) or _link_density(line) > 0.9


def _strip_chrome(lines: list[str]) -> list[str]:
    """Drop navigation rows wherever they sit, and keep every paragraph.

    This is not cosmetic. The reader returns a page as markdown *including* its
    sidebar, and the German Wikipedia article on Leonardo DiCaprio opens with
    24 000 characters of menu (exactly the cap), so the run read the
    navigation, none of the article, and answered that the sources do not say
    what his father did. It is in the second paragraph.

    A filter rather than a cut, because the menu is not only at the top and the
    article does not start at one identifiable line: cutting to the first long
    paragraph skipped the whole introduction on the very page that prompted
    this. Prose is decided per line, so an intro survives wherever it is.
    """
    kept = [line for line in lines if not _is_chrome(line)]
    # Never hand back an empty page because a heuristic was too eager: a page
    # that really is a list of links keeps what it had.
    if sum(len(line) for line in kept) < sum(len(line) for line in lines) * 0.15:
        return lines
    return kept


def _clean(text: str, max_chars: int) -> str:
    """Drop the reader's header block and collapse the blank-line noise that
    markdown conversion leaves behind."""
    body = text
    for marker in ("Markdown Content:", "Content:"):
        _, separator, tail = body.partition(f"{marker}\n")
        if separator:
            body = tail
            break

    lines = _strip_chrome([line.rstrip() for line in body.splitlines()])
    collapsed: list[str] = []
    for line in lines:
        if not line and collapsed and not collapsed[-1]:
            continue
        collapsed.append(line)

    cleaned = "\n".join(collapsed).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    # Cut on a paragraph boundary so a chunk never ends mid-sentence.
    head = cleaned[:max_chars]
    boundary = head.rfind("\n\n")
    return head[:boundary] if boundary > max_chars // 2 else head
