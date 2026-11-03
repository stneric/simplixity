"""Links the reader typed into the question.

A URL in the question is the reader naming the page the answer is about. That
is more specific than anything a search could find, so the page is read first,
in full, and cited as the main source (see `agent/linked.py`). This module only
finds the links; it does no I/O.

Only public web addresses count. Pages are read through the reader service,
and when that service is self-hosted it sits inside the same network as Redis
and SearXNG, so a link to `localhost` or a private address would ask it to
fetch something that was never meant to be reachable from a chat box.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

# More than this and the question is a list of links, not a question about a
# page. Each one is read in full, so the cap is also a cost ceiling.
MAX_LINKS = 3

# A scheme or a leading `www.`, then everything up to whitespace or a character
# that cannot sit in a URL typed into prose. Trailing punctuation is trimmed
# afterwards, because "see https://example.org/a." ends in a full stop, not in
# a path segment called "a.".
#
# Or a bare domain followed by a path, which is how a link is usually typed by
# hand: "github.com/stneric/simplixity". The path is what makes it a link. A
# bare name with a dot in it is as often a file or a library ("README.md",
# "Node.js") as a site, so "spiegel.de" on its own is left alone.
_URL = re.compile(
    r"(?:https?://|www\.)[^\s<>\"'`]+"
    r"|(?<![\w@./:-])(?:[a-z0-9-]+\.)+[a-z]{2,24}/[^\s<>\"'`]*",
    re.IGNORECASE,
)
_SCHEME = re.compile(r"^https?://", re.IGNORECASE)
# Quotation marks included, typographic ones written as escapes.
_TRAILING = ".,;:!?\"'\u00bb\u00ab\u201c\u201d\u2018\u2019"
_CLOSERS = {")": "(", "]": "[", "}": "{"}


def extract(text: str) -> list[str]:
    """The public web links in `text`, in order, without duplicates."""
    found: list[str] = []
    for match in _URL.finditer(text or ""):
        url = _trim(match.group(0))
        if not _SCHEME.match(url):
            url = f"https://{url}"
        if url not in found and is_public(url):
            found.append(url)
        if len(found) == MAX_LINKS:
            break
    return found


def remove(text: str) -> str:
    """`text` with its links removed, for a search query that must not carry
    one: an engine searches a URL as a string of words."""
    return " ".join(_URL.sub(" ", text or "").split())


def is_public(url: str) -> bool:
    """Whether `url` is an http(s) address on the public internet."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme not in ("http", "https") or not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # A name. One without a dot is a container or a machine on the local
        # network ("redis", "searxng"), and these are the internal suffixes.
        return "." in host and not host.endswith((".local", ".internal", ".localhost", ".lan"))
    return address.is_global


def _trim(url: str) -> str:
    """Drop punctuation that belongs to the sentence rather than the URL.

    A closing bracket is kept when the URL opened it, which is how Wikipedia
    writes disambiguations (`/wiki/Mercury_(planet)`), and dropped when it
    closes the reader's own parenthesis or a markdown link.
    """
    while url:
        last = url[-1]
        unbalanced = last in _CLOSERS and url.count(last) > url.count(_CLOSERS[last])
        if last not in _TRAILING and not unbalanced:
            break
        url = url[:-1]
    return url
