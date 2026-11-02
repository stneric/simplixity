"""Batched embeddings over the OpenAI wire format.

Free tiers rate-limit per *request*, not per token, so the single most important
property here is many inputs per call. The vector dimension is read from the
response and never assumed.

Every failure returns None rather than raising: ranking is an optimisation, and
an unavailable embedding model must degrade the answer, not end the run.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app import net
from app.config import settings
from app.llm.client import Usage, provider_base_url, provider_headers

log = logging.getLogger(__name__)

Vector = list[float]

# ------------------------------------------------------------- what is embedded
#
# Asymmetric retrieval models were trained with a marker on every input saying
# whether it is a question or a passage, and they only ever saw text that way.
# Raw text is out of distribution, and it degrades worst exactly where these
# models earn their place: Nemotron-3-Embed's own card measures Spanish-English
# similarity at 0.06 without its prefixes and 0.80 with them. Every vector this
# app made before had none, so a German question against English papers was
# compared in the one regime the model was never taught.
#
# The table is matched against the model id, most specific first. A model not
# in it gets no prefix, which is right for the symmetric ones (OpenAI's,
# bge-m3) and merely what happened before for anything unknown.

QUERY = "query"
DOCUMENT = "document"
# Text compared with text of its own kind, a paper with a paper, a brief with
# an abstract. The E5 family wants the query marker on both sides for that.
SYMMETRIC = "symmetric"

_E5 = ("query: ", "passage: ")
_SEARCH = ("Represent this sentence for searching relevant passages: ", "")
_PREFIXES: tuple[tuple[str, tuple[str, str]], ...] = (
    ("bge-m3", ("", "")),
    ("nemotron", _E5),
    ("multilingual-e5", _E5),
    ("/e5-", _E5),
    ("nomic-embed", ("search_query: ", "search_document: ")),
    ("embeddinggemma", ("task: search result | query: ", "title: none | text: ")),
    (
        "qwen3-embedding",
        ("Instruct: Given a question, retrieve passages that answer it\nQuery: ", ""),
    ),
    ("mxbai-embed", _SEARCH),
    ("snowflake-arctic-embed", _SEARCH),
    ("bge-", _SEARCH),
)

# Bumped whenever what goes into a stored vector changes: the prefixes, the
# header in front of a passage. Stored vectors carry it beside the model name
# (`vector_tag`), so a change makes every old vector stale, and stale vectors
# are re-embedded rather than compared with new ones they no longer match.
INDEX_VERSION = 2


def prefixes(model: str) -> tuple[str, str]:
    """(query prefix, document prefix) for a model, empty where it wants none."""
    wanted = (model or "").lower()
    for marker, pair in _PREFIXES:
        if marker in wanted:
            return pair
    return ("", "")


def prepare(texts: list[str], model: str, kind: str) -> list[str]:
    """Texts as this model expects to see them for this kind of input."""
    query, document = prefixes(model)
    prefix = document if kind == DOCUMENT else query
    return [f"{prefix}{text}" for text in texts] if prefix else list(texts)


def vector_tag(model: str) -> str:
    """What a stored vector records as its origin: the model, and the version of
    what was fed to it."""
    return f"{model}#v{INDEX_VERSION}"


def passage_text(text: str, *, title: str = "") -> str:
    """A passage with the paper it comes from in front of it.

    Cut from its page, "the proposed method reduces the error by a third" says
    nothing about which method or which field; with the title above it the
    vector carries what the passage is about. Only the embedded text gets the
    header. The stored passage, which the writer quotes and the check compares
    word for word, is left as it stands on the page.
    """
    head = (title or "").strip()
    return f"{head}\n{text}" if head else text


async def embed(
    texts: list[str],
    *,
    model: str,
    provider: str = "openrouter",
    usage: Usage | None = None,
) -> list[Vector] | None:
    """Embed every text, batched. Returns None if the provider could not.

    `usage` accumulates what the provider charged. Optional because ranking is
    an optimisation that several callers run outside a priced run, but when a
    run does pass one, the footer's price covers the embeddings too. It read
    zero for them before, which was right only for as long as the configured
    model happened to be one of the free ones.
    """
    if not texts:
        return []

    batches = [
        texts[i : i + settings.embedding_batch_size]
        for i in range(0, len(texts), settings.embedding_batch_size)
    ]
    try:
        headers = provider_headers(provider)
    except Exception as exc:
        log.info("embeddings unavailable: %s", exc)
        return None

    url = f"{provider_base_url(provider)}/embeddings"
    results = await asyncio.gather(
        *(_embed_batch(url, headers, batch, model, usage) for batch in batches),
        return_exceptions=True,
    )

    vectors: list[Vector] = []
    for result in results:
        if isinstance(result, BaseException) or result is None:
            log.info("embedding batch failed (%s); falling back to lexical ranking", result)
            return None
        vectors.extend(result)

    if len(vectors) != len(texts):
        log.info("embedding count mismatch: got %d for %d inputs", len(vectors), len(texts))
        return None
    return vectors


async def _embed_batch(
    url: str,
    headers: dict[str, str],
    batch: list[str],
    model: str,
    usage: Usage | None = None,
) -> list[Vector] | None:
    response = await net.client().post(
        url,
        json={"model": model, "input": batch},
        headers=headers,
        timeout=settings.embedding_timeout_s,
    )
    if response.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"HTTP {response.status_code}", request=response.request, response=response
        )
    body = response.json()
    if usage is not None:
        usage.add(body.get("usage"))
    data = body.get("data") or []
    # The API may return items out of order; `index` is authoritative.
    ordered = sorted(data, key=lambda item: item.get("index", 0))
    vectors = [item.get("embedding") for item in ordered]
    if any(not isinstance(v, list) or not v for v in vectors):
        return None
    return vectors
