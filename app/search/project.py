"""The project corpus as a search backend.

A project's earlier answers are retrieved the same way a web result is: scored
for relevance to the question, ranked against everything else, and numbered as
a source the reader can open. That is the whole design: no second retrieval
stack, no separate context slot, no "memory" the answer draws on invisibly.

The consequences are the point:

* **A reused answer is visible.** It arrives as `[4] This project · How Kafka
  logs are parsed`, and clicking it opens the chat it came from. An assistant
  that quietly remembers is one you cannot check.
* **It competes.** At tier trust 0.55 a past answer sits above an unknown page
  and far below a paper, so a project never outranks the evidence. It only
  wins where nothing better was found, which is exactly when it is useful.
* **Screened papers count too.** A paper marked relevant in a reading list
  filed under the project (the literature page) is offered the same way, as
  a paper at the academic tier, with its abstract as the text.
* **It cannot bootstrap itself.** The chat being asked in is excluded, so an
  answer can never cite itself, and only completed runs are eligible.
"""

from __future__ import annotations

import logging

import aiosqlite

from app.search import quality
from app.search.base import INTERNAL_PREFIX, KIND_PAPER, SearchResult, uncited
from app.search.library import opens_at
from app.store import literature, projects

log = logging.getLogger(__name__)

# What the reader clicks. Relative, because the transcript is stored and may
# be read from a different host than the one that wrote it. `INTERNAL_PREFIX` is
# what tells the deduper this is a source and not a malformed link.
CHAT_URL = INTERNAL_PREFIX + "{chat_id}"


async def search(
    conn: aiosqlite.Connection,
    project_id: str,
    question: str,
    *,
    exclude_chat_id: str | None = None,
    limit: int = projects.MAX_CORPUS_RESULTS,
    papers: bool = True,
) -> list[SearchResult]:
    """The project's past answers, and the papers its reading lists marked
    relevant, that bear on this question, best first.

    Never raises: a project that cannot be read is a missing convenience, not a
    failed run.
    """
    try:
        documents = await projects.corpus(conn, project_id, exclude_chat_id=exclude_chat_id)
    except Exception as exc:  # the web search still has to run
        log.info("could not read the project corpus: %s", exc)
        documents = []
    # Not in a literature project: there the library searches the papers
    # itself, page by page, and their abstracts here would be a second copy.
    rows: list = []
    if papers:
        try:
            rows = await literature.relevant_for_project(conn, project_id)
        except Exception as exc:  # same: a missing convenience, not a failed run
            log.info("could not read the project's reading lists: %s", exc)
    if not documents and not rows:
        return []

    candidates = [(_as_result, doc, f"{doc['query']} {doc['answer']}") for doc in documents]
    candidates += [
        (_paper_result, row, f"{row['data'].get('title', '')} {row['data'].get('abstract', '')}")
        for row in rows
        # A source the reader cannot open cannot be checked.
        if opens_at(row["data"])
    ]
    scores = quality.relevance_scores(question, [text for _, _, text in candidates])
    floor = quality.relevance_floor(scores)

    results: list[SearchResult] = []
    for (build, item, _), score in zip(candidates, scores, strict=True):
        if score < floor:
            continue
        results.append(build(item, score))

    results.sort(key=lambda r: r.relevance, reverse=True)
    return results[:limit]


def _paper_result(row: dict, relevance: float) -> SearchResult:
    """A paper the person screened and marked relevant in one of the project's
    reading lists.

    It cites as what it is, a paper, at the academic tier, linked to its DOI:
    the person has read the abstract and judged it on topic, which is more
    than the ranking knows about any web result. The abstract is the content,
    so the answer is written from what the person screened rather than from a
    page fetched again.
    """
    data = row["data"]
    abstract = (data.get("abstract") or data.get("tldr") or "").strip()
    result = SearchResult(
        title=data.get("title") or "Paper",
        # The PDF where a database named one: a click on a paper is a request
        # to read it, not to land on the publisher's abstract page.
        url=opens_at(data),
        snippet=abstract[: projects.SNIPPET_CHARS],
        content=abstract[: projects.SNIPPET_CHARS],
        engine="reading list",
        engines=["reading list"],
        score=0.0,
        published=str(data.get("year") or ""),
        authors=list(data.get("authors") or [])[:6],
        doi=data.get("doi") or "",
        journal=data.get("venue") or "",
        kind=KIND_PAPER,
        relevance=relevance,
    )
    result.tier = quality.ACADEMIC
    result.trust = quality.TIER_TRUST[quality.ACADEMIC]
    result.reasons = ("marked relevant in this project's reading list",)
    return result


def _as_result(doc: dict, relevance: float) -> SearchResult:
    """One past answer as a source.

    The answer goes in as `content`, not as a snippet: it has already been
    read, so there is nothing to fetch and no reason for the ranker to treat it
    as a two-line preview of something longer.
    """
    # Without its citation numbers: they point at that run's sources, and here
    # the reader's "[3]" would read as this run's source 3.
    answer = uncited(doc.get("answer") or "").strip()
    result = SearchResult(
        title=doc.get("chat_title") or doc.get("query") or "Earlier answer",
        url=CHAT_URL.format(chat_id=doc["chat_id"]),
        snippet=answer[: projects.SNIPPET_CHARS],
        content=answer[: projects.SNIPPET_CHARS],
        engine="project",
        engines=["project"],
        # Below any engine score a web result carries, so `position` in the
        # ranking never lifts a past answer over a page the engines agreed on.
        score=0.0,
        published=doc.get("created_at") or "",
        relevance=relevance,
    )
    # Set directly rather than via `assess()`: provenance here is known, and
    # asking the hostname classifier about a relative URL would answer "web".
    result.tier = quality.PROJECT
    result.trust = quality.TIER_TRUST[quality.PROJECT]
    result.reasons = ("answered earlier in this project",)
    return result
