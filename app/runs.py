"""Starting and finishing a run: the seam between HTTP and the agent.

The API layer should not know about Redis keys, orchestrator internals or how
history is persisted, and the agent should not know about requests. This module
is the only place that knows all three.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

import aiosqlite
from redis.asyncio import Redis

from app.agent import blocks as agent_blocks
from app.agent import depth as depth_module
from app.agent import prompts, runner
from app.agent.orchestrator import RunRequest
from app.agent.orchestrator import run as orchestrate
from app.config import settings
from app.llm import LLM
from app.search import lenses as lens_module
from app.search.base import SearchResult
from app.search.library import Library
from app.search.project import search as project_search
from app.store import attachments, chats, settings_store
from app.store import library as library_store
from app.store.db import DEFAULT_USER
from app.store.stream import RunStream

log = logging.getLogger(__name__)

TITLE_TOKENS = 24


class AtCapacity(RuntimeError):
    """Too many runs already in flight."""


@dataclass(slots=True, frozen=True)
class TitleSpec:
    """Which model names a new chat. `None` anywhere means nothing names it.

    Its own type rather than two fields on `RunRequest`: naming the chat is not
    part of the run, it happens after the run has finished and after its footer
    has been written, and the agent has no business knowing about it.
    """

    model: str
    provider: str


@dataclass(slots=True)
class RunHandle:
    run_id: str
    chat_id: str
    message_id: str
    depth: str
    model: str
    lenses: list[str]


async def start(
    *,
    redis: Redis,
    db: aiosqlite.Connection,
    query: str,
    chat_id: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    depth: str | None = None,
    lenses: list[str] | None = None,
    project_id: str | None = None,
    web: bool = False,
    earlier_chats: bool = False,
    user: str = DEFAULT_USER,
) -> RunHandle:
    """Create the run, persist the pending turn, and hand it to the supervisor."""
    # Claimed, not merely checked: setup below awaits the database several
    # times, and a check alone let every request in that gap take the last slot.
    if not runner.claim():
        raise AtCapacity(
            f"{settings.max_concurrent_runs} runs are already in flight. Wait for one to finish."
        )
    try:
        return await _start(
            redis=redis,
            db=db,
            query=query,
            chat_id=chat_id,
            model=model,
            provider=provider,
            depth=depth,
            lenses=lenses,
            project_id=project_id,
            web=web,
            earlier_chats=earlier_chats,
            user=user,
        )
    except BaseException:
        # Spawning is the last thing `_start` does, so a failure here means no
        # run took the slot over.
        runner.release()
        raise


async def _start(
    *,
    redis: Redis,
    db: aiosqlite.Connection,
    query: str,
    chat_id: str | None,
    model: str | None,
    provider: str | None,
    depth: str | None,
    lenses: list[str] | None,
    project_id: str | None,
    web: bool,
    earlier_chats: bool,
    user: str,
) -> RunHandle:
    # The request may override the saved choice, but never has to repeat it.
    saved = await settings_store.load(db, user)
    resolved_model = model or saved.chat_model
    resolved_provider = provider or saved.chat_provider
    resolved_depth = depth_module.get(depth or saved.depth).id
    # `normalise`, not `resolve`: "auto" is a choice the run has to see, not an
    # unknown id to drop on the way in.
    resolved_lenses = lens_module.normalise(lenses or saved.lenses)

    run_id = uuid.uuid4().hex
    resolved_chat_id = chat_id or uuid.uuid4().hex

    is_new_chat = await chats.ensure_chat(db, resolved_chat_id, query, project_id, user=user)
    history = [] if is_new_chat else await chats.history(db, resolved_chat_id)

    # A chat in a literature project answers from its papers, and a chat with
    # files attached from those files. Decided here, on the server, from where
    # the chat is filed and what is stored under it, never from what the client
    # says: the switch in the composer can turn the web *on*, but nothing a
    # request carries can make a thesis project forget it is one.
    # What setup could not do. Each becomes a notice at the top of the answer:
    # a project whose papers could not be read answers from the web, and that
    # must not look like an ordinary answer from the papers.
    warnings: list[str] = []
    library = await _library_of(db, resolved_chat_id, saved, user, warnings)
    if library is not None:
        # The composer shows no depth or source chips here, only the web
        # switch, so the router decides depth, and on the web it picks the
        # corpora as well.
        resolved_depth = depth_module.AUTO.id
        resolved_lenses = [lens_module.LIBRARY.id] + ([lens_module.AUTO] if web else [])
        # Written by the literature model, the one the person picked on the
        # literature page for work on their papers, rather than by the everyday
        # chat model: a thesis answer quotes passages word for word and cites
        # every sentence, and a fast model does both noticeably worse. A model
        # the request names explicitly still wins, as everywhere. Not for a
        # chat that only has files attached: the person picked that model for
        # their papers, and a question about a letter is an ordinary chat.
        if not model and saved.literature_model and library.project_id:
            resolved_model = saved.literature_model
            resolved_provider = provider or saved.literature_provider or saved.chat_provider
    message_id = await chats.start_message(
        db,
        chat_id=resolved_chat_id,
        run_id=run_id,
        query=query,
        depth=resolved_depth,
        model=f"{resolved_provider}/{resolved_model}",
    )

    stream = RunStream(redis, run_id, ttl=settings.run_ttl_seconds)
    await stream.set_state(
        status="queued",
        phase="starting",
        depth=resolved_depth,
        lenses=",".join(resolved_lenses),
        model=resolved_model,
        provider=resolved_provider,
        chat_id=resolved_chat_id,
        message_id=message_id,
        query=query,
        cancel="0",
    )

    request = RunRequest(
        query=query,
        chat_id=resolved_chat_id,
        model=resolved_model,
        provider=resolved_provider,
        depth=resolved_depth,
        lenses=[i for i in resolved_lenses if i != lens_module.LIBRARY.id],
        embedding_model=saved.embedding_model if saved.embedding_enabled else None,
        embedding_provider=saved.embedding_provider,
        # Empty is the normal case and means "the chat model triages too".
        router_model=saved.router_model or settings.router_model or None,
        router_provider=saved.router_provider or settings.router_provider,
        judge_model=saved.judge_model or None,
        judge_provider=saved.judge_provider or saved.chat_provider,
        home_location=saved.home_location or settings.home_location,
        history=history,
        # Retrieved here rather than inside the run: this module is the only one
        # that knows about both storage and the agent, and keeping it that way
        # is why the agent has never needed a database handle. Scored against
        # the raw question. The classifier's standalone rewrite would be
        # marginally better and would cost the whole retrieval a round-trip's
        # wait behind it.
        # Only when the request asks (`ChatRequest.chats`, the composer's Chats
        # switch). Always on would, in a literature project, let an answer
        # written with the web on launder the web back in; the reader decides
        # per question, and an earlier answer is cited as what it is,
        # `This project · …`, never as a paper.
        project_sources=await _project_sources(
            db, resolved_chat_id, query, warnings, papers=library is None
        )
        if earlier_chats
        else [],
        library=library,
        web=web if library is not None else True,
    )

    for warning in warnings:
        await stream.emit_block(agent_blocks.notice_block(warning))

    runner.spawn(
        stream,
        lambda s: orchestrate(s, request),
        finalize=_finalizer(db, request, is_new_chat, _title_spec(saved)),
        claimed=True,
    )
    log.info(
        "run %s started · chat=%s depth=%s sources=%s model=%s/%s",
        run_id,
        resolved_chat_id,
        resolved_depth,
        "+".join(resolved_lenses),
        resolved_provider,
        resolved_model,
    )
    return RunHandle(
        run_id, resolved_chat_id, message_id, resolved_depth, resolved_model, resolved_lenses
    )


async def _library_of(
    db: aiosqlite.Connection,
    chat_id: str,
    saved: settings_store.UISettings,
    user: str,
    warnings: list[str],
) -> Library | None:
    """The chat's project's papers, if the project has a reading list, and the
    files attached to the chat, if any. None when there is neither.

    A failure to read either is answered as if there were none, which is the
    one way left to answer at all, and is said in `warnings`: otherwise a
    thesis project would quietly answer from the web as an ordinary chat."""
    project_id = None
    try:
        found = await chats.project_of(db, chat_id)
        if found and await library_store.project_has_library(db, found):
            project_id = found
    except Exception as exc:  # an unreadable project is an ordinary chat, said so
        log.warning("could not read the project's reading lists for chat %s: %s", chat_id, exc)
        warnings.append(
            "This project’s reading lists could not be read, so this answer is not "
            "limited to your papers."
        )
    try:
        attached = await attachments.has_files(db, chat_id, user)
    except Exception as exc:  # unreadable attachments: answered without them, said so
        log.warning("could not read the files attached to chat %s: %s", chat_id, exc)
        warnings.append(
            "The files attached to this chat could not be read; this answer ignores them."
        )
        attached = False
    if project_id is None and not attached:
        return None
    return Library(
        db,
        project_id,
        chat_id=chat_id if attached else None,
        user=user,
        embedding_model=saved.embedding_model if saved.embedding_enabled else None,
        embedding_provider=saved.embedding_provider,
    )


async def _project_sources(
    db: aiosqlite.Connection,
    chat_id: str,
    query: str,
    warnings: list[str],
    *,
    papers: bool = True,
) -> list[SearchResult]:
    """What this chat's project already answered that bears on the question.

    Empty for a chat that is not in a project, which is most of them. Never
    raises: a run that cannot read the project's chats still answers. It says
    so, though: the reader switched them on, and "nothing relevant in them" and
    "never looked" must not read the same.
    """
    try:
        project_id = await chats.project_of(db, chat_id)
        if not project_id:
            return []
        found = await project_search(db, project_id, query, exclude_chat_id=chat_id, papers=papers)
    except Exception as exc:  # never fail a run over the sidebar, but say it
        log.warning("could not search the project corpus for chat %s: %s", chat_id, exc)
        warnings.append("This project’s earlier chats could not be searched for this answer.")
        return []
    if found:
        log.info("project corpus contributed %d earlier answer(s)", len(found))
    return found


def _title_spec(saved: settings_store.UISettings) -> TitleSpec | None:
    """Which model names a new chat, or None when nothing should.

    Off is the default. The call cannot be counted in the run's reported price,
    it happens after the footer, so it is opt-in rather than something an install
    pays for without being asked.

    Switching it on picks a model without demanding one: whatever was chosen for
    titles, else the router, else the chat model. A separate title model is worth
    having because this is the one call in the app where the cheapest model in
    the install is unambiguously the right one.
    """
    if not saved.title_enabled:
        return None
    if saved.title_model:
        return TitleSpec(saved.title_model, saved.title_provider or saved.chat_provider)
    if saved.router_model:
        return TitleSpec(saved.router_model, saved.router_provider or saved.chat_provider)
    return TitleSpec(saved.chat_model, saved.chat_provider)


def _finalizer(
    db: aiosqlite.Connection,
    request: RunRequest,
    is_new_chat: bool,
    title: TitleSpec | None = None,
):
    """Persist the finished transcript, whatever the outcome.

    Blocks come from the Redis snapshot rather than being accumulated in memory,
    so what gets stored is exactly what the client rendered.
    """

    async def finalize(stream: RunStream, status: str) -> None:
        blocks = await stream.snapshot()
        if status != "completed":
            blocks = await _with_reason(stream, status, blocks)
        try:
            await chats.finish_message(
                db, stream.run_id, blocks=blocks, answer=_answer_of(blocks), status=status
            )
        except Exception:
            # The row must not stay "running" until the next restart: the UI
            # would rejoin it, and once the stream expires the answer is gone
            # with nothing saying why. Close it out as failed, without the
            # transcript that could not be written, then let the runner log it.
            await chats.finish_message(
                db,
                stream.run_id,
                blocks=[agent_blocks.notice_block("This answer could not be saved.", "error")],
                answer="",
                status="error",
            )
            raise
        if title is not None and is_new_chat and status == "completed":
            await _title(db, request.chat_id, request.query, title)

    return finalize


async def _with_reason(
    stream: RunStream, status: str, blocks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Append why the run ended, so the stored transcript says it too.

    A live client is told by the terminal event, which is not a block and so
    never reaches the transcript. Reopening the chat afterwards showed one
    sentence ("This run did not finish.") for every cause there is, which is
    precisely the case where the cause is the only thing worth knowing.
    """
    try:
        message = (await stream.get_state()).get("message", "")
    except Exception as exc:  # a failed run must still be stored
        log.info("could not read the terminal message for run %s: %s", stream.run_id, exc)
        return blocks
    if not message:
        return blocks
    level = "info" if status == "cancelled" else "error"
    return [*blocks, agent_blocks.notice_block(message, level)]


