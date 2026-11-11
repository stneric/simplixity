"""Pages the reader linked in the question, read first and cited first.

A link in the question says which page the answer is about, and that outranks
everything the ranking could decide. So the page is not treated as one more
search result:

* **Read before the classifier.** The classifier writes the search queries, and
  "what do other sources say about this?" has no queries until the page's
  subject is known. Reading first costs the page's latency once; the searched
  path reads it anyway, later.
* **Read whatever the preset.** Quick reads no pages, which is right for search
  results and wrong for the one page the reader pointed at.
* **Registered first and outside the source budget,** like the library
  overview: it holds citation number 1, and it takes no slot from the evidence
  a search finds.
* **Handed to the writer whole and ahead of the ranked passages,** so a
  ranking that prefers another page cannot push it out of the prompt, and
  `_fit_to_window` trims the search results before it trims this.

A page that cannot be read is said so, and the run carries on as an ordinary
search: an answer about the subject beats a blank page, as long as the reader
knows it is not the page they linked.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging

from app.agent import route as route_module
from app.agent.context import RunContext, Steps, ellipsis
from app.agent.depth import Depth
from app.agent.route import Route
from app.embed.rerank import Passage, passages_from
from app.search import content
from app.search.base import SearchResult

log = logging.getLogger(__name__)

# Twice what a search result may contribute. A linked article is the subject,
# and cutting it at the length that suits a supporting page would drop its
# second half from a request to summarise it.
PAGE_CHARS = 48_000

# How much of each page the classifier sees: enough to name the subject and
# write queries about it, at a few hundred tokens.
EXCERPT_CHARS = 1200


@dataclasses.dataclass(slots=True)
class Linked:
    """The linked pages that could be read, as the writer will see them."""

    passages: list[Passage] = dataclasses.field(default_factory=list)
    # (citation number, title, opening text) per page, for the classifier.
    pages: list[tuple[int, str, str]] = dataclasses.field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.passages)

    @property
    def indexes(self) -> list[int]:
        return [index for index, _title, _excerpt in self.pages]


async def read(ctx: RunContext, urls: list[str], steps: Steps) -> Linked:
    """Read and register the linked pages. Never raises for a page that fails."""
    if not urls:
        return Linked()

    label = "the linked page" if len(urls) == 1 else f"{len(urls)} linked pages"
    async with steps(f"Reading {label}") as step:
        pages = await asyncio.gather(
            *(content.read_page(url, max_chars=PAGE_CHARS) for url in urls),
            return_exceptions=True,
        )
        results: list[SearchResult] = []
        failed: list[str] = []
        for url, page in zip(urls, pages, strict=True):
            if not isinstance(page, content.Page):
                if isinstance(page, BaseException):
                    log.info("linked page %s failed: %s", url, page)
                failed.append(url)
                continue
            result = SearchResult(
                title=page.title, url=url, published=page.published, content=page.text
            )
            result.title = result.title or result.domain or url
            result.assess()
            results.append(result)

        ctx.budget.pages_fetched += len(results)
        registered = []
        if results:
            ctx.sources.allow_extra(len(results))
            registered = await ctx.sources.add(results)
        step.text = _said(registered, failed)

    if failed:
        await ctx.notice(
            f"Could not read {', '.join(failed)}. The page may block automated "
            "readers or need a login, so this answer is "
            + ("written from the other pages and a search." if registered else "searched instead."),
            "warn",
        )
    return Linked(
        passages=passages_from(registered),
        pages=[(index, r.title, r.content[:EXCERPT_CHARS]) for index, r in registered],
    )


def route(current: Route, requested: Depth, *, read: bool) -> Route:
    """The route for a question the classifier would answer from the model alone.

    With the page read, that is a question about the page ("summarise this"),
    and it is answered from the page without a search. Without it, it is a
    question about a page the run never saw, which only a search can answer.
    """
    widened = route_module.widened(current, requested)
    reason = (
        f"Answered from the linked page · {widened.depth.label}"
        if read
        else f"The linked page could not be read · searching at {widened.depth.label}"
    )
    return dataclasses.replace(widened, reason=reason)


def _said(registered: list, failed: list[str]) -> str:
    if not registered:
        return "Could not read the linked page" if len(failed) == 1 else "Could not read the links"
    titles = " · ".join(ellipsis(result.title, 60) for _index, result in registered)
    if failed:
        return f"Read {titles}; {len(failed)} could not be opened"
    return f"Read {titles}"
