"""Block protocol: the contract between the agent and any renderer.

    block   {id, type, data}                            a new block appeared
    patch   {id, ops: [...]}                            mutate an existing block
    phase   {phase: "researching"|"reading"|"writing"}  coarse UI state
    done    {status, message?, stats?}                  terminal

Ops are the RFC-6902 subset add/replace/remove plus `append` for string deltas
(see store.stream.apply_patch). The frontend can be rewritten without the
backend noticing.
"""

from __future__ import annotations

import uuid
from typing import Any

# Phases, named once so the backend and the UI cannot drift apart.
PHASE_UNDERSTANDING = "understanding"
PHASE_RESEARCHING = "researching"
PHASE_READING = "reading"
# The structured widget call, which is a whole model round-trip of its own and
# happens after the last search step: the one stretch of a run where the page
# would otherwise look finished while it is not.
PHASE_COMPOSING = "composing"
PHASE_WRITING = "writing"

STATUS_ACTIVE = "active"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
# Ran out of budget rather than fell over. Distinct from "failed" because the
# repair differs (a preset to raise, not a provider to fix) and distinct from
# absence because a step that silently did not happen reads, to the person
# looking at the page, exactly like a step that happened and found nothing.
STATUS_SKIPPED = "skipped"
# The answer cites nothing, so there was nothing to check. Distinct from
# `failed` (the checker broke) and from absence (which reads as "checked and
# clean"): an answer with no citations is the one case where this app's whole
# contract was ignored, and it has to be the loudest thing on the page rather
# than the quietest.
STATUS_UNCITED = "uncited"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def block(kind: str, data: dict[str, Any], block_id: str | None = None) -> dict[str, Any]:
    return {"id": block_id or new_id(kind), "type": kind, "data": data}


# ---------------------------------------------------------------- block kinds


def research_block(title: str = "Researching") -> dict[str, Any]:
    """Emitted *before* the classifier call, never after.

    The fork awaits a full non-streaming structured call before the first byte
    reaches the client, and that wait is most of its perceived latency. Emit the
    shell first, populate it with patches after.
    """
    return block("research", {"title": title, "status": STATUS_ACTIVE, "sub_steps": []})


def sources_block(sources: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return block("sources", {"sources": sources or []})


def text_block(content: str = "") -> dict[str, Any]:
    return block("text", {"content": content})


def thinking_block() -> dict[str, Any]:
    """Reasoning-model output. Kept out of the answer so the prose stays clean,
    but shown, because watching a model think is genuinely informative."""
    return block("thinking", {"content": "", "status": STATUS_ACTIVE})


def plan_block(sub_questions: list[str] | None = None) -> dict[str, Any]:
    return block("plan", {"sub_questions": sub_questions or [], "status": STATUS_ACTIVE})


def sub_question_block(question: str) -> dict[str, Any]:
    return block(
        "sub_question",
        {"question": question, "status": STATUS_ACTIVE, "findings": "", "sources": []},
    )


def provenance_block(data: dict[str, Any]) -> dict[str, Any]:
    """How the answer was produced: engines that answered, engines that refused,
    corpora searched, depth, and what it cost.

    A block rather than a field on the terminal event, because the terminal event
    is not persisted, and "which search engine found this" is exactly the
    question a reader asks two days later, looking at a saved chat.
    """
    return block("provenance", data)


def verification_block(data: dict[str, Any]) -> dict[str, Any]:
    """Which of the answer's cited sentences its sources actually carry.

    Emitted after the prose, because it is a statement *about* the prose. A
    block rather than a notice: the reader who wants to know why an answer is
    trustworthy wants the claims, and the reader who does not wants one line:
    which is what a collapsed block is for.
    """
    return block("verification", data)


def notice_block(text: str, level: str = "warn") -> dict[str, Any]:
    """Something degraded but the run continues: a dead search engine, a
    skipped budget. Silence here is how a half-researched answer passes for a
    complete one."""
    return block("notice", {"text": text, "level": level})


# ----------------------------------------------------------------- patch ops
# Generic first; the named helpers below exist so call sites read as intent
# rather than as JSON-Pointer strings.


def replace(path: str, value: Any) -> dict[str, Any]:
    return {"op": "replace", "path": path, "value": value}


def add(path: str, value: Any) -> dict[str, Any]:
    return {"op": "add", "path": path, "value": value}


def append(path: str, value: str) -> dict[str, Any]:
    """String delta, the one extension op. Streams tokens without resending the answer."""
    return {"op": "append", "path": path, "value": value}


def add_sub_step(text: str, status: str = STATUS_ACTIVE) -> dict[str, Any]:
    return add("/data/sub_steps/-", {"text": text, "status": status})


def set_sub_step_status(index: int, status: str = STATUS_DONE) -> dict[str, Any]:
    return replace(f"/data/sub_steps/{index}/status", status)


def set_sub_step_text(index: int, text: str) -> dict[str, Any]:
    return replace(f"/data/sub_steps/{index}/text", text)


def set_status(status: str) -> dict[str, Any]:
    return replace("/data/status", status)


def set_title(title: str) -> dict[str, Any]:
    return replace("/data/title", title)


def append_text(delta: str) -> dict[str, Any]:
    return append("/data/content", delta)


def append_findings(delta: str) -> dict[str, Any]:
    return append("/data/findings", delta)


def add_source(source: dict[str, Any]) -> dict[str, Any]:
    return add("/data/sources/-", source)
