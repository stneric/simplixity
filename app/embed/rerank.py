"""Choosing which passages reach the model.

A fetched page is thousands of words, most of them irrelevant. Sending whole
pages wastes context and buries the answer; sending the first N characters
throws away the part that answers the question. So: chunk, score, keep the top
few, and always attribute each chunk to the source it came from.

Ranking degrades in two steps rather than failing:
  embeddings → lexical overlap → original order.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass

from app.embed.embedder import DOCUMENT, QUERY, embed, passage_text, prepare
from app.llm.client import Usage
from app.search.base import SearchResult

log = logging.getLogger(__name__)

CHUNK_CHARS = 1200
CHUNK_OVERLAP = 150
# One exhaustive page must not crowd out five corroborating ones.
MAX_PER_SOURCE = 3

# Letters of any script. `[a-z0-9]` would read "Größe" as "gr" and
# "Übertragung" as "bertragung", and the fallback would rank a German question
# by the fragments of its words that happen to be ASCII.
_WORD = re.compile(r"[^\W_]{2,}")
# Phrasing, in the two languages questions here arrive in. Without the German
# half, "welche", "der" and "die" outweighed the subject of every German query.
_STOP = frozenset(
    """
    the a an and or but of to in for on with is are was were be been it its this
    that as at by from how what when where which who why does do did can could
    should would will about into than then there their they you your we our
    der die das den dem des ein eine einer eines einem einen und oder aber ist
    sind war waren wird werden wie was wer wo wann warum welche welcher welches
    nicht auch mit von für auf aus bei durch über unter nach vor zum zur sich es
    im in an als zu
    """.split()  # noqa: SIM905, a word list reads as a word list
)


@dataclass(slots=True)
class Passage:
    """A chunk of evidence, permanently attached to its citation number.

    The provenance fields travel with the text because the writer has to weigh
    the sources, and it cannot do that from a numbered list of anonymous
    paragraphs: "Official, 2026-07-27" and "Web, undated, snippet only" support
    very different sentences.
    """

    text: str
    source_index: int
    title: str
    url: str
    score: float = 0.0
    domain: str = ""
    tier: str = ""
    tier_label: str = ""
    published: str = ""
    read: bool = False
    # What an earlier stage already knew about this passage, used only to break
    # ties: the library search's verdict on a passage of the person's papers.
    # A German question scores zero lexically against every English passage, and
    # without a tie-break the first three chunks of the first page won, whether
    # or not the search had matched any of them.
    prior: float = 0.0


def chunk_text(text: str, *, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split on paragraph boundaries, packing up to `size` characters per chunk.

    Overlap exists so a fact spanning a boundary survives in at least one chunk.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        # A single oversized paragraph (tables, minified prose) is hard-split.
        while len(paragraph) > size:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(paragraph[:size])
            paragraph = paragraph[size - overlap :]
        if not current:
            current = paragraph
        elif len(current) + len(paragraph) + 2 <= size:
            current = f"{current}\n\n{paragraph}"
        else:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ""
            current = f"{tail}\n\n{paragraph}".strip() if tail else paragraph
    if current:
        chunks.append(current)
    return chunks


def passages_from(results: list[tuple[int, SearchResult]]) -> list[Passage]:
    """Turn (citation number, result) pairs into rankable passages."""
    passages: list[Passage] = []
    for index, result in results:
        title = result.title or result.domain or result.url
        common = {
            "domain": result.domain,
            "tier": result.tier,
            "tier_label": result.tier_label,
            "published": result.published,
            "read": bool(result.content),
        }
        if result.excerpts:
            # A page of the person's papers comes cut already, at sentences,
            # matched passages first (`search.library`).
            for position, chunk in enumerate(result.excerpts):
                prior = result.relevance if position < result.matched else 0.0
                passages.append(Passage(chunk, index, title, result.url, prior=prior, **common))
        elif result.content:
            for chunk in chunk_text(result.content):
                passages.append(Passage(chunk, index, title, result.url, **common))
        elif result.snippet:
            passages.append(Passage(result.snippet, index, title, result.url, **common))
    return passages


async def rank_passages(
    query: str,
    results: list[tuple[int, SearchResult]],
    *,
    top_k: int,
    model: str | None = None,
    provider: str = "openrouter",
    usage: Usage | None = None,
) -> list[Passage]:
    """Return at most `top_k` passages, best first.

    `usage` is the run's embedding bill, if it has one; see `embedder.embed`.
    """
    passages = passages_from(results)
    if not passages:
        return []
    if len(passages) <= top_k:
        return passages

    scored = None
    if model:
        scored = await _semantic_scores(
            query, passages, model=model, provider=provider, usage=usage
        )
    if scored is None:
        _lexical_scores(query, passages)
    return _take_diverse(passages, top_k)


async def _semantic_scores(
    query: str,
    passages: list[Passage],
    *,
    model: str,
    provider: str,
    usage: Usage | None = None,
) -> bool | None:
    # One request, two kinds of input: the question marked as a query, each
    # passage as a document under the title of the page it came from
    # (`embedder.prepare`, `embedder.passage_text`).
    documents = [passage_text(p.text, title=p.title) for p in passages]
    vectors = await embed(
        prepare([query], model, QUERY) + prepare(documents, model, DOCUMENT),
        model=model,
        provider=provider,
        usage=usage,
    )
    if vectors is None:
        return None
    query_vector, *passage_vectors = vectors
    for passage, vector in zip(passages, passage_vectors, strict=True):
        passage.score = _cosine(query_vector, vector)
    return True


def _lexical_scores(query: str, passages: list[Passage]) -> None:
    """Rare-term overlap: a poor man's BM25, and far better than nothing.

    Weighting by inverse frequency stops a term that appears in every passage
    from deciding the ranking.
    """
    terms = _tokens(query)
    if not terms:
        return
    documents = [_tokens(p.text) for p in passages]
    frequency = {term: sum(term in doc for doc in documents) or 1 for term in terms}
    total = len(documents)

    for passage, document in zip(passages, documents, strict=True):
        if not document:
            continue
        score = sum(math.log(1 + total / frequency[term]) for term in terms if term in document)
        # Normalise so a long chunk does not win purely by containing more words.
        passage.score = score / math.sqrt(len(document))


def _take_diverse(passages: list[Passage], top_k: int) -> list[Passage]:
    """Best passages first, capped per source so the evidence stays broad."""
    ordered = sorted(passages, key=lambda p: (p.score, p.prior), reverse=True)
    taken: list[Passage] = []
    per_source: dict[int, int] = {}
    overflow: list[Passage] = []

    for passage in ordered:
        count = per_source.get(passage.source_index, 0)
        if count >= MAX_PER_SOURCE:
            overflow.append(passage)
            continue
        per_source[passage.source_index] = count + 1
        taken.append(passage)
        if len(taken) == top_k:
            return taken

    # Only if breadth left room unfilled do the runners-up get in.
    return (taken + overflow)[:top_k]


def _tokens(text: str) -> set[str]:
    return {word for word in _WORD.findall(text.lower()) if word not in _STOP}


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0