def _answer_of(blocks: list[dict[str, Any]]) -> str:
    for block in reversed(blocks):
        if block.get("type") == "text":
            return (block.get("data") or {}).get("content", "")
    return ""


async def _title(db: aiosqlite.Connection, chat_id: str, query: str, spec: TitleSpec) -> None:
    """Name the chat. Only reached when the setting asked for it.

    Best-effort by design: the truncated query is already in place, so a failure
    here costs a nicer title and nothing else.

    **Nothing here is in the price the run reported.** This call happens after
    the footer has been written, which is why the setting exists and why it is
    off by default. `reasoning_tokens=0` keeps what it does cost near zero: the
    24-token ceiling is on the title, and a thinking model handed the usual
    1024-token reasoning allowance spent almost all of it deciding what to call
    a chat, which was ~90% of the invisible spend and bought nothing.
    """
    try:
        llm = LLM(spec.model, spec.provider)
        title = await llm.complete(
            prompts.title(query), max_tokens=TITLE_TOKENS, reasoning_tokens=0
        )
        await chats.set_title(db, chat_id, title)
    except Exception as exc:
        log.info("could not generate a title for chat %s: %s", chat_id, exc)


async def cancel(redis: Redis, run_id: str) -> bool:
    """Cooperative cancellation: a flag the run checks between steps."""
    stream = RunStream(redis, run_id, ttl=settings.run_ttl_seconds)
    if not await stream.exists():
        return False
    await stream.cancel()
    return True
