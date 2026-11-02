"""Which of a round's candidates the person sees first.

The order is the product here: a round finds a hundred papers and the person
screens twenty-five, so whatever ranks twenty-sixth may as well not have been
found. Four signals, each one reported back in the round log so the order can
be explained:

* **Relevance.** Before anything has been screened, how well a paper matches
  the brief. Afterwards, **Rocchio feedback** on embeddings: the brief, moved
  towards the centroid of the papers marked relevant and away from the
  excluded ones, the textbook relevance-feedback query and the reason a second
  round is better than the first. Without an embedding model it falls back to
  lexical overlap with the brief, which is worse and never broken, and the
  round says which one ran.
* **Agreement.** How many databases returned it. Independent indexes agreeing
  on a paper is weak evidence that it is central to the topic.
* **The citation graph.** How many of the relevant papers cite it or are cited
  by it. A paper three of your picks cite is very likely one you want.
* **Impact**, citations log-scaled and relative to the round, weighted lightly:
  heavily weighted it buries every recent paper under the classics.
"""

from __future__ import annotations

import logging
import math

from app.embed.embedder import SYMMETRIC, Vector, embed, prepare
from app.scholar.paper import Paper
from app.search import quality

log = logging.getLogger(__name__)

# Rocchio's classic weights: keep the query, move most of the way towards what
# was relevant, a little away from what was not. Negative feedback is the weaker
# signal, a paper is excluded for one reason and resembles the relevant ones in
# many others.
ALPHA, BETA, GAMMA = 1.0, 0.75, 0.25

W_RELEVANCE, W_AGREEMENT, W_GRAPH, W_IMPACT = 0.6, 0.15, 0.15, 0.1

EMBED_CHARS = 1500


def text_of(paper: Paper) -> str:
    return f"{paper.title}. {(paper.abstract or paper.tldr)[:EMBED_CHARS]}"


def lexical(brief_text: str, papers: list[Paper]) -> list[float]:
    if not papers:
        return []
    return quality.relevance_scores(brief_text, [text_of(p) for p in papers])


async def semantic(
    brief_text: str,
    papers: list[Paper],
    relevant: list[Paper],
    excluded: list[Paper],
    *,
    model: str,
    provider: str,
) -> list[float] | None:
    """Cosine to the Rocchio query, or None when no embedding came back."""
    if not papers:
        return []
    texts = [brief_text] + [text_of(p) for p in relevant + excluded + papers]
    # Paper against paper and brief against abstract: like compared with like,
    # which the E5 family wants marked as a query on both sides.
    vectors = await embed(prepare(texts, model, SYMMETRIC), model=model, provider=provider)
    if not vectors:
        return None
    brief_vec = _unit(vectors[0])
    rel = [_unit(v) for v in vectors[1 : 1 + len(relevant)]]
    exc = [_unit(v) for v in vectors[1 + len(relevant) : 1 + len(relevant) + len(excluded)]]
    candidates = vectors[1 + len(relevant) + len(excluded) :]

    query = [ALPHA * x for x in brief_vec]
    if rel:
        query = [q + BETA * c for q, c in zip(query, _mean(rel), strict=True)]
    if exc:
        query = [q - GAMMA * c for q, c in zip(query, _mean(exc), strict=True)]
    query = _unit(query)
    return [_dot(query, _unit(v)) for v in candidates]


def combine(
    papers: list[Paper], relevance: list[float], *, lexical_scores: list[float] | None = None
) -> None:
    """Write `score` onto each paper. Relevance is rescaled to the round's own
    range first: cosines bunch between 0.3 and 0.7, and unscaled they would let
    the other signals decide the order on their own."""
    if not papers:
        return
    low, high = min(relevance), max(relevance)
    spread = (high - low) or 1.0
    most_cited = max((p.citations or 0) for p in papers)
    for i, paper in enumerate(papers):
        rel = (relevance[i] - low) / spread
        if lexical_scores is not None:
            # A light lexical term keeps the brief's own words in play once the
            # embedding query has drifted towards the picks.
            rel = 0.8 * rel + 0.2 * lexical_scores[i]
        agreement = min(max(len(paper.sources) - 1, 0), 3) / 3
        graph = min(paper.links, 3) / 3
        impact = (
            math.log1p(paper.citations or 0) / math.log1p(most_cited) if most_cited > 0 else 0.0
        )
        paper.score = round(
            W_RELEVANCE * rel + W_AGREEMENT * agreement + W_GRAPH * graph + W_IMPACT * impact, 4
        )


def _unit(vector: Vector) -> Vector:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


def _mean(vectors: list[Vector]) -> Vector:
    return [sum(column) / len(vectors) for column in zip(*vectors, strict=True)]


def _dot(a: Vector, b: Vector) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))
