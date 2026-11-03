"""The four things every mode does: search, read, rank, write.

Quick, balanced and deep differ in how much of each they do, not in how they
do it. So the primitives live here and the modes just spend different budgets
against them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from typing import Any

from app.agent import blocks, prompts
from app.agent.context import RunContext, Steps
from app.embed.rerank import Passage, rank_passages
from app.llm import Delta, LLMError
from app.search import content as content_fetch
from app.search.base import SearchResult, keep_relevant, order
from app.search.lenses import LIBRARY
from app.search.searxng import SearchReport, search_all

log = logging.getLogger(__name__)

# Streaming one XADD per token would hammer Redis and the browser for no visible
# gain: below roughly this cadence the eye reads it as continuous text anyway.
_FLUSH_CHARS = 48
_FLUSH_SECONDS = 0.12

# Below this many sources a run that rested on a single engine is worth a word.
# Above it the one engine found enough, and the footer already names it.
_THIN_SOURCES = 5


async def search_and_register(
    ctx: RunContext,
    queries: list[str],
    steps: Steps,
    *,
    results_per_query: int,
    question: str = "",
) -> list[tuple[int, SearchResult]]:
    """Run the queries, rank what comes back, register it, return numbered results.

    Ranking before registration is the point: citation numbers are handed out in
    the order sources are registered, so ordering here is what puts the statute
    at [1] and the consultancy's summary of it at [7]: in the prompt, in the
    answer, and in the sidebar.
    """
    if not queries:
        return []
    # The standalone question, not the keyword queries, is what a result has to
    # be about. The queries are narrower than it.
    question = question or " ".join(queries)

    async with steps(_label(queries, ctx)) as step:
        results, own, report = await search_corpora(
            ctx, queries, limit_per_query=results_per_query, question=question
        )
        ctx.budget.searches += len(queries)
        registered, dropped = await register(ctx, results, question, own=own)
        step.text, step.detail = _searched_label(len(queries), len(registered), report, dropped)

    if not registered and not len(ctx.sources):
        # A source-bound run says this once, where it decides not to write.
        if not ctx.source_bound:
            await ctx.notice(_nothing_found(ctx, report))
    elif not registered:
        # A later search that added nothing is not a failed run. It happens
        # routinely to the follow-up round. The source list is capped, so a
        # second search that finds the same pages registers none of them, and
        # "Nothing was found in Web" under twenty sources is simply false.
        log.info("search added no new sources; %d already registered", len(ctx.sources))
    elif report.failures:
        await ctx.notice(
            f"{len(report.failures)} of {_plural(len(queries), 'search', 'searches')} failed; "
            "answering on the rest."
        )
    elif _mostly_refused(ctx, report) and len(ctx.sources) < _THIN_SOURCES:
        # The failure mode of a self-hosted instance without API keys: the free
        # engines suspend one by one under load and the last one standing
        # answers everything. On such an instance that is the normal state, so
        # it is only said when it shows: when the survivor found little. One
        # engine with twenty good results needs no apology, and the footer
        # names it either way.
        answered = ", ".join(report.engine_names) or "nothing"
        await ctx.notice(
            f"Few sources found, and only {answered} answered; "
            f"{_plural(len(report.refused), 'engine')} refused.",
            "info",
        )
    return registered


async def search_corpora(
    ctx: RunContext, queries: list[str], *, limit_per_query: int, question: str = ""
) -> tuple[list[SearchResult], list[SearchResult], SearchReport]:
    """Every search a run makes goes through here: the web corpora through
    SearXNG, and a literature project's papers through its library.

    Returns the web results, the library's pages, and the web's report. Kept
    apart because they are filtered differently (see `register`). In a
    source-bound run the web is not asked at all, not asked and discarded:
    a question typed into a thesis project is not sent to search engines
    unless the person switched the web on.
    """
    report = SearchReport()
    web_lenses = [lens for lens in ctx.lenses if lens.engines or lens.categories]
    own: list[SearchResult] = []

    async def from_library() -> list[SearchResult]:
        if ctx.library is None:
            return []
        try:
            return await ctx.library.search(
                [*queries, question] if question else queries,
                limit=limit_per_query,
                usage=ctx.embedding_usage,
            )
        except Exception as exc:  # the web half, if any, still answers
            log.warning("library search failed: %s", exc)
            await ctx.notice(f"{ctx.library.label} could not be searched: {_brief(exc)}")
            return []

    async def from_web() -> tuple[list[SearchResult], SearchReport]:
        if not web_lenses or ctx.source_bound:
            return [], SearchReport()
        return await search_all(
            queries, limit_per_query=limit_per_query, lenses=web_lenses, time_range=ctx.time_range
        )

    own, (results, report) = await asyncio.gather(from_library(), from_web())
    if ctx.library is not None:
        # Counted like an engine, so the step and the footer say the papers
        # were searched and what they returned. Without it a source-bound run
        # reads "answered without searching" over six cited pages.
        searched = SearchReport(queries=len(queries), lenses=[LIBRARY.id])
        searched.engines.update({ctx.library.label: len(own)})
        report.merge(searched)
    ctx.search_report.merge(report)
    if ctx.library is not None:
        for note in ctx.library.fresh_notes():
            await ctx.notice(note, "info")
    return results, own, report


def _mostly_refused(ctx: RunContext, report: SearchReport) -> bool:
    """True when the corpora searched have lost most of their engines."""
    named = {engine for lens in ctx.lenses for engine in lens.engines}
    refused = {name for name in report.refused if name in named}
    return len(report.engine_names) <= 1 and len(refused) >= 2


def _searched_label(
    queries: int, found: int, report: SearchReport, dropped: int = 0
) -> tuple[str, str]:
    """What the run actually did, and which engines did it.

    The user should never have to guess which search engine produced an answer,
    and the research block is where they are already looking. Discards are
    reported too, as the share kept: silently dropping half the results is the
    kind of judgement that should be visible when it goes wrong. The engines go
    on the quieter second line, so the first one reads as a sentence.
    """
    kept = _plural(found, "relevant result")
    if dropped:
        kept = f"{found} of {found + dropped} results relevant"
    text = f"{_plural(queries, 'search', 'searches')} · {kept}"
    parts = []
    engines = report.engine_names[:4]
    if engines:
        more = len(report.engine_names) - len(engines)
        listed = ", ".join(_engine(name) for name in engines)
        parts.append(f"{listed} +{more}" if more > 0 else listed)
    if report.time_range:
        parts.append(f"past {report.time_range}")
    return text, " · ".join(parts)


# SearXNG names its engines in lower case. Most read fine capitalised; these do not.
_ENGINE_NAMES = {
    "duckduckgo": "DuckDuckGo",
    "arxiv": "arXiv",
    "pubmed": "PubMed",
    "github": "GitHub",
    "stackoverflow": "Stack Overflow",
    "youtube": "YouTube",
    "openalex": "OpenAlex",
    "crossref": "Crossref",
}


def _engine(name: str) -> str:
    """An engine as a person would write it. Names that already carry capitals
    (the library's label) are left alone."""
    if name != name.lower():
        return name
    if name in _ENGINE_NAMES:
        return _ENGINE_NAMES[name]
    return " ".join(w.upper() if len(w) <= 3 else w.capitalize() for w in name.split())


def _nothing_found(ctx: RunContext, report: SearchReport) -> str:
    """Say which of the three ways this went wrong, because the fixes differ."""
    if report.failures and not report.engines:
        return (
            "Web search is not returning results. Check that SearXNG is running and "
            "that JSON output is enabled."
        )
    if report.unresponsive and not report.engines:
        refused = ", ".join(f"{name} ({reason})" for name, reason in report.unresponsive.items())
        return f"No search engine answered: {refused}."
    lenses = ", ".join(lens.label for lens in ctx.lenses) or "the selected sources"
    return f"Nothing was found in {lenses}. Widening the sources may help."


async def register(
    ctx: RunContext,
    results: list[SearchResult],
    question: str,
    *,
    own: list[SearchResult] | None = None,
) -> tuple[list[tuple[int, SearchResult]], int]:
    """Filter, order and number search results. Returns them and how many were
    dropped as off-topic.

    Shared by every path that searches, because the order of these three steps
    is the whole of the citation contract: relevance first, provenance second,
    and numbers last. A trusted source about a different subject is still about
    a different subject, and once it holds a citation number the answer is built
    on it.

    The project's own answers join whichever search reaches here first, so they
    are ranked against it rather than appended underneath; `take_project_sources`
    hands them out once per run.

    `own` are pages of the person's papers, and they skip the relevance filter
    the way the project's answers do: the library has already ranked them
    against the queries, with embeddings that read across languages, and the
    keyword filter here would drop an English paper for a German question.
    """
    relevant, dropped = keep_relevant(results, question)
    found = relevant + list(own or []) + ctx.take_project_sources()
    registered = await ctx.sources.add(order(found, prefer_recent=ctx.prefer_recent))
    return registered, dropped


async def fetch(ctx: RunContext, targets: list[SearchResult]) -> int:
    """Read full pages, count them against the budget, and republish the sidebar.

    The sidebar was written before these were read; refreshing it says which
    sources now rest on the page rather than on two lines an engine wrote about
    it. Returns how many pages were read.
    """
    if not targets:
        return 0
    fetched = await content_fetch.fetch_many(targets, limit=len(targets))
    ctx.budget.pages_fetched += fetched
    if fetched:
        await ctx.sources.refresh()
    return fetched


async def read_pages(
    ctx: RunContext, registered: list[tuple[int, SearchResult]], steps: Steps, *, limit: int
) -> None:
    """Fetch full text for the most promising results.

    Snippets are written to be skimmed, not reasoned over; this is what makes a
    balanced or deep answer differ from a quick one.
    """
    if limit <= 0 or not registered:
        return

    # `registered` arrives in rank order, so the first `limit` unread results are
    # the best-evidenced ones. Reading the top of an unranked list is how a run
    # spends its whole content budget on the first blog an engine happened to
    # return.
    targets = [result for _index, result in registered if not result.content][:limit]
    if not targets:
        return

    async with steps(f"Reading {_plural(len(targets), 'page')}") as step:
        fetched = await fetch(ctx, targets)
        step.text = (
            f"Read {_plural(fetched, 'page')}"
            if fetched == len(targets)
            else f"Read {fetched} of {_plural(len(targets), 'page')}"
        )
        if not fetched:
            step.text = "Could not read any pages; using search snippets"


async def read_cited(
    ctx: RunContext, question: str, passages: list[Passage], steps: Steps, *, limit: int
) -> list[Passage]:
    """Read the pages the answer is about to be written from, then re-rank.

    `read_pages` runs before the passages are chosen and reads the best-ranked
    results, which is the right order: at that point nothing better is known.
    But the passage set is the run's final answer to "what is this written
    from", and a source that reaches it carrying two lines an engine wrote about
    a page is a citation the reader cannot check against anything. The reader
    who follows it finds a page the run never opened.

    So the reading is topped up once the set is known: every source in it that
    is still a snippet gets fetched, and the passages are rebuilt against the
    pages. One pass, not a loop: re-ranking can pull in a source that is itself
    unread, and chasing that to a fixed point would spend the whole budget on
    the tail of the evidence.

    What cannot be read stays in, still labelled a snippet in the prompt and in
    the sidebar. Dropping it would be worse: a source the run could not open is
    weak evidence, and weak evidence that says so beats a shorter list that
    pretends the page was never found.
    """
    if limit <= 0 or not passages or not ctx.budget.can_research():
        return passages

    by_index = dict(ctx.sources.all())
    cited = sorted({p.source_index for p in passages if p.source_index in by_index})
    targets = [
        by_index[index]
        for index in sorted({p.source_index for p in passages if not p.read})
        if index in by_index and not by_index[index].content
    ][:limit]
    if not targets:
        return passages

    # Counted before the fetch, because `fetch_many` fills in `.content` and the
    # two groups become indistinguishable afterwards.
    already = sum(1 for index in cited if by_index[index].content)

    async with steps(f"Reading {_plural(len(targets), 'more page')} the answer will cite") as step:
        fetched = await fetch(ctx, targets)
        # Said as the top-up it is, then against the whole cited set. A bare
        # "Read 8 of 8" under "Read 5 pages" reads as a contradiction; only the
        # top-up reads as "the answer cites one page". Both together say it.
        read = already + fetched
        whole = (
            f"all {len(cited)} cited sources read in full"
            if read == len(cited)
            else f"{read} of {len(cited)} cited sources read in full"
        )
        step.text = (
            f"Read {_plural(fetched, 'more page')} · {whole}"
            if fetched
            else f"Could not open {_plural(len(targets), 'cited page')}; citing the snippet"
        )

    if not fetched:
        return passages
    return await rank(ctx, question, ctx.sources.all(), top_k=ctx.depth.passages)


# Pictures retrieved for a widget. Few are kept, because only one is shown and
# the rest exist so the model has something to choose between, but many are
# asked for, because the filter that matters (does this result actually carry a
# picture?) runs after the search, and asking for six leaves one to choose from.
IMAGE_CANDIDATES = 6
IMAGE_SEARCH_WIDTH = 24


async def search_images(
    ctx: RunContext, question: str, *, query: str = ""
) -> tuple[list[SearchResult], str]:
    """The picture search, as a bare network call. Never raises.

    Returns the candidates and, when there are none, why. "No usable picture was
    found" was true of an instance whose image engines had all been removed, of
    a query that matched nothing, and of results whose pictures were thrown away
    by a URL check: three different repairs behind one sentence.

    Separate from the evidence. Image results are pages built around
    a photograph (stock libraries, wallpaper sites), and as *text* they are
    worth nothing; letting them into the main search means the answer gets
    written from them. So they are searched for on their own and registered
    afterwards, at the tail: they receive citation numbers, because a picture
    must say where it came from, but they arrive too late to displace anything.

    Split from the registration below so the *call* can run beside the main
    search while the *numbering* still happens after it.
    """
    from app.search.lenses import LENSES

    if ctx.source_bound:
        return [], "Pictures come from the web, which is switched off for this project"
    lens = LENSES.get("images")
    if lens is None:
        return [], "No picture search is configured"

    try:
        # The keyword query, not the question. Image archives index captions:
        # Wikimedia Commons answers "flag of Argentina" and returns nothing at
        # all for "wie sieht die argentinische flagge aus", which is how a
        # picture search ends up choosing between stock libraries.
        results, report = await search_all(
            [query or question], limit_per_query=IMAGE_SEARCH_WIDTH, lenses=[lens]
        )
    except Exception as exc:  # a missing picture never fails an answer
        log.info("picture search failed: %s", exc)
        return [], f"The picture search failed: {_brief(exc)}"

    ctx.search_report.merge(report)
    ctx.budget.searches += 1
    if not results:
        refused = ", ".join(report.unresponsive) if report.unresponsive else ""
        return [], (
            f"No image engine answered ({refused})"
            if refused
            else "The image engines returned nothing for this"
        )

    with_images = [result for result in results if result.image]
    if not with_images:
        # Every result was a page *about* a picture with no picture on it. Worth
        # separating: it points at the engines or at the URL filter, never at
        # the question.
        return [], f"{_plural(len(results), 'image result')}, none carrying a usable picture"

    relevant, dropped = keep_relevant(with_images, question)
    if not relevant:
        return [], f"{_plural(dropped, 'picture')} found, all about something else"
    return order(relevant)[:IMAGE_CANDIDATES], ""


async def register_images(
    ctx: RunContext, found: tuple[list[SearchResult], str], steps: Steps
) -> int:
    """Give the retrieved pictures their citation numbers, after the evidence."""
    pictures, why = found
    async with steps("Looking for a picture") as step:
        if not pictures:
            step.text = why or "No usable picture was found"
            return 0
        ctx.sources.allow_extra(len(pictures))
        registered = await ctx.sources.add(pictures)
        ctx.pictures.extend(index for index, _result in registered)
        step.text = f"Found {_plural(len(registered), 'picture')}"
        return len(registered)


async def find_images(ctx: RunContext, question: str, steps: Steps, *, query: str = "") -> int:
    """Search for pictures and register them, in series. Used where there is no
    main search to run beside: the deep path has already finished searching by
    the time it knows the answer wants a picture."""
    return await register_images(ctx, await search_images(ctx, question, query=query), steps)


def _brief(exc: BaseException) -> str:
    return str(exc).strip() or type(exc).__name__


async def alongside(main, extra):
    """Run an optional second coroutine beside the first.

    `asyncio.gather` with a bare `None` in it is a TypeError, and threading that
    condition through the caller costs more lines than it saves. `extra` may be
    None; the second half of the result is then None too.
    """
    if extra is None:
        return await main, None
    return await asyncio.gather(main, extra)


# Below this many sources the lexical fallback in embed/rerank.py ranks as well
# as the embedding model does (there is simply not enough to reorder), and it
# costs no network round-trip. Skipping is what makes a simple answer cheap.
RERANK_MIN_SOURCES = 6


async def rank(
    ctx: RunContext, question: str, registered: list[tuple[int, SearchResult]], *, top_k: int
) -> list[Passage]:
    """Pick the passages that actually reach the model."""
    if not registered:
        return []
    # Except for the person's papers: a German question against English pages
    # shares no words with them, so "ranks as well lexically" is false there at
    # any count, and the passages to embed are a handful of one PDF's pages.
    papers = any(result.excerpts for _index, result in registered)
    model = ctx.embedding_model if papers or len(registered) > RERANK_MIN_SOURCES else None
    if model is None and ctx.embedding_model:
        log.info("skipping the reranker: %d sources rank as well lexically", len(registered))
    return await rank_passages(
        question,
        registered,
        top_k=top_k,
        model=model,
        provider=ctx.embedding_provider,
        usage=ctx.embedding_usage,
    )


async def write_answer(
    ctx: RunContext,
    messages: list[dict[str, Any]],
    *,
    max_tokens: int,
    hold: Awaitable[Any] | None = None,
) -> str:
    """Stream an answer into a text block. Returns the finished text.

    Reasoning tokens go to their own block: they are worth seeing, but splicing
    them into the prose would ruin the answer.

    `hold` is work that must land *above* the answer: the widget call. The
    request goes out immediately either way and only the first block waits, so
    a card that takes six seconds to build does not cost six seconds of the
    run: it is built while the writer is still thinking. Blocks are rendered in
    the order they were emitted, which is the whole reason for the wait.

    The wait is for the *emission*, never for the stream. Deltas that arrive
    while the card is still being built are kept here and replayed once it
    lands. Awaiting the card inside the loop instead deadlocked: the stream
    holds an LLM concurrency slot for as long as it is being read, the card
    needs a slot of its own, and with as many runs at this point as there are
    slots, every writer sat holding one while waiting on a card that could
    never get one.
    """
    stream = ctx.llm.stream(messages, max_tokens=max_tokens)
    ctx.budget.spend_llm()
    held = asyncio.ensure_future(hold) if hold is not None else None
    waiting: list[Delta] = []

    text: dict[str, Any] | None = None
    answer_buffer: Buffer | None = None
    thinking: str | None = None
    thinking_buffer: Buffer | None = None
    produced = False

    async def take(delta: Delta) -> None:
        nonlocal text, answer_buffer, thinking, thinking_buffer, produced
        # The text block before anything else, including the reasoning block,
        # so the answer's own blocks keep their order below the card.
        if answer_buffer is None:
            text = blocks.text_block()
            await ctx.stream.emit_block(text)
            answer_buffer = Buffer(ctx, text["id"], blocks.append_text)
        if delta.reasoning:
            if thinking_buffer is None:
                block = blocks.thinking_block()
                thinking = block["id"]
                await ctx.stream.emit_block(block)
                # Same `/data/content` shape as a text block, different renderer.
                thinking_buffer = Buffer(ctx, thinking, blocks.append_text)
            await thinking_buffer.add(delta.reasoning)
        if delta.content:
            produced = True
            await answer_buffer.add(delta.content)

    async def release() -> None:
        """The card has landed: everything kept back may now be emitted."""
        nonlocal held
        if held is not None:
            await held
            held = None
        while waiting:
            await take(waiting.pop(0))

    try:
        async for delta in stream:
            if held is not None and not held.done():
                waiting.append(delta)
                continue
            await release()
            await take(delta)
        # The stream has ended and released its slot, so waiting out a card that
        # is still being built cannot starve it.
        await release()
    finally:
        # A stream that broke mid-way still shows what it had written, as long
        # as nothing is left to wait for.
        if waiting and (held is None or held.done()):
            try:
                await release()
            except Exception as exc:  # the error already unwinding says more
                log.info("could not emit the held-back answer: %s", exc)
        if answer_buffer is not None:
            await answer_buffer.flush()
        if thinking_buffer is not None:
            await thinking_buffer.flush()
            await ctx.stream.emit_patch(thinking, [blocks.set_status(blocks.STATUS_DONE)])

    if not produced:
        raise LLMError("The model returned an empty answer")

    await _finish_truncated(ctx, messages, answer_buffer)
    return answer_buffer.text if answer_buffer else ""


# One continuation, never two. A model that fills a second ceiling without
# reaching a conclusion is not going to reach one on a third, and each attempt
# is a full prompt of evidence paid for again.
CONTINUE_ATTEMPTS = 1


async def _finish_truncated(
    ctx: RunContext, messages: list[dict[str, Any]], buffer: Buffer | None
) -> None:
    """Carry on writing when the answer stopped because it ran out of room.

    `finish_reason: length` is the provider saying "there was more". Left
    alone it reaches the reader as a sentence that stops mid-word, followed by
    a Copy button and a footer, which reads as a finished answer and is the
    one failure mode a research tool cannot have. Nothing here is guesswork:
    the continuation is asked for only when the provider said the ceiling was
    hit, and it appends to the same block, so the reader sees one answer.
    """
    if buffer is None:
        return

    for _ in range(CONTINUE_ATTEMPTS):
        if getattr(ctx.llm, "last_finish_reason", "") != "length":
            return
        if not ctx.budget.can_continue():
            break
        log.info("the answer hit its length limit; continuing it")
        ctx.budget.spend_llm()
        try:
            stream = ctx.llm.stream(
                prompts.continuation(messages, buffer.text),
                max_tokens=ctx.depth.answer_tokens,
            )
            async for delta in stream:
                if delta.content:
                    await buffer.add(delta.content)
        except LLMError as exc:
            # The answer that exists is still an answer; say what happened.
            log.info("could not continue the answer: %s", exc)
            break
        finally:
            await buffer.flush()

    if getattr(ctx.llm, "last_finish_reason", "") == "length":
        await ctx.notice(
            "The answer reached its length limit and is cut short. Ask a narrower "
            "question, or raise the depth preset's answer budget.",
            "warn",
        )


class Buffer:
    """Coalesces token deltas into patches at a readable cadence."""

    def __init__(self, ctx: RunContext, block_id: str, op) -> None:
        self._ctx = ctx
        self._block_id = block_id
        self._op = op
        self._pending = ""
        self._last_flush = time.monotonic()
        self.text = ""

    async def add(self, delta: str) -> None:
        self._pending += delta
        self.text += delta
        if (
            len(self._pending) >= _FLUSH_CHARS
            or time.monotonic() - self._last_flush >= _FLUSH_SECONDS
        ):
            await self.flush()

    async def flush(self) -> None:
        if not self._pending:
            return
        await self._ctx.stream.emit_patch(self._block_id, [self._op(self._pending)])
        self._pending = ""
        self._last_flush = time.monotonic()


def _label(queries: list[str], ctx: RunContext) -> str:
    """Show the actual queries and the corpora: the cheapest way to make the run
    legible while it is still running."""
    where = " + ".join(lens.label for lens in ctx.lenses) or "the web"
    shown = " · ".join(q for q in queries[:3])
    return f"Searching {where}: {shown}" if shown else f"Searching {where}"


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"
