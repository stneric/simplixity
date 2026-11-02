"""Full texts on the literature page: upload, assign, index.

The person downloads the PDFs themselves, the publishers want their own login,
and this app holds none, and hands them back here. Each upload is read page by
page, matched to a paper of the reading list where that is certain, cut into
passages and embedded, all in the one request, so that a file the page lists as
ready is one the project chat can actually search.

Embedding can fail without failing the upload: the passages are stored either
way and searched by keyword until they are indexed, and the file says how many
of its passages carry a vector. The page shows that number and an Index button,
rather than an upload that silently searches worse than the others.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

import aiosqlite

from app.embed.embedder import DOCUMENT, embed, passage_text, prepare, vector_tag
from app.scholar import fulltext, sources
from app.scholar.paper import Paper
from app.store import library as store
from app.store import literature
from app.tools.literature import models_for

log = logging.getLogger(__name__)

# Passages embedded per request. The embedder batches below this itself; the
# bound is on how long one upload or one Index click may hold the request.
INDEX_BATCH = 512


async def upload(
    conn: aiosqlite.Connection,
    collection: dict[str, Any],
    filename: str,
    data: bytes,
    *,
    user: str,
    paper_id: int | None = None,
    own: bool = False,
) -> dict[str, Any]:
    """Store a PDF, match it, index it. Returns the file and the match.

    `paper_id` is the card the person dropped it on; it is still checked
    against the title, and a mismatch is a warning on the file, not a refusal,
    since an extraction can lose a title that is plainly on the page.

    `own` is a paper the person brings rather than one a round found: matched
    against the list first all the same (it may be there already), and made a
    paper of its own (`adopt`) when it is not.
    """
    extracted = await asyncio.to_thread(fulltext.extract, data)
    sha = hashlib.sha256(data).hexdigest()
    pieces = fulltext.chunks(extracted.pages)
    file, new = await store.add_file(
        conn,
        collection["id"],
        name=(filename or "paper.pdf")[:200],
        sha256=sha,
        data=data,
        pages=extracted.pages,
        text_pages=extracted.text_pages,
        warnings=list(extracted.warnings),
        chunks=[(c.page, c.text) for c in pieces],
    )

    papers = await literature.list_papers(conn, collection["id"])
    candidates: list[int] = []
    described = ""
    if paper_id is not None:
        paper = next((p for p in papers if p["id"] == paper_id), None)
        if paper is None:
            raise fulltext.FulltextError("That paper is not in this reading list.")
        await _assign(conn, collection["id"], file, extracted, paper, how="manual")
    elif not file.get("paper_id"):
        found = fulltext.match(extracted, filename, papers)
        candidates = found.candidates
        if found.paper_id is not None:
            paper = next(p for p in papers if p["id"] == found.paper_id)
            await _assign(conn, collection["id"], file, extracted, paper, how=found.how)
            if own:
                # Brought as a source, so it counts as one even if a round had
                # shown it and nobody had decided yet.
                await literature.decide(
                    conn, collection["id"], paper["id"], decision=literature.RELEVANT
                )
        elif own:
            _row, described = await adopt(conn, collection, file, extracted, filename)

    # A file uploaded before and only now made a paper was embedded under its
    # file name, and `assign` cleared those vectors.
    indexed = await index(conn, collection, user=user) if new or described else None
    return {
        "file": await store.get_file(conn, collection["id"], file["id"]),
        "new": new,
        "candidates": candidates,
        "index": indexed,
        "described": described,
    }


async def lookup(described: fulltext.Described, extracted: fulltext.Extracted) -> Paper | None:
    """The registry's record of a PDF, when the PDF can be shown to be it.

    By the DOI or arXiv id it prints, else by its title; either way the
    record's title must be on the PDF's first pages (`fulltext.confirmed`),
    because a DOI from the abstract or a near-miss title search would file the
    person's paper under someone else's name, and every citation with it.
    """
    dois = [described.doi] if described.doi else []
    if described.arxiv:
        # arXiv registers a DataCite DOI for every preprint, which OpenAlex
        # resolves like any other.
        dois.append(f"10.48550/arxiv.{described.arxiv}")
    try:
        found = await sources.openalex_by_dois(dois) if dois else []
        if not found and described.how != "file":
            found = await sources.search(
                sources.OPENALEX, described.title, limit=5, years=(None, None)
            )
    except Exception as exc:
        # Offline, rate-limited, or OpenAlex down: the paper is still added,
        # described by its own PDF, and the upload note says so.
        log.info("lookup of an uploaded paper failed: %s", type(exc).__name__)
        return None
    return next((p for p in found if fulltext.confirmed(extracted, p.title)), None)


async def adopt(
    conn: aiosqlite.Connection,
    collection: dict[str, Any],
    file: dict[str, Any],
    extracted: fulltext.Extracted,
    filename: str,
) -> tuple[dict[str, Any], str]:
    """Make a PDF a paper of the reading list, marked relevant, and file it
    there. Returns the paper and how it was described: "registry", or the
    `Described.how` of a record made from the PDF alone.

    Relevant because bringing it is the decision: the project chat answers
    only from relevant papers, and a paper someone uploaded as their own
    source and then found missing from every answer would be this feature not
    working. If the paper is already in the list (a round found it, or it was
    excluded once), that row is used, not a second one.
    """
    described = fulltext.describe(extracted, filename)
    paper = await lookup(described, extracted)
    how = "registry"
    if paper is None:
        how = described.how
        paper = Paper(
            title=described.title,
            authors=described.authors,
            doi=described.doi,
            arxiv=described.arxiv,
        )
    paper.origin = "upload"
    paper.sources = paper.sources or []
    row = await literature.paper_by_key(conn, collection["id"], paper.key)
    if row is None:
        data = paper.as_dict()
        data["described"] = how
        await literature.add_papers(
            conn,
            collection["id"],
            [{"key": paper.key, "data": data, "origin": "upload", "score": 0.0}],
            round_no=0,
        )
        row = await literature.paper_by_key(conn, collection["id"], paper.key)
    assert row is not None
    await literature.decide(conn, collection["id"], row["id"], decision=literature.RELEVANT)
    await _assign(conn, collection["id"], file, extracted, row, how="upload")
    return row, how


async def _assign(
    conn: aiosqlite.Connection,
    collection_id: str,
    file: dict[str, Any],
    extracted: fulltext.Extracted,
    paper: dict[str, Any],
    *,
    how: str,
) -> None:
    warnings = [w for w in file.get("warnings", []) if w != fulltext.TITLE_NOT_FOUND]
    if how == "manual" and not fulltext.title_found(extracted, paper["data"].get("title", "")):
        warnings.append(fulltext.TITLE_NOT_FOUND)
    await store.assign(conn, collection_id, file["id"], paper["id"], how=how, warnings=warnings)


async def assign(
    conn: aiosqlite.Connection,
    collection: dict[str, Any],
    file_id: str,
    paper_id: int | None,
    *,
    user: str,
) -> dict[str, Any] | None:
    """File an uploaded PDF under a paper by hand, or release it."""
    file = await store.get_file(conn, collection["id"], file_id)
    if file is None:
        return None
    if paper_id is None:
        await store.assign(conn, collection["id"], file_id, None, how="")
        return await store.get_file(conn, collection["id"], file_id)
    paper = await literature.get_paper(conn, collection["id"], paper_id)
    if paper is None:
        raise fulltext.FulltextError("That paper is not in this reading list.")
    pages = await store.pages_of(conn, file_id)
    await _assign(
        conn, collection["id"], file, fulltext.Extracted(pages=pages), paper, how="manual"
    )
    # Filed under a paper, its passages are embedded under that paper's title,
    # so the vectors the assignment cleared are made again straight away.
    await index(conn, collection, user=user)
    return await store.get_file(conn, collection["id"], file_id)


async def add_as_paper(
    conn: aiosqlite.Connection, collection: dict[str, Any], file_id: str, *, user: str
) -> dict[str, Any] | None:
    """A waiting file made a paper of its own: the person says it is none of
    the list's. Read again from its bytes, since the producer's title is not
    stored with the pages."""
    file = await store.get_file(conn, collection["id"], file_id)
    if file is None:
        return None
    found = await store.pdf_of(conn, file_id)
    if found is None or file.get("kind", "pdf") != "pdf":
        raise fulltext.FulltextError("Only a PDF can be added as a paper.")
    extracted = await asyncio.to_thread(fulltext.extract, found[1])
    _row, described = await adopt(conn, collection, file, extracted, file["name"])
    await index(conn, collection, user=user)
    return {"file": await store.get_file(conn, collection["id"], file_id), "described": described}


async def current_tag(conn: aiosqlite.Connection, user: str) -> str | None:
    """How a vector must have been made to count as indexed for this profile,
    or None without an embedding model (then nothing is, and nothing claims to be)."""
    models = await models_for(conn, user)
    return vector_tag(models.embed[0]) if models.embed else None


async def files(conn: aiosqlite.Connection, collection_id: str, *, user: str) -> list[dict]:
    """A collection's files, counted as indexed against the current model."""
    return await store.list_files(conn, collection_id, await current_tag(conn, user))


