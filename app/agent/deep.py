"""Deep research: plan, investigate in parallel, check for gaps, then report.

The shape is different from quick mode, not merely bigger. A bounded tool loop
asks "what should I search next?" one call at a time; this decomposes the
question up front so the sub-questions can be researched concurrently, and then
asks once whether the evidence is actually sufficient.

Everything here is written to degrade rather than fail. A sub-question whose
search dies contributes nothing and says so; the report is still written from
the ones that worked.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.agent import blocks, prompts, research
from app.agent.budget import Cancelled
from app.agent.context import RunContext, Steps
from app.embed.rerank import Passage
from app.llm import LLMError
from app.search.base import SearchResult
from app.search.quality import as_query
from app.search.searxng import SearchReport

log = logging.getLogger(__name__)

# Evidence per sub-question. Enough to answer one facet; small enough that five
# of them in parallel do not cost more than the report itself.
PASSAGES_PER_SUB_QUESTION = 6
FINDINGS_TOKENS = 400
# Ceilings for the two structured calls. Without one OpenRouter reserves the
# model's whole output window against the balance and can refuse the call, see
# `LLM.structured`. A plan is a handful of question-and-query pairs; a gap check
# is a verdict and at most as many again.
PLAN_TOKENS = 800
GAP_TOKENS = 600
# Above this a "query" is a sentence the planner forgot to reduce, and it goes
# through the keyword reduction like any other. Deliberately loose: the reduction
# is the thing being replaced here, so it is a backstop and not the rule.
MAX_QUERY_WORDS = 12
# How many empty sub-questions the notice names before it stops counting. A
# bare "1 of 9 found no usable sources" sends the reader hunting through nine
# collapsed blocks for the one that came back empty, and most readers simply
# do not, which makes the warning worse than useless: it is unsettling and
# unactionable at once. Naming them is the whole point, so the cap is only
# there to stop a run where everything failed from pasting its plan into a
# banner.
NAMED_UNANSWERED = 3


class SubQuestion(BaseModel):
    """One facet to research, and the words to search for it with.

    The query is asked for separately because deriving it from the question was
    a positional cut, `as_query` keeps the first ten non-stop words, and the
    subject of a precisely worded sub-question is routinely not in the first
    ten. "Which ranking Russian officials […] have been assassinated […] since
    2025" was searched as `ranking Russian officials military officers defense
    industry personnel collaborators have`: the engines answered with pages
    about military ranks, the sub-question reported no usable sources, and the
    report concluded that no assassinations had occurred. The model that wrote
    the sub-question knows which word carries it; nothing else in the pipeline
    does.
    """

    question: str = ""
    query: str = ""

    @model_validator(mode="before")
    @classmethod
    def _accept_a_bare_question(cls, value: Any) -> Any:
        """A model that ignores the schema and writes a plain string still gets
        its sub-question researched, on the old keyword reduction."""
        return {"question": value} if isinstance(value, str) else value

    @property
    def search_query(self) -> str:
        """What actually reaches the engines."""
        query = " ".join(self.query.split())
        if not query or len(query.split()) > MAX_QUERY_WORDS:
            return as_query(query or self.question)
        return query


class Plan(BaseModel):
    sub_questions: list[SubQuestion] = Field(default_factory=list)


class GapCheck(BaseModel):
    sufficient: bool = True
    follow_up_questions: list[SubQuestion] = Field(default_factory=list)


@dataclass(slots=True)
class Outcome:
    """What one sub-question produced. A failure is a result, not a blank.

    An empty string would be filtered out by `_format_findings`, so a
    sub-question that was researched and found nothing would leave no trace in
    the report prompt at all. The writer would see the question, see evidence about
    something else, and have no way to tell "nobody looked" from "somebody looked
    and there is nothing there".
    """

    question: str
    findings: str = ""
    # The words that were actually searched, so a gap check that proposes the
    # identical search again can be recognised before it is paid for.
    query: str = ""


@dataclass(slots=True)
class Research:
    """Everything the deep path hands to the writer."""

    passages: list[Passage] = field(default_factory=list)
    findings: str = ""
    # Sub-questions that were researched and came back with nothing usable.
    unanswered: list[str] = field(default_factory=list)


async def research_deeply(ctx: RunContext, question: str, steps: Steps) -> Research:
    """Plan, investigate, close the gaps, and hand the lot to the writer."""
    pending, plan_id = await _plan(ctx, question, steps)
    outcomes: list[Outcome] = []
    round_number = 0

    while pending:
        await ctx.guard()
        if not ctx.budget.can_research():
            reason = ctx.budget.note_exhaustion("research")
            if reason:
                await ctx.notice(f"{reason} Writing the report with what was gathered.", "info")
            break

        # Never launch more sub-questions than the call budget can pay for.
        affordable = max(1, ctx.depth.max_llm_calls - ctx.budget.llm_calls - 2)
        batch, dropped = pending[:affordable], pending[affordable:]
        if dropped:
            log.info("deep: dropping %d sub-question(s) over budget", len(dropped))

        outcomes.extend(await _investigate_all(ctx, batch))
        round_number += 1

        if round_number > ctx.depth.max_rounds or not ctx.budget.can_research():
            break
        pending = await _find_gaps(ctx, question, outcomes, steps)

    # The plan is finished the moment nothing is pending, and saying so is what
    # folds it away. Without this patch it would stay open for the rest of the run,
    # a list of questions already answered in full underneath it.
    await ctx.stream.emit_patch(plan_id, [blocks.set_status(blocks.STATUS_DONE)])

    unanswered, asked = _unanswered(outcomes)
    if unanswered:
        # Next to the answer, not only inside four collapsed blocks. Which part
        # of the plan came back empty is the single most useful thing a reader
        # can know about how far to trust a report.
        await ctx.notice(_unanswered_notice(unanswered, asked), "info")

    passages = await research.rank(ctx, question, ctx.sources.all(), top_k=ctx.depth.passages)
    return Research(passages, _format_findings(outcomes), unanswered)


def _unanswered(outcomes: list[Outcome]) -> tuple[list[str], int]:
    """The questions still without findings, and how many distinct ones there were.

    Counted per question, not per attempt. A gap round may retry a question that
    came back empty with better search words, and one that then succeeds is
    answered; one that fails twice is one unanswered question, not two, and the
    notice names it once.
    """
    answered = {_key(outcome.question) for outcome in outcomes if outcome.findings}
    distinct: dict[str, str] = {}
    for outcome in outcomes:
        distinct.setdefault(_key(outcome.question), outcome.question)
    unanswered = [text for key, text in distinct.items() if key not in answered]
    return unanswered, len(distinct)


def _unanswered_notice(unanswered: list[str], total: int) -> str:
    """Name the sub-questions that came back empty, not merely how many."""
    named = "; ".join(f"\u201c{question}\u201d" for question in unanswered[:NAMED_UNANSWERED])
    remaining = len(unanswered) - NAMED_UNANSWERED
    if remaining > 0:
        named += f"; and {remaining} more"
    return (
        f"{len(unanswered)} of {total} sub-questions found no usable sources: {named}. "
        "The report covers the rest; treat those as unresearched, not as settled."
    )


# ----------------------------------------------------------------------- plan


async def _plan(ctx: RunContext, question: str, steps: Steps) -> tuple[list[SubQuestion], str]:
    """Decompose the question. A failure here is survivable: the original
    question is a perfectly good single sub-question.

    Returns the sub-questions and the id of the block showing them, which the
    caller marks done once they have all been answered.
    """
    async with steps("Planning the research") as step:
        try:
            ctx.budget.spend_llm()
            plan = await ctx.llm.structured(
                prompts.plan(question, ctx.depth.sub_questions, catalog=ctx.catalog),
                Plan,
                max_tokens=PLAN_TOKENS,
            )
            sub_questions = _clean(plan.sub_questions)[: ctx.depth.sub_questions]
        except LLMError as exc:
            log.warning("planning failed, falling back to the question itself: %s", exc)
            sub_questions = []

        if not sub_questions:
            sub_questions = [SubQuestion(question=question)]
            step.text = "Researching the question directly"
        else:
            step.text = f"Planned {len(sub_questions)} sub-questions"

    block = blocks.plan_block([sub.question for sub in sub_questions])
    await ctx.stream.emit_block(block)
    return sub_questions, block["id"]


# ---------------------------------------------------------------- investigate


async def _investigate_all(ctx: RunContext, sub_questions: list[SubQuestion]) -> list[Outcome]:
    """Research every sub-question concurrently.

    Concurrency is bounded further down (the LLM, search and content layers
    each hold their own semaphore), so this can fan out without a fan-out cap
    of its own.
    """
    results = await asyncio.gather(
        *(_investigate(ctx, sub_question) for sub_question in sub_questions),
        return_exceptions=True,
    )

    outcomes: list[Outcome] = []
    for sub_question, result in zip(sub_questions, results, strict=True):
        if isinstance(result, Cancelled):
            raise result  # stop means stop, even mid-fan-out
        if isinstance(result, BaseException):
            log.warning("sub-question failed: %s: %s", sub_question.question, result)
            # Still an outcome. Dropping it here is how a sub-question that blew
            # up became indistinguishable from one that was never planned.
            outcomes.append(Outcome(sub_question.question, query=sub_question.search_query))
            continue
        outcomes.append(result)
    return outcomes


async def _investigate(ctx: RunContext, sub_question: SubQuestion) -> Outcome:
    """Search, read and summarise one sub-question into its own block."""
    await ctx.guard()
    question = sub_question.question
    query = sub_question.search_query
    block = blocks.sub_question_block(question)
    await ctx.stream.emit_block(block)
    block_id = block["id"]

    try:
        results, own, report = await _search_one(ctx, sub_question)
        if not results and not own:
            await _fail(ctx, block_id, _no_results(ctx, report))
            return Outcome(question, query=query)

        registered, _dropped = await research.register(ctx, results, question, own=own)
        await research.fetch(
            ctx, [r for _i, r in registered if not r.content][: ctx.depth.fetch_pages]
        )

        passages = await research.rank(ctx, question, registered, top_k=PASSAGES_PER_SUB_QUESTION)
        if not passages:
            await _fail(ctx, block_id, "No usable sources for this sub-question.")
            return Outcome(question, query=query)

        await ctx.stream.emit_patch(
            block_id,
            [blocks.replace("/data/sources", sorted({p.source_index for p in passages}))],
        )

        ctx.budget.spend_llm()
        buffer = research.Buffer(ctx, block_id, blocks.append_findings)
        async for delta in ctx.llm.stream(
            prompts.findings(question, passages), max_tokens=FINDINGS_TOKENS
        ):
            if delta.content:
                await buffer.add(delta.content)
        await buffer.flush()

        await ctx.stream.emit_patch(block_id, [blocks.set_status(blocks.STATUS_DONE)])
        return Outcome(question, buffer.text.strip(), query)
    except Cancelled:
        await ctx.stream.emit_patch(block_id, [blocks.set_status(blocks.STATUS_FAILED)])
        raise
    except Exception as exc:
        log.warning("sub-question %r failed: %s", question, exc)
        await _fail(ctx, block_id, f"This sub-question failed: {exc}")
        return Outcome(question, query=query)


async def _search_one(
    ctx: RunContext, sub_question: SubQuestion
) -> tuple[list[SearchResult], list[SearchResult], SearchReport]:
    ctx.budget.searches += 1
    # The report is merged inside: every sub-question searches, and the footer
    # has to name the engines behind the run as a whole.
    results, own, report = await research.search_corpora(
        ctx,
        [sub_question.search_query],
        limit_per_query=ctx.depth.results_per_query,
        question=sub_question.question,
    )
    return results, own, report


def _no_results(ctx: RunContext, report: SearchReport) -> str:
    """Why this sub-question found nothing: the engines' problem or the web's."""
    if ctx.source_bound:
        return f"Nothing in {ctx.library.label.lower()} covers this sub-question."
    if report.unresponsive and not report.engines:
        refused = ", ".join(f"{name} ({reason})" for name, reason in report.unresponsive.items())
        return f"No search engine answered this sub-question: {refused}."
    if report.failures:
        return "The search for this sub-question failed."
    return "Search returned nothing for this sub-question."


async def _fail(ctx: RunContext, block_id: str, message: str) -> None:
    await ctx.stream.emit_patch(
        block_id,
        [blocks.append_findings(message), blocks.set_status(blocks.STATUS_FAILED)],
    )


# ----------------------------------------------------------------------- gaps


async def _find_gaps(
    ctx: RunContext, question: str, outcomes: list[Outcome], steps: Steps
) -> list[SubQuestion]:
    """One follow-up round, only when the evidence is genuinely short.

    Asking is cheap. Looping until an iteration counter runs out, whether or not
    anything is missing, is not.

    The sub-questions that found nothing are named in the prompt, because they
    are the gaps most worth a second query and the check could not see them: it
    was shown the findings, and a sub-question that failed produced none.
    """
    if not any(outcome.findings for outcome in outcomes):
        return []

    async with steps("Checking for gaps") as step:
        try:
            ctx.budget.spend_llm()
            check = await ctx.llm.structured(
                prompts.gaps(
                    question,
                    _format_findings(outcomes),
                    ctx.depth.sub_questions,
                    [outcome.question for outcome in outcomes if not outcome.findings],
                ),
                GapCheck,
                max_tokens=GAP_TOKENS,
            )
        except LLMError as exc:
            log.info("gap check failed, treating the evidence as sufficient: %s", exc)
            step.text = "Gap check unavailable; writing the report"
            return []

        if check.sufficient or not check.follow_up_questions:
            step.text = "Evidence is sufficient"
            return []

        follow_ups = _untried(_clean(check.follow_up_questions), outcomes)
        follow_ups = follow_ups[: ctx.depth.sub_questions]
        if not follow_ups:
            # Every gap it named is a search this run has already made. Asking
            # the engines the same words again returns the same nothing.
            step.text = "No new searches to try, writing the report"
            return []
        step.text = f"Found {len(follow_ups)} gap(s); researching further"
        return follow_ups


# -------------------------------------------------------------------- helpers


def _key(question: str) -> str:
    """What makes two sub-questions the same one, for deduplication."""
    return " ".join((question or "").split()).lower().rstrip("?").strip()


def _untried(follow_ups: list[SubQuestion], outcomes: list[Outcome]) -> list[SubQuestion]:
    """Drop the follow-ups this run has already answered or already searched.

    The gap check is shown the questions that came back empty, so it can suggest
    better words for them, and it often does. It also often hands the same
    question back with the same words, which is an identical search, a second
    empty block and a second line in the notice. A question already answered is
    never worth re-asking; one that failed is, but only with different words.
    """
    answered = {_key(outcome.question) for outcome in outcomes if outcome.findings}
    searched = {(_key(outcome.question), outcome.query) for outcome in outcomes}
    return [
        sub
        for sub in follow_ups
        if _key(sub.question) not in answered
        and (_key(sub.question), sub.search_query) not in searched
    ]


def _clean(questions: list[SubQuestion]) -> list[SubQuestion]:
    """Strip empties and near-duplicates the model sometimes emits."""
    seen: set[str] = set()
    cleaned = []
    for sub_question in questions:
        text = (sub_question.question or "").strip()
        key = _key(text)
        if not text or key in seen:
            continue
        seen.add(key)
        cleaned.append(SubQuestion(question=text, query=sub_question.query))
    return cleaned


def _format_findings(outcomes: list[Outcome]) -> str:
    return "\n\n".join(
        f"### {outcome.question}\n{outcome.findings}" for outcome in outcomes if outcome.findings
    )
