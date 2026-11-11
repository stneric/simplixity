"""Run lifecycle: understand the question, research it, write the answer.

One entry point, four shapes behind it: fetch it, answer directly, search then
answer, or plan-research-report. Which one runs is decided by `agent/route.py`
and expressed as a budget, so the control flow here stays flat enough to read in
one screen.

Two ordering rules do most of the work here.

**The research block is emitted before the classifier call.** The fork awaited a
full non-streaming structured call before the first byte reached the client, and
that wait was most of its perceived latency. Emit the shell first, patch it
after.

**A lookup is tried before a search, not after it.** Searched like any other
question, a forecast costs a classify call, two searches, five page reads, an
embedding call and a widget call, and then arrives from a structured service in
a third of a second, at the end. Anything a service can answer outright is
fetched first; the searched path is what happens when no service answers.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from app.agent import blocks, deep, linked, prompts, research, shapes, verify, widgets
from app.agent import depth as depth_module
from app.agent import route as route_module
from app.agent.budget import Budget, Cancelled
from app.agent.context import RunContext, SourceRegistry, Steps, ellipsis
from app.agent.route import Route
from app.config import settings
from app.embed.rerank import passages_from
from app.llm import LLM, LLMAuthError, LLMConfigError, LLMError
from app.providers import catalog
from app.search import lenses as lens_module
from app.search import links
from app.search import weather as weather_source
from app.search.base import LIBRARY_PREFIX
from app.search.searxng import HARD_WINDOWS, TIME_RANGES
from app.store.stream import RunStream

log = logging.getLogger(__name__)

Message = dict[str, Any]

# The classifier answers a handful of short fields at once and needs little room.
# Without a ceiling OpenRouter reserves the model's whole output window against
# the account balance and refuses the call outright; see `LLM.structured`.
CLASSIFY_TOKENS = 500

# How many follow-up queries the second look may ask for. One, because the
# point is the missing piece: a list of three is the plan the Deep preset
# exists for, at the price of the everyday one.
FOLLOW_UPS = 1


class Classification(BaseModel):
    # In the order the model answers them. Strict JSON mode fills properties in
    # schema order, so a field is decided with only the ones above it in view.
    # The shape and the picture come before `needs_search` because they are
    # what makes a search necessary: decided after it, "wie sieht ein Okapi aus"
    # had already been answered from memory by the time the model got to
    # noticing the reader wanted to see one.
    standalone_question: str = ""
    # "" or one of `shapes.SHAPES`. How the answer should be laid out, see
    # agent/shapes.py. A price written as prose is a number the reader has to
    # dig out of a paragraph.
    answer_shape: str = ""
    # The bare place name, when the shape is "weather". SearXNG's weather
    # engines hand the query straight to a geocoder, which resolves "Berlin" and
    # raises on "weather in Berlin", so the place has to arrive on its own.
    place: str = ""
    # Whether the reader wants to *see* the thing. A picture can only be shown
    # if a search returned one, so this decides whether the separate image
    # search runs at all.
    wants_image: bool = False
    # What to search the image archives for: the subject as a caption names it,
    # in English. The first web query is the fallback, and it is written for a
    # different index: field terminology for Scholar, the reader's language for
    # the web. Wikimedia Commons answers "okapi" and not either of those.
    image_query: str = ""
    needs_search: bool = True
    search_queries: list[str] = Field(default_factory=list)
    # "" | day | week | month | year. Set when the answer depends on the current
    # state of the world: a page from 2019 is not evidence about this year, and
    # the whole point of a research tool is not to answer as if it were.
    recency: str = ""
    # simple | standard | research. How much work the question is worth, which
    # is what "Auto" depth resolves against. Two more fields on a call that was
    # happening anyway. The same trade that bought `recency`.
    complexity: str = "standard"
    # Which corpora to search. Asked for, and honoured, only when the reader
    # chose "Auto" in the source picker. An explicit selection is a decision and
    # is never overridden by this call.
    lenses: list[str] = Field(default_factory=list)
    # The parts of a prose answer that are structure rather than argument, a
    # timeline, pros and cons, key facts, see `shapes.FACET_LIST`. Only read
    # when `answer_shape` is empty; `route.plan` decides whether they turn the
    # answer into an overview.
    facets: list[str] = Field(default_factory=list)
    # Literature projects only: the request is about the collection as a whole
    # (an introduction, an overview), so every paper's abstract is handed over
    # beside what the search finds. See `_library_overview`.
    whole_library: bool = False


@dataclass(slots=True)
class RunRequest:
    """Everything a run needs, resolved before it starts."""

    query: str
    chat_id: str
    model: str
    provider: str
    depth: str
    # Which corpora to search. Resolved from the request or the saved settings,
    # never left implicit: a scientific question searched against the general
    # web is the difference between a study and a supplement shop.
    lenses: list[str] = field(default_factory=list)
    embedding_model: str | None = None
    embedding_provider: str = "openrouter"
    # Optional small model for triage. Empty means the chat model does it.
    router_model: str | None = None
    router_provider: str = "openrouter"
    # Optional model for the citation check. Empty keeps `RunContext.checker`'s rule.
    judge_model: str | None = None
    judge_provider: str = "openrouter"
    # Where "what is the weather" means, when the question does not say.
    home_location: str = ""
    history: list[Message] = field(default_factory=list)
    # Answers this project has already produced, retrieved before the run
    # starts (see `runs.start`). Handed over as results rather than as a
    # database handle: the agent has never known what SQLite is, and a project
    # is not a reason to teach it.
    project_sources: list[Any] = field(default_factory=list)
    # A literature project's papers (`search.library.Library`), or None, and
    # whether the person allowed the web beside them. See `RunContext.library`.
    library: Any = None
    web: bool = True
    # The pictures the question is about, as content parts (`store.images`),
    # and whether they came with an earlier question. Only ever set for a
    # model that can see, or one the catalogue says nothing about.
    images: list[dict[str, Any]] = field(default_factory=list)
    images_earlier: bool = False


def _lenses_for(request: RunRequest) -> list:
    """The corpora a run starts with. A literature project searches its papers
    first, and the web corpora only when the web is switched on."""
    lenses = lens_module.resolve(request.lenses)
    if request.library is None:
        return lenses
    return [lens_module.LIBRARY, *(lenses if request.web else [])]


async def run(stream: RunStream, request: RunRequest) -> None:
    """Execute one research run. Terminal events are the runner's job except
    for the happy path, which knows the stats worth reporting."""
    requested = depth_module.get(request.depth)
    # The budget exists before the router does (a search can return before the
    # classifier answers), so it starts on a concrete preset and is re-capped by
    # `_apply` the moment the route is known. AUTO never reaches it.
    depth = depth_module.resolve(requested)
    ctx = RunContext(
        stream=stream,
        llm=LLM(request.model, request.provider),
        router=LLM(request.router_model, request.router_provider) if request.router_model else None,
        judge=LLM(request.judge_model, request.judge_provider) if request.judge_model else None,
        depth=depth,
        requested_depth=requested,
        budget=Budget(depth),
        sources=SourceRegistry(stream, depth.max_sources),
        query=request.query,
        # On "Auto" this is the fallback the run searches with if the classifier
        # never answers; the real choice is made in `_classify`.
        lenses=_lenses_for(request),
        auto_lenses=lens_module.wants_auto(request.lenses)
        and (request.library is None or request.web),
        history=prompts.trim_history(request.history),
        embedding_model=request.embedding_model,
        embedding_provider=request.embedding_provider,
        home_location=request.home_location,
        project_sources=list(request.project_sources),
        library=request.library,
        web=request.web,
        images=list(request.images),
        images_earlier=request.images_earlier,
    )

    await ctx.phase(blocks.PHASE_UNDERSTANDING)
    shell = blocks.research_block("Understanding the question")
    await stream.emit_block(shell)
    steps = Steps(stream, shell["id"])

    try:
        await _execute(ctx, steps)
    except Cancelled as exc:
        await steps.finish(blocks.STATUS_FAILED)
        await _emit_provenance(ctx)
        await stream.emit_done("cancelled", str(exc))
        return
    except LLMError as exc:
        # Provider problems are the common failure and deserve their own
        # message rather than a stack-trace-shaped one from the supervisor.
        await steps.finish(blocks.STATUS_FAILED)
        await _emit_provenance(ctx)
        await stream.emit_done("error", str(exc), stats=ctx.budget.summary())
        return

    await _emit_provenance(ctx)
    await stream.emit_done("completed", stats=ctx.budget.summary())


async def _emit_provenance(ctx: RunContext) -> None:
    """Close every run with how it was produced.

    Emitted on the error and cancellation paths too: a run that failed after
    searching four engines and reading nothing is precisely the case where the
    reader needs to see what happened, and it is the case a "stats" field on the
    terminal event would lose the moment the chat is reloaded.
    """
    try:
        await ctx.stream.emit_block(blocks.provenance_block(ctx.provenance()))
    except Exception as exc:  # never let bookkeeping bury a finished answer
        log.warning("could not emit the provenance footer: %s", exc)


async def _execute(ctx: RunContext, steps: Steps) -> None:
    # A link the reader typed is fetched through the web, so a project with the
    # web off leaves it unread like everything else outside the papers.
    urls = [] if ctx.source_bound else links.extract(ctx.query)
    # The pre-router first, because it costs nothing. It only answers when it is
    # certain, and None (ask the model) is its ordinary answer. Not with a link
    # in the question: "thanks https://…" is not small talk.
    # Nor with a picture: "?" under a photo is a question about the photo.
    shortcut = None if urls or ctx.images else route_module.triage(ctx.query)
    declined = ""
    if shortcut is not None and ctx.source_bound and shortcut.mode == route_module.LOOKUP:
        # A forecast is fetched from a web service, and this project has the web
        # off. Asked anyway, it goes to the papers like any other question.
        shortcut = None
    if shortcut is not None:
        await steps.note(shortcut.reason)
        if await _take(ctx, shortcut, ctx.query, steps):
            return
        # Which place the service has already turned down, so the classified
        # path below does not ask it the same question a second time.
        declined = shortcut.place or ctx.home_location

    if ctx.library is not None:
        try:
            ctx.catalog = await ctx.library.catalog()
        except Exception as exc:  # the queries are only less well aimed without it
            log.info("could not list the library: %s", exc)
    pinned = await linked.read(ctx, urls, steps)
    classification = await _classify(ctx, steps, pinned)
    question = classification.standalone_question.strip() or ctx.query
    route = route_module.plan(classification, ctx.asked_depth)

    # A widget is made of retrieved material (a picture the search returned, a
    # price a source stated), so choosing one is itself a statement that the
    # question needs a search. The two fields are answered in the same breath and
    # models do sometimes contradict themselves across them; the shape is the
    # more specific claim, so it wins.
    if route.shape in shapes.WIDGET_SHAPES:
        classification.needs_search = True
        if route.mode == route_module.DIRECT:
            route = route_module.widened(route, ctx.asked_depth)

    if ctx.library is not None:
        route = _bound_to_library(ctx, route)

    # A question about the linked page needs no search, but it is not the model
    # alone either: it is answered from the page. A link that could not be read
    # turns the same question into a search, the only way left to answer it.
    search = True
    if urls and route.mode == route_module.DIRECT:
        route = linked.route(route, ctx.asked_depth, read=bool(pinned))
        search = not pinned
    if pinned and not classification.wants_image:
        # "Was ist <link>" reads as a profile, and a profile asks for a picture.
        # The page is the subject here, and an image search finds pictures of
        # whatever its words also name, so none is searched for or shown unless
        # the reader asked to see something.
        route = dataclasses.replace(route, wants_images=False)
        ctx.stray_pictures = False

    if route.mode == route_module.LOOKUP:
        # Compared folded, because the two names come from different places: the
        # pre-router lifts "reutlingen" out of the raw question, the classifier
        # writes "Reutlingen". Matching them exactly meant the service that had
        # just declined was asked the identical question again, and the reader
        # watched "No weather service answered" scroll past twice.
        asking_again = bool(declined) and _same_place(route.place or ctx.home_location, declined)
        if not asking_again and await _take(ctx, route, question, steps):
            return
        # No service answered. The cheap preset was justified by the lookup
        # succeeding, so the fallback goes back to the full reading budget
        # rather than searching a forecast on one page of snippets.
        route = route_module.widened(route, ctx.asked_depth)

    _apply(ctx, route)

    if route.mode == route_module.DIRECT:
        await _answer_directly(ctx, question, steps)
        return

    # Two different things, and conflating them is what put a hard one-year
    # filter on ordinary web searches. `prefer_recent` is "this question wants
    # current sources" and belongs to the ranking; `time_range` deletes
    # everything older at the engine and is reserved for the windows narrow
    # enough to earn it (`searxng.HARD_WINDOWS`).
    recency = classification.recency if classification.recency in TIME_RANGES else ""
    ctx.prefer_recent = bool(recency)
    ctx.time_range = recency if recency in HARD_WINDOWS else ""

    # Trimmed again, against the preset the router actually chose. The
    # classifier was told the provisional preset's ceiling (it has to run
    # before there is a route), so on "Auto" it may well have written three
    # queries for a question that turned out to need one.
    queries = classification.search_queries[: ctx.depth.queries]
    overview = []
    findings = ""
    unanswered: list[str] = []
    passages: list = []
    if not search:
        await steps.retitle(f"Reading “{ellipsis(pinned.pages[0][1])}”")
    else:
        await steps.retitle(f"Researching “{ellipsis(question)}”")
        _widen_for(ctx, route.shape)
        if ctx.library is not None:
            # A short attachment is read whole whatever was asked: the search
            # could only hand over less of it. Abstracts only when the request
            # is about the library as a whole, as before.
            whole = classification.whole_library
            if whole or await _fits_whole(ctx):
                overview = await _library_overview(ctx, steps, papers=whole)
        if ctx.depth.sub_questions:
            found = await deep.research_deeply(ctx, question, steps)
            passages, findings, unanswered = found.passages, found.findings, found.unanswered
            if route.wants_images and ctx.budget.can_research():
                await research.find_images(ctx, question, steps, query=_image_query(route, queries))
        else:
            passages = await _research_directly(ctx, question, queries, steps, route)

    # Last stop before the prompt is built: whatever the answer is about to be
    # written from should be a page the run actually opened, not two lines an
    # engine wrote about one. Both paths go through it, because both can end on
    # a snippet. Deep reaches this with sources from five sub-questions.
    await ctx.guard()
    passages = await research.read_cited(
        ctx, question, passages, steps, limit=ctx.depth.fetch_pages
    )
    lead = pinned.passages + overview
    if lead:
        # First, so a window too small for everything keeps the linked page and
        # the overview the request was about, and once: the ranking may have
        # picked some of their passages too.
        shown = {(p.source_index, p.text) for p in lead}
        passages = lead + [p for p in passages if (p.source_index, p.text) not in shown]

    def build(kept: list, compact: bool) -> list[dict[str, Any]]:
        return (
            prompts.report(
                question,
                kept,
                findings,
                ctx.history,
                route.shape,
                facets=route.facets,
                unanswered=unanswered,
                compact=compact,
                library=ctx.library is not None,
                linked=pinned.indexes,
            )
            if ctx.depth.sub_questions
            else prompts.answer(
                question,
                kept,
                ctx.history,
                ctx.depth,
                route.shape,
                facets=route.facets,
                compact=compact,
                library=ctx.library is not None,
                linked=pinned.indexes,
            )
        )

    window = await catalog.window_of(ctx.llm)
    passages, dropped, answer_tokens, compact = _fit_to_window(ctx, passages, build, window=window)
    if dropped:
        # Named, because the alternative is an answer that is quietly thinner
        # than the research behind it and says nothing about why.
        await ctx.notice(
            f"{ctx.llm.model} reads {window} tokens, so the {dropped} lowest-ranked of "
            f"{dropped + len(passages)} passages were left out of its prompt. A larger "
            "window would use them.",
        )
    messages = build(passages, compact)
    if ctx.library is not None:
        await _mark_passages(ctx, passages)

    await ctx.guard()

    # A shape with a picture of its own (a profile, an image answer) chooses it
    # on its card. Everything else that asked for one shows it here, above the
    # card and the prose, and without a model call.
    if route.wants_images and not shapes.wants_image(route.shape):
        await widgets.show_picture(ctx)
    widget = await _start_card(ctx, route.shape, question, passages)

    await steps.finish()

    if not passages and ctx.library is not None:
        # The one place a literature project differs from every other chat in
        # outcome rather than in where it looks. Elsewhere an unsourced answer,
        # labelled as one, beats a blank page; here the person asked for answers
        # from their papers, and the model's own knowledge written up in their
        # thesis voice is precisely what they must not get.
        await ctx.notice(ctx.library.nothing_found, "warn")
        return

    if not passages:
        # Better an answer labelled as unsourced than a blank page.
        await ctx.notice(
            "No usable sources were found, so this answer comes from the model alone.", "warn"
        )
        messages = prompts.direct(question, ctx.history)

    await ctx.phase(blocks.PHASE_WRITING)
    try:
        answer = await research.write_answer(ctx, messages, max_tokens=answer_tokens, hold=widget)
    finally:
        # An answer that produced nothing never reached the point where it waits
        # for the card, and a failed one may not have either. Either way the
        # task is not left running behind a finished run.
        if widget is not None and not widget.done():
            await widget

    # Everything above judged the evidence; this is the only step that judges
    # the answer. It runs last because the prose is already streamed and
    # readable, so the check costs the reader nothing and can never delay or
    # break the thing it is checking.
    if ctx.depth.verify and passages:
        await _verify(ctx, question, answer, passages)


async def _verify(ctx: RunContext, question: str, answer: str, passages: list) -> None:
    """`verify.check` with a blast radius of zero.

    The answer is on the reader's screen by the time this runs, so there is no
    failure here worth turning into one they can see. A check that cannot run
    leaves the run exactly as good as it was before the check existed.
    """
    try:
        await verify.check(ctx, question, answer, passages)
    except Cancelled:
        raise
    except Exception as exc:  # the answer is already written and on screen
        log.info("citation check failed and was dropped: %s", exc)


# Below this a local window cannot carry the full ruleset and the evidence both,
# so the writer is given the compact one. Above it, a local model is treated
# exactly like a remote one.
COMPACT_BELOW_TOKENS = 16_000
# Slack for the estimate itself: the chars-per-token ratio is crude, the chat
# template adds tokens this cannot see, and overrunning the window costs the
# *sources*, which is the one thing this whole arrangement is protecting.
WINDOW_SAFETY_TOKENS = 256


def _fit_to_window(
    ctx: RunContext, passages: list, build: Any, window: int | None = None
) -> tuple[list, int, int, bool]:
    """Fit the writer's prompt to what the model will actually accept.

    Returns the passages that fit, how many did not, the tokens left for the
    answer, and whether to use the compact ruleset.

    Only local runtimes are budgeted: a remote model's window is far larger than
    anything this app assembles, and guessing at it would trim good evidence for
    no reason. A local one is served with whatever window its runtime gives
    that model (`catalog.window_of`, by default Ollama's 4096), and truncates
    silently past it, so the choice is not "trim or don't" but "trim here, by
    rank, with the numbering intact, or let the runtime cut the middle out of
    the prompt". Without `window`, `local_context_tokens` stands in for it.
    """
    if window is None:
        window = settings.local_context_tokens if ctx.llm.is_local else 0
    if window <= 0:
        return passages, 0, ctx.depth.answer_tokens, False

    compact = window < COMPACT_BELOW_TOKENS
    # The answer shares the window with its own prompt, so a deep preset's 4000
    # tokens of report cannot be honoured in a 4096 window; a third is the most
    # that leaves room for evidence worth writing from.
    answer_tokens = min(ctx.depth.answer_tokens, max(400, window // 3))

    # Measured rather than assumed: build the same prompt with no evidence in it
    # and the remainder is the rules, the history and the question.
    empty = build([], compact)
    overhead = sum(len(m["content"]) for m in empty) // prompts.CHARS_PER_TOKEN
    room = window - overhead - answer_tokens - WINDOW_SAFETY_TOKENS
    if room <= 0:
        log.warning("no room for evidence in a %d-token window", window)
        return passages[:1], max(0, len(passages) - 1), answer_tokens, compact

    kept, dropped = prompts.fit_passages(passages, max_chars=room * prompts.CHARS_PER_TOKEN)
    if dropped:
        log.info(
            "context fit: %d of %d passages kept for a %d-token window",
            len(kept),
            len(passages),
            window,
        )
    return kept, dropped, answer_tokens, compact


async def _start_card(
    ctx: RunContext, shape: str, question: str, passages: list
) -> asyncio.Task[bool] | None:
    """Start the card this shape asks for beside the writer, or None.

    Above the prose, but not *before* it: the card is a second model call, and
    running it in series puts its whole latency between the reader and the
    first word (on a reasoning model, fifteen seconds of a thirty-five second
    answer). It runs beside the writer and only the first block waits
    for it, so the card still lands above the paragraph it belongs to while
    costing almost nothing in wall clock.

    Which card, and whether its absence is announced, is the registry's call
    (`shapes.card_for`): a borrowed card is refused silently, a card that *is*
    the answer says why it is missing. Weather reaching here means the lookup
    did not answer, so the model-filled schema is the fallback it always was.
    """
    plan = shapes.card_for(shape)
    # Every card is built from retrieved material; a picture is the one that is
    # chosen from the image search rather than from the evidence.
    if plan is None or not (passages or shape == shapes.IMAGE):
        return None
    card, promised = plan
    if not ctx.budget.can_research():
        # Silently dropping the card is how a price question ends up as a
        # paragraph that never states the price, so it is at least in the log.
        log.info("no budget left for the %s card: %s", card, ctx.budget.summary())
        return None
    await ctx.phase(blocks.PHASE_COMPOSING)
    return asyncio.create_task(_build_widget(ctx, card, question, passages, announce=promised))


async def _build_widget(
    ctx: RunContext, shape: str, question: str, passages: list, *, announce: bool = True
) -> bool:
    """`widgets.emit` as a task: a widget may cost itself, never the answer."""
    try:
        return await widgets.emit(ctx, shape, question, passages, announce=announce)
    except Cancelled:
        raise
    except Exception as exc:
        log.info("widget %s failed and was dropped: %s", shape, exc)
        return False


def _same_place(one: str, other: str) -> bool:
    """Whether two place names are the same question for a geocoder."""
    return (
        " ".join(str(one or "").split()).casefold() == " ".join(str(other or "").split()).casefold()
    )


def _apply(ctx: RunContext, route: Route) -> None:
    """Adopt a route's budget. Idempotent: every path through `_execute` runs
    it exactly once before spending anything against the new preset."""
    ctx.depth = route.depth
    ctx.budget.depth = route.depth
    ctx.sources.set_limit(route.depth.max_sources)
    ctx.shape = route.shape
    ctx.facets = route.facets
    ctx.mode = route.mode
    ctx.route_reason = route.reason


async def _take(ctx: RunContext, route: Route, question: str, steps: Steps) -> bool:
    """Answer from the route alone, if it can. False means fall through.

    Falling through is a normal outcome and deliberately leaves nothing behind:
    the depth is adopted only once the shortcut has actually worked, so a failed
    lookup does not leave the searched path on a one-query budget.
    """
    if route.mode == route_module.DIRECT:
        _apply(ctx, route)
        await _answer_directly(ctx, question, steps)
        return True

    if route.mode == route_module.LOOKUP:
        return await _answer_from_lookup(ctx, route, question, steps)
    return False


async def _answer_directly(ctx: RunContext, question: str, steps: Steps) -> None:
    """Write from the model alone. The route has already been adopted."""
    await steps.retitle("Answering from the model’s own knowledge")
    await steps.finish()
    await ctx.phase(blocks.PHASE_WRITING)
    await research.write_answer(
        ctx, prompts.direct(question, ctx.history), max_tokens=ctx.depth.answer_tokens
    )


async def _answer_from_lookup(ctx: RunContext, route: Route, question: str, steps: Steps) -> bool:
    """Fetch the conditions and write around them. Returns whether it worked.

    Weather is fetched, not extracted. SearXNG's own engines return a structured
    reading (temperature, unit, condition, hourly forecast), so putting a model
    between that and the screen could only introduce errors. What makes it fast
    is doing it *first*: everything the searched path would have cost is simply
    not spent.
    """
    place = route.place or ctx.home_location
    if not place:
        # Honest failure, not a guess. Geolocation is deliberately excluded by
        # the CSP and the permissions policy, so there is nowhere else to look.
        await ctx.notice(
            "The question names no place and no home location is set in Settings, "
            "so the conditions are searched for instead.",
            "info",
        )
        return False

    async with steps(f"Looking up the weather for {place}") as step:
        report = await weather_source.lookup(place)
        if report is None:
            step.text = "No weather service answered; reading the forecast pages instead"
            return False
        step.text = f"{report.location or place} · {report.engine}"

    _apply(ctx, route)
    payload = report.as_dict()
    await ctx.stream.emit_block(blocks.block(route.shape, payload))
    await steps.finish()

    await ctx.guard()
    await ctx.phase(blocks.PHASE_WRITING)
    await research.write_answer(
        ctx, prompts.lookup_answer(question, payload), max_tokens=ctx.depth.answer_tokens
    )
    return True


# A literature-project answer's floor, in tokens. See `_bound_to_library`.
LIBRARY_ANSWER_TOKENS = 4000


def _bound_to_library(ctx: RunContext, route: Route) -> Route:
    """A literature project's route: always a search, never the model alone.

    "Explain self-attention" is a question the classifier rightly says needs no
    search, and in an ordinary chat the model's own explanation is the answer.
    In a thesis project it is not: the person asked for what their papers say,
    and an uncited paragraph in fluent academic prose is the one answer that
    looks like it came from them while it did not. Lookups and pictures come
    from the web, so they go when the web is off.
    """
    requested = ctx.asked_depth
    # Passage citations carry the cited words inside the answer, so the same
    # answer costs half as many tokens again. At the ordinary budget a long
    # answer stopped mid-sentence, continuation and all.
    if route.depth.answer_tokens < LIBRARY_ANSWER_TOKENS:
        route = dataclasses.replace(
            route, depth=dataclasses.replace(route.depth, answer_tokens=LIBRARY_ANSWER_TOKENS)
        )
    if route.mode == route_module.DIRECT or (
        ctx.source_bound and route.mode == route_module.LOOKUP
    ):
        widened = route_module.widened(route, requested)
        route = dataclasses.replace(
            widened,
            reason=f"Answered from {ctx.library.label.lower()} · {widened.depth.label}",
            depth=dataclasses.replace(
                widened.depth,
                answer_tokens=max(widened.depth.answer_tokens, LIBRARY_ANSWER_TOKENS),
            ),
        )
    if ctx.source_bound:
        route = dataclasses.replace(route, wants_images=False)
    return route


def _widen_for(ctx: RunContext, shape: str) -> None:
    """Add the corpora this answer shape needs, keeping the user's selection.

    Additive, never replacing: the user picked those lenses, and a
    price question asked against Science should still search Science. It just
    also needs somewhere that actually holds a price.
    """
    if ctx.source_bound:
        return
    extra = [lens for lens in shapes.lenses_for(shape) if lens not in ctx.lens_ids]
    if not extra:
        return
    ctx.lenses = _keep_library(ctx, lens_module.resolve(ctx.lens_ids + extra))
    log.info("shape %r widened the search to %s", shape, ctx.lens_ids)


async def _research_directly(
    ctx: RunContext, question: str, queries: list[str], steps: Steps, route: Route
) -> list:
    """Quick and balanced: search, optionally read, rank. No planning phase:
    at these budgets the planning call costs more than it saves.

    When the answer wants a picture, that search runs *beside* the main one
    rather than after it. It stays a separate search for the reason it always
    has. Image results are pages built around a photograph and are worth
    nothing as text evidence, but there is no reason for the reader to wait for
    it in series, and its results are still registered afterwards, so they can
    never displace a source the answer rests on.
    """
    await ctx.phase(blocks.PHASE_RESEARCHING)
    pictures = (
        research.search_images(ctx, question, query=_image_query(route, queries))
        if route.wants_images
        else None
    )
    registered, found = await research.alongside(
        research.search_and_register(
            ctx,
            queries or [question],
            steps,
            results_per_query=ctx.depth.results_per_query,
            question=question,
        ),
        pictures,
    )
    if found is not None:
        await research.register_images(ctx, found, steps)
    if not registered:
        return []

    await ctx.guard()
    if ctx.depth.fetch_pages:
        await ctx.phase(blocks.PHASE_READING)
        await research.read_pages(ctx, registered, steps, limit=ctx.depth.fetch_pages)

    await ctx.guard()
    passages = await research.rank(ctx, question, registered, top_k=ctx.depth.passages)
    return await _second_look(ctx, question, passages, steps)


async def _second_look(ctx: RunContext, question: str, passages: list, steps: Steps) -> list:
    """One follow-up search, written against what the first one actually found.

    Every query in a run is composed before a single source has been seen, so a
    run can only look for what the model could name up front. Ask what
    DiCaprio's parents did and it searches his name, reads what came back, and
    reports that the sources do not say. This is the one step that gets to
    write a query knowing what is already on the table, and it is also the only
    place that can notice the question was half-answered.

    Cheap by construction: one structured call on the router, and a search only
    when that call says something is missing. Skipped entirely when the budget
    is thin: a follow-up that leaves nothing to write the answer with is worse
    than the gap it was closing.
    """
    if not ctx.depth.gap_check or not passages or not ctx.budget.can_research():
        return passages

    async with steps("Checking the evidence against the question") as step:
        try:
            ctx.budget.spend_llm()
            check = await (ctx.router or ctx.llm).structured(
                prompts.gaps(question, prompts.format_evidence(passages), FOLLOW_UPS),
                deep.GapCheck,
                max_tokens=CLASSIFY_TOKENS,
            )
        except LLMError as exc:
            log.info("second look failed, answering with what was found: %s", exc)
            step.text = "Wrote the answer from the first search"
            return passages

        follow_ups = [sub for sub in check.follow_up_questions if sub.question or sub.query][
            :FOLLOW_UPS
        ]
        if check.sufficient or not follow_ups:
            step.text = "Checked for gaps: the sources cover the question"
            return passages
        missing = follow_ups[0].question or follow_ups[0].query
        step.text = f"Missing: {ellipsis(missing, 60)}. Searching again"

    await ctx.guard()
    if not ctx.budget.can_research():
        return passages
    # The gap check now writes the search terms itself, so the follow-up is
    # searched with the words the model chose rather than with its own sentence.
    more = await research.search_and_register(
        ctx,
        [sub.search_query for sub in follow_ups],
        steps,
        results_per_query=ctx.depth.results_per_query,
        question=question,
    )
    if not more:
        return passages
    if ctx.depth.fetch_pages:
        await research.read_pages(ctx, more, steps, limit=1)
    await ctx.guard()
    # Ranked against everything the run has, not just the new results: the
    # follow-up is meant to add a missing piece, not to replace the evidence.
    return await research.rank(ctx, question, ctx.sources.all(), top_k=ctx.depth.passages)


async def _classify(ctx: RunContext, steps: Steps, pinned: linked.Linked) -> Classification:
    """Decide whether to search, and with what.

    A classifier failure is not fatal: searching the raw question is a fine
    default. An *auth* failure is fatal, and pretending otherwise would burn a
    minute of research before failing at the same place anyway. With a linked
    page read, the default is that page instead: the question was about it.
    """
    async with steps("Reading the question") as step:
        try:
            ctx.budget.spend_llm()
            # The router model when one is configured: this is the only call in
            # a run whose job is triage rather than writing, and it is the call
            # that stands between the user and the first byte of output.
            # The pictures go to the classifier only when the chat model is the
            # one classifying, the model known to see them. A separate router
            # is told they are there and has to go by the words.
            sees = ctx.router is None
            result = await (ctx.router or ctx.llm).structured(
                prompts.with_images(
                    prompts.classify(
                        ctx.query,
                        ctx.history,
                        ctx.depth,
                        ctx.lenses,
                        choose_lenses=ctx.auto_lenses,
                        catalog=ctx.catalog,
                        linked=pinned.pages,
                        images=len(ctx.images),
                        sees_images=sees,
                    ),
                    ctx.images if sees else [],
                    earlier=ctx.images_earlier,
                ),
                Classification,
                max_tokens=CLASSIFY_TOKENS,
            )
        except (LLMAuthError, LLMConfigError):
            raise
        except LLMError as exc:
            if ctx.images:
                # Asked about a picture: the picture is the evidence, and the
                # raw question ("what is this?") is no search query.
                log.info("classification failed, answering from the image: %s", exc)
                step.text = "Answering from the image"
                return Classification(needs_search=False, standalone_question=ctx.query)
            if pinned:
                log.info("classification failed, answering from the linked page: %s", exc)
                step.text = "Answering from the linked page"
                return Classification(needs_search=False, standalone_question=ctx.query)
            log.info("classification failed, searching the raw question: %s", exc)
            step.text = "Searching the question as asked"
            query = links.remove(ctx.query) or ctx.query
            return Classification(
                needs_search=True,
                standalone_question=ctx.query,
                search_queries=[query],
                wants_image=route_module.asks_to_see(ctx.query),
            )

        # A picture asked for by name is not left to the model's reading.
        # Only ever raised: the words decide "yes", the model decides the rest.
        # Not with a picture attached: "what is in this picture" is about the
        # one in hand, not a request to be shown another.
        if not result.wants_image and not ctx.images and route_module.asks_to_see(ctx.query):
            log.info("the question asks for a picture by name; the classifier missed it")
            result.wants_image = True
        # A picture can only be shown if a search returned one, so asking to see
        # something is asking for a search. The two fields are answered in the
        # same breath and do contradict each other; the picture is the more
        # specific claim. Settled here so the step below says what will happen.
        if result.wants_image and not result.needs_search:
            result.needs_search = True
        result.image_query = " ".join(result.image_query.split())

        # A URL in a query is searched as a string of words. The classifier is
        # told not to write one, and this holds when it does anyway.
        written = (links.remove(q) if "://" in q else q.strip() for q in result.search_queries if q)
        queries = [q for q in written if q][: ctx.depth.queries]
        result.search_queries = queries
        # Dropped here rather than at the search, so the step the reader sees
        # and the window the search actually used cannot disagree.
        if result.recency and shapes.is_timeless(result.answer_shape):
            log.info("shape %r ignores the %r window", result.answer_shape, result.recency)
            result.recency = ""
        _chosen_lenses(ctx, result)
        if not result.needs_search:
            step.text = "Answering directly: no search needed"
        else:
            # What was decided, not that a decision was made: "understood the
            # question" is true of every run and tells the reader nothing.
            labels = [lens.label for lens in ctx.lenses]
            if len(labels) > 1:
                lenses = f"{', '.join(labels[:-1])} and {labels[-1]}"
            else:
                lenses = labels[0] if labels else "the web"
            said = [f"Searching {lenses}"]
            # Said as it will actually be done: a narrow window restricts the
            # search, a year only moves recent sources up the ranking.
            if result.recency in HARD_WINDOWS:
                said.append(f"restricting to the past {result.recency}")
            elif result.recency in TIME_RANGES:
                said.append("preferring recent sources")
            step.text = " · ".join(said)
        return result


def _chosen_lenses(ctx: RunContext, result: Classification) -> bool:
    """Adopt the corpora the classifier picked, on "Auto" only. Returns whether
    it chose anything usable.

    An explicit selection is a decision and is never overridden. The model gets
    asked only when the reader said it should. A model that names a corpus that
    does not exist, or none at all, leaves the fallback in place rather than
    searching nothing.
    """
    if not ctx.auto_lenses:
        return False
    picked = [i for i in result.lenses if i in lens_module.CHOOSABLE]
    if not picked:
        log.info("auto lenses: nothing usable in %r, keeping %s", result.lenses, ctx.lens_ids)
        return False
    ctx.lenses = _keep_library(ctx, lens_module.resolve(picked))
    log.info("auto lenses: searching %s", ctx.lens_ids)
    return True


async def _mark_passages(ctx: RunContext, passages: list) -> None:
    """Tell each page of the person's papers which of its passages the answer
    is written from, so opening the citation shows them highlighted rather than
    a whole page to search by eye."""
    by_index = dict(ctx.sources.all())
    marked = False
    for passage in passages:
        source = by_index.get(passage.source_index)
        if source is None or not source.url.startswith(LIBRARY_PREFIX):
            continue
        text = " ".join(passage.text.split())
        if text and text not in source.passages:
            source.passages.append(text)
            marked = True
    if marked:
        await ctx.sources.refresh()


async def _fits_whole(ctx: RunContext) -> bool:
    try:
        return await ctx.library.fits_whole()
    except Exception as exc:  # the search still finds what is relevant
        log.info("could not size the attachments: %s", exc)
        return False


async def _library_overview(ctx: RunContext, steps: Steps, *, papers: bool = True) -> list:
    """Every relevant paper's abstract, registered as a source, for a request
    about the library as a whole, and the pages of the files attached to the
    chat (`Library.overview`).

    No search finds "write an introduction": it matches no passage, and the
    semantic floor is right to say so. What such a request needs is what the
    person would reach for, the abstracts of everything they collected, so the
    writer gets those, numbered and checkable like any other source, beside
    whatever the searches found for the details. Outside the source budget,
    which is for evidence found by searching, not for the library itself.
    """
    try:
        found = await ctx.library.overview(papers=papers)
    except Exception as exc:  # the searches still answer, only without the overview
        log.info("could not build the library overview: %s", exc)
        return []
    if not found:
        return []
    abstracts = sum(1 for r in found if r.title.endswith("(abstract)"))
    pages = len(found) - abstracts
    what = " and ".join(
        part
        for part in (
            f"{pages} page{'s' if pages != 1 else ''} of the attached files" if pages else "",
            f"the abstracts of {abstracts} papers" if abstracts else "",
        )
        if part
    )
    async with steps(f"Reading {what}") as step:
        ctx.sources.allow_extra(len(found))
        registered = await ctx.sources.add(found)
        step.text = f"Gave the writer {what}"
    return passages_from(registered)


def _keep_library(ctx: RunContext, lenses: list) -> list:
    """`lenses.resolve` only knows the pickable corpora, so a re-resolved list
    has lost the project's papers. They are put back first, every time."""
    if ctx.library is None:
        return lenses
    return [lens_module.LIBRARY, *(lens for lens in lenses if lens.id != lens_module.LIBRARY.id)]


def _image_query(route: Route, queries: list[str]) -> str:
    """What the picture search asks for: the classifier's caption-style query,
    else the first web query, else (in `search_images`) the question itself."""
    return route.image_query or (queries[0] if queries else "")