async def index(conn: aiosqlite.Connection, collection: dict[str, Any], *, user: str) -> dict:
    """Embed the passages that have no vector from the current model.

    Returns what happened, for the page to say: how many were embedded, how
    many are still waiting, and why, when nothing could be.
    """
    models = await models_for(conn, user)
    if models.embed is None:
        return {
            "embedded": 0,
            "waiting": 0,
            "model": "",
            "error": "No embedding model is set, so your PDFs are searched by keyword only. "
            "Turn embeddings on in Settings → Models for searches that find a passage "
            "in other words.",
        }
    model, provider = models.embed
    tag = vector_tag(model)
    pending = await store.unembedded(conn, collection["id"], tag, limit=INDEX_BATCH)
    if not pending:
        return {"embedded": 0, "waiting": 0, "model": model, "error": ""}
    texts = [passage_text(text, title=title) for _, text, title in pending]
    vectors = await embed(prepare(texts, model, DOCUMENT), model=model, provider=provider)
    if not vectors or len(vectors) != len(pending):
        return {
            "embedded": 0,
            "waiting": len(pending),
            "model": model,
            "error": f"The embedding model ({model}) did not answer. The passages are stored "
            "and searched by keyword until they are indexed.",
        }
    await store.set_vectors(
        conn,
        [(chunk_id, vector) for (chunk_id, _, _), vector in zip(pending, vectors, strict=True)],
        tag,
    )
    waiting = len(await store.unembedded(conn, collection["id"], tag, limit=INDEX_BATCH))
    return {"embedded": len(pending), "waiting": waiting, "model": model, "error": ""}
