"""Everything one run carries around, and the two things it shares.

`SourceRegistry` owns citation numbers. A number, once handed out, means the
same source for the rest of the run: in the prompt, in the answer, and in the
sidebar. Deep research reaches the same page from three sub-questions, so
deduplication has to be central rather than per-phase.

`Steps` turns "add a sub-step, do work, mark it done, mark it failed if it
raised" into a context manager, because that pattern otherwise repeats a dozen
times and gets the failure branch wrong at least once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

from app.agent import blocks
from app.agent.budget import Budget, Cancelled
from app.agent.depth import Depth
from app.llm import LLM, Usage
from app.search.base import SearchResult, canonical_url
from app.search.lenses import Lens
from app.search.searxng import SearchReport
from app.store.stream import RunStream

log = logging.getLogger(__name__)

Message = dict[str, Any]


# How many sources one domain may contribute, as a share of the whole list.
#
# `search.base.order` already caps this, but per search, and a run makes
# several: three queries each contributing their two Docker Hub pages is six,
# and "wie installiere ich pytorch mit cuda" came back with four of ten sources
# from one registry. The cap has to hold where the numbers are handed out,
# which is here, because that is the only place that sees the whole run.
_PER_DOMAIN_SHARE = 4
_PER_DOMAIN_MIN = 2


class SourceRegistry:
    """Assigns and remembers citation numbers, and mirrors them into the UI."""

    def __init__(self, stream: RunStream, limit: int) -> None:
        self._stream = stream
        self._limit = limit
        self._by_key: dict[str, int] = {}
        self._items: dict[int, SearchResult] = {}
        self._block_id: str | None = None

    @property
    def _per_domain(self) -> int:
        """Scaled to the preset: two on the cheap ones, more when there is room
        for a subject that genuinely lives on one site."""
        return max(_PER_DOMAIN_MIN, self._limit // _PER_DOMAIN_SHARE)

    def _domain_full(self, domain: str) -> bool:
        if not domain:
            return False
        seen = sum(1 for item in self._items.values() if item.domain == domain)
        return seen >= self._per_domain

    def __len__(self) -> int:
        return len(self._items)

    @property
    def full(self) -> bool:
        return len(self._items) >= self._limit

    def set_limit(self, limit: int) -> None:
        """Re-cap the registry once the router has chosen a depth.

        The registry is built before the classifier runs (sources have to be
        publishable the moment a search returns), so its ceiling starts as the
        provisional preset's. Never lowered below what is already registered:
        a source that has a citation number keeps it.
        """
        self._limit = max(limit, len(self._items))

    def allow_extra(self, count: int) -> None:
        """Raise the ceiling, for sources that are not evidence.

        A picture needs a citation number so the reader can see where it came
        from, but it must not take a slot from a source the answer is written
        from. The depth preset's `max_sources` is a budget for evidence.
        """
        self._limit += max(0, count)

    async def add(self, results: list[SearchResult]) -> list[tuple[int, SearchResult]]:
        """Register results, returning (citation number, result) pairs.

        A URL already known keeps its original number; if the new copy carries
        page content and the old one does not, the content is kept: it cost a
        fetch either way.
        """
        registered: list[tuple[int, SearchResult]] = []
        fresh: list[tuple[int, SearchResult]] = []

        for result in results:
            key = canonical_url(result.url)
            if not key:
                continue
            known = self._by_key.get(key)
            if known is not None:
                existing = self._items[known]
                if result.content and not existing.content:
                    existing.content = result.content
                registered.append((known, existing))
                continue
            if self.full:
                continue
            if self._domain_full(result.domain):
                log.debug("skipping %s: already at the per-domain cap", result.url)
                continue
            index = len(self._items) + 1
            self._by_key[key] = index
            self._items[index] = result
            registered.append((index, result))
            fresh.append((index, result))

        if fresh:
            await self._publish(fresh)
        return registered

    async def _publish(self, fresh: list[tuple[int, SearchResult]]) -> None:
        if self._block_id is None:
            block = blocks.sources_block()
            self._block_id = block["id"]
            await self._stream.emit_block(block)
        await self._stream.emit_patch(
            self._block_id,
            [blocks.add_source(result.as_source(index)) for index, result in fresh],
        )

    async def refresh(self) -> None:
        """Re-publish the whole list after the sources themselves have changed.

        Sources are published the moment they are registered, which is *before*
        the reading step runs, so without this the sidebar says "search snippet
        only" about every page the run went on to read in full, which is the one
        thing that distinguishes a balanced answer from a quick one.
        """
        if self._block_id is None or not self._items:
            return
        await self._stream.emit_patch(
            self._block_id, [blocks.replace("/data/sources", self.payload())]
        )

    def all(self) -> list[tuple[int, SearchResult]]:
        return sorted(self._items.items())

    def payload(self) -> list[dict[str, Any]]:
        return [result.as_source(index) for index, result in self.all()]

    def summary(self) -> dict[str, Any]:
        """Counts by provenance tier, for the footer.

        "10 sources" says nothing about whether an answer is checkable. "4
        official, 1 news, 5 web" says most of it.
        """
        tiers: dict[str, int] = {}
        read = 0
        for result in self._items.values():
            tiers[result.tier] = tiers.get(result.tier, 0) + 1
            read += bool(result.content)
        return {"total": len(self._items), "read": read, "tiers": tiers, "limit": self._limit}


class Steps:
    """Sub-steps inside a research block, as context managers."""

    def __init__(self, stream: RunStream, block_id: str) -> None:
        self._stream = stream
        self._block_id = block_id
        self._count = 0

    def __call__(self, text: str) -> _Step:
        return _Step(self, text)

    async def _open(self, text: str) -> int:
        index = self._count
        self._count += 1
        await self._stream.emit_patch(self._block_id, [blocks.add_sub_step(text)])
        return index

    async def _close(
        self, index: int, status: str, text: str | None = None, detail: str | None = None
    ) -> None:
        ops = [blocks.set_sub_step_status(index, status)]
        if text:
            ops.append(blocks.set_sub_step_text(index, text))
        if detail:
            ops.append(blocks.set_sub_step_detail(index, detail))
        await self._stream.emit_patch(self._block_id, ops)

    async def note(self, text: str) -> None:
        """A step that is already finished: recording a fact, not doing work."""
        index = await self._open(text)
        await self._close(index, blocks.STATUS_DONE)

    async def finish(self, status: str = blocks.STATUS_DONE) -> None:
        await self._stream.emit_patch(self._block_id, [blocks.set_status(status)])

    async def retitle(self, title: str) -> None:
        await self._stream.emit_patch(self._block_id, [blocks.set_title(title)])


class _Step:
    """`async with steps("Searching") as step:` completes or fails on exit.

    Assign `step.text` to replace the label with what actually happened
    ("3 searches · 24 results"), which is more useful after the fact than the
    intent was before it. `step.detail` is a quieter second line for what
    supports it, such as the engines that answered.
    """

    def __init__(self, owner: Steps, label: str) -> None:
        self._owner = owner
        self._label = label
        self._index = -1
        self.text: str | None = None
        self.detail: str | None = None

    async def __aenter__(self) -> _Step:
        self._index = await self._owner._open(self._label)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        status = blocks.STATUS_DONE if exc is None else blocks.STATUS_FAILED
        await self._owner._close(self._index, status, self.text, self.detail)
        return False  # never swallow the exception


@dataclass(slots=True)
class RunContext:
    """One run's world. Passed down rather than reached for globally."""

    stream: RunStream
    llm: LLM
    depth: Depth
    budget: Budget
    sources: SourceRegistry
    query: str
    # A small, fast model for triage only: the classifier call and nothing
    # else. None means "use the chat model", which is the default and is what a
    # single-model install wants.
    router: LLM | None = None
    # The model that checks the citations, when one is set apart for it. None
    # leaves the choice to `verify._judge`.
    judge: LLM | None = None
    # A literature project's papers, one line each (`Library.catalog`), for the
    # calls that write queries. Empty everywhere else.
    catalog: str = ""
    # What the request asked for, which may be AUTO. `depth` above is what the
    # router resolved it to, and is the only one anything spends against.
    requested_depth: Depth | None = None
    # Which corpora this run may search, and whether recency should outweigh
    # provenance. Both are decided once, up front, and read by every search.
    lenses: list[Lens] = field(default_factory=list)
    # Whether the reader left the corpora to the classifier. Kept so the footer
    # can say who chose them. A run that searched Science because the model
    # decided to is a different claim from one the reader asked for.
    auto_lenses: bool = False
    prefer_recent: bool = False
    time_range: str = ""
    # How the answer was laid out: "", figure, series, table, image, match,
    # diagram. In the footer because a reader looking at a scoreboard should be
    # able to see that the run decided to draw one.
    shape: str = ""
    # The overview's building blocks when `shape` is the overview, which is the
    # only thing the panel call is allowed to compose from.
    facets: tuple[str, ...] = ()
    # What the router decided and why, for the footer. A run that quietly did
    # less than it was asked to is the thing that would make routing untrustable.
    mode: str = ""
    route_reason: str = ""
    # Where "what is the weather" means, when the question does not say. Set in
    # Settings; empty is a normal state and the run says so rather than guessing.
    home_location: str = ""
    # Accumulated across every search the run makes, so the footer can report
    # which engines the answer actually rests on.
    search_report: SearchReport = field(default_factory=SearchReport)
    # Citation numbers of what the picture search found, in its own order. Kept
    # apart from the evidence because a web page may carry a thumbnail too, and
    # the picture shown beside a prose answer has to be one searched *as* a
    # picture of the subject, not the banner of whichever page ranked first.
    pictures: list[int] = field(default_factory=list)
    history: list[Message] = field(default_factory=list)
    # The pictures the question is about, as content parts, and whether they
    # came with an earlier question. Attached to the last user message of the
    # writer's prompt (`research.write_answer`) and of the classifier's.
    images: list[dict[str, Any]] = field(default_factory=list)
    images_earlier: bool = False
    embedding_model: str | None = None
    embedding_provider: str = "openrouter"
    # The reranker's bill. Its own sink rather than a client's, because
    # embeddings go out over plain httpx and have no `LLM` to hang off.
    embedding_usage: Usage = field(default_factory=Usage)
    # Past answers from the chat's project, waiting to be registered with the
    # first search of the run. See `take_project_sources`.
    project_sources: list[SearchResult] = field(default_factory=list)
    # A literature project's papers (`search.library.Library`), or None. When
    # set, the run is source-bound: it searches these, and the web only when
    # `web` is on. Typed loosely for the reason `project_sources` is handed over
    # as results: the agent searches it and never learns what stores it.
    library: Any = None
    web: bool = True
    # Whether a card may fall back to the first picture any source carries.
    # Off when the question is about a page the reader linked and they did not
    # ask to see anything: the picture would come from a search result about
    # the subject, not from the page, and is as likely a logo or a stock photo
    # as anything to do with it.
    stray_pictures: bool = True
    notices: list[str] = field(default_factory=list)

    def take_project_sources(self) -> list[SearchResult]:
        """The project's past answers, handed out exactly once.

        They join the first search's results so they are ranked against them
        rather than bolted on afterwards. Handing them out again for every
        sub-question would re-number the same chats five times, and a citation
        number that means two things is worse than a missing one.
        """
        sources, self.project_sources = self.project_sources, []
        return sources

    @property
    def source_bound(self) -> bool:
        """Whether this run may only answer from the person's own papers."""
        return self.library is not None and not self.web

    def checker(self) -> LLM:
        """The model that rules on the citations, see `verify._judge`."""
        if self.judge is not None:
            return self.judge
        if self.library is not None:
            return self.llm
        return self.router or self.llm

    @property
    def lens_ids(self) -> list[str]:
        return [lens.id for lens in self.lenses]

    def provenance(self) -> dict[str, Any]:
        """Everything the footer needs to explain the run to a sceptical reader."""
        requested = self.requested_depth or self.depth
        return {
            "depth": self.depth.id,
            "depth_label": self.depth.label,
            # What was asked for, next to what ran. They differ whenever the
            # router trimmed the work, and that has to be visible.
            "requested_depth": requested.id,
            "requested_depth_label": requested.label,
            "mode": self.mode,
            "route_reason": self.route_reason,
            "lenses": [{"id": lens.id, "label": lens.label} for lens in self.lenses],
            # Who chose them. A run that searched Science because the classifier
            # decided to is a different claim from one the reader asked for.
            "lenses_auto": self.auto_lenses,
            "search": self.search_report.as_dict(),
            "sources": self.sources.summary(),
            "recency": self.time_range,
            # Separate from `recency` because they are separate claims: one says
            # older sources were excluded, the other that recent ones were ranked
            # higher. A footer that prints "restricted to the past year" for the
            # second is describing a search that did not happen.
            "prefer_recent": self.prefer_recent,
            "shape": self.shape,
            # Who wrote and who checked. The same name twice is a model that
            # judged its own answer, which the reader is entitled to know.
            "models": {"writer": self.llm.model, "judge": self.checker().model},
            "stats": {
                **self.budget.summary(),
                **self.spend().as_dict(),
                # Its own meter, deliberately outside the token counts above.
                "embedding": self.embedding_usage.as_dict(),
                # And the one number that is a total, because a bill is a bill.
                "cost_usd": self._total_cost(),
            },
        }

    def spend(self) -> Usage:
        """What the language models did, across every one the run used.

        The router is a second client and often a second *model*, so a footer
        that read only the answer model would understate a run by a whole
        triage call.

        The embedding model is *not* in here, and that is the point. It is
        charged per source ranked rather than per call, and a deep run ranks its
        whole source list once per sub-question, so it contributes several
        hundred thousand tokens against a few thousand from the models. Summed
        into one figure, the footer read "456k tokens, $0.081" and invited the
        obvious arithmetic: at that token count the price looks impossible, and
        a reader who checks it concludes the bill is wrong rather than that two
        different meters were added together. The cost is still totalled below.

        One call is deliberately outside this number and cannot be inside it:
        the chat title (`runs._title`), which happens after the footer has been
        written. It runs on the triage model for that reason.
        """
        total = Usage()
        for client in (self.llm, self.router, self.judge):
            usage = getattr(client, "usage", None)
            if isinstance(usage, Usage):
                total.merge(usage)
        return total

    def _total_cost(self) -> float | None:
        """Every meter's price in one number, or None when nobody quoted one."""
        spent = self.spend()
        priced = spent.priced or self.embedding_usage.priced
        return round(spent.cost_usd + self.embedding_usage.cost_usd, 6) if priced else None

    async def guard(self) -> None:
        """Cooperative cancellation, checked between steps. Raises `Cancelled`."""
        if await self.stream.is_cancelled():
            raise Cancelled("Stopped at your request")

    async def notice(self, text: str, level: str = "warn") -> None:
        """Surface a degradation once. Repeating "SearXNG timed out" five times
        tells the user nothing the first one did not."""
        if text in self.notices:
            return
        self.notices.append(text)
        await self.stream.emit_block(blocks.notice_block(text, level))

    async def phase(self, phase: str) -> None:
        await self.stream.emit_phase(phase)
