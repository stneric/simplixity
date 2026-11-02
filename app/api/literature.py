"""Literature: reading lists, their intake conversation, search rounds,
screening and BibTeX export. See app/tools/literature.py for the workflow.

Plain requests, like the redact tool's: a round takes as long as the slowest
database it asks (bounded by `scholar_timeout_s`), and the browser shows that
it is waiting. Every collection belongs to a profile; another profile's reads
as missing.
"""

from __future__ import annotations

import re
from pathlib import Path as PathLib
from typing import Annotated, Any
from urllib.parse import quote, unquote

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from app.api.deps import ID_PATTERN, DbDep, UserDep, rate_limit
from app.config import settings
from app.llm.client import LLMError
from app.scholar import fulltext as fulltext_module
from app.scholar import sources
from app.store import library, projects
from app.store import literature as store
from app.tools import documents, fulltext, literature

router = APIRouter()

WEB_DIR = PathLib(__file__).resolve().parent.parent.parent / "web"

CollectionId = Annotated[str, Path(pattern=ID_PATTERN)]
FileId = Annotated[str, Path(pattern=ID_PATTERN)]


class CreateRequest(BaseModel):
    name: str = Field(default="", max_length=120)
    project_id: str | None = Field(default=None, pattern=ID_PATTERN)


class BriefBody(BaseModel):
    question: str = Field(default="", max_length=600)
    purpose: str = Field(default="", max_length=400)
    field: str = Field(default="", max_length=120)
    include: list[str] = Field(default_factory=list, max_length=10)
    exclude: list[str] = Field(default_factory=list, max_length=10)
    year_from: int = Field(default=0, ge=0, le=2100)
    year_to: int = Field(default=0, ge=0, le=2100)
    queries: list[str] = Field(default_factory=list, max_length=6)
    language: str = Field(default="", max_length=8)


class PatchRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    # "" unfiles; absent leaves it alone.
    project_id: str | None = Field(default=None, pattern=r"^$|" + ID_PATTERN)
    brief: BriefBody | None = None


class TalkRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class ScreenRequest(BaseModel):
    paper_ids: list[int] = Field(min_length=1, max_length=literature.SCREEN_BATCH)
    force: bool = False


class AssignRequest(BaseModel):
    # None releases the file back to the unassigned list.
    paper_id: int | None = None


class DecideRequest(BaseModel):
    decision: str | None = Field(default=None, pattern=r"^(|relevant|maybe|excluded)$")
    note: str | None = Field(default=None, max_length=500)


async def _owned(db, collection_id: str, user: str) -> dict[str, Any]:
    collection = await store.get(db, collection_id, user=user)
    if collection is None:
        raise HTTPException(status_code=404, detail="Unknown reading list")
    return collection


async def _check_project(db, project_id: str | None, user: str) -> None:
    if project_id and await projects.get(db, project_id, user=user) is None:
        raise HTTPException(status_code=404, detail="Unknown project")


@router.get("/api/literature")
async def list_collections(db: DbDep, user: UserDep) -> dict:
    return {
        "collections": await store.list_collections(db, user=user),
        # What a round will ask, so the page can say so before anyone searches,
        # and say why IEEE Xplore is missing when it is.
        "sources": [{"id": s, "label": sources.LABELS[s]} for s in sources.available()],
        "ieee": bool(settings.ieee_api_key),
    }


@router.post("/api/literature")
async def create_collection(body: CreateRequest, db: DbDep, user: UserDep) -> dict:
    await _check_project(db, body.project_id, user)
    return await store.create(db, body.name, project_id=body.project_id, user=user)


@router.get("/api/literature/{collection_id}")
async def get_collection(collection_id: CollectionId, db: DbDep, user: UserDep) -> dict:
    collection = await _owned(db, collection_id, user)
    return {
        "collection": collection,
        "papers": await store.list_papers(db, collection_id),
        "files": await fulltext.files(db, collection_id, user=user),
    }


@router.patch("/api/literature/{collection_id}")
async def patch_collection(
    collection_id: CollectionId, body: PatchRequest, db: DbDep, user: UserDep
) -> dict:
    await _owned(db, collection_id, user)
    fields: dict[str, Any] = {}
    if body.name is not None:
        fields["name"] = body.name.strip()
    if body.project_id is not None:
        await _check_project(db, body.project_id, user)
        fields["project_id"] = body.project_id or None
    if body.brief is not None:
        fields["brief"] = literature.Brief(**body.brief.model_dump()).tidy().model_dump()
    if fields:
        await store.update(db, collection_id, user=user, **fields)
    return {"collection": await store.get(db, collection_id, user=user)}


@router.delete("/api/literature/{collection_id}")
async def delete_collection(collection_id: CollectionId, db: DbDep, user: UserDep) -> dict:
    if not await store.delete(db, collection_id, user=user):
        raise HTTPException(status_code=404, detail="Unknown reading list")
    return {"ok": True}


@router.post("/api/literature/{collection_id}/talk")
async def talk(collection_id: CollectionId, body: TalkRequest, db: DbDep, user: UserDep) -> dict:
    collection = await _owned(db, collection_id, user)
    try:
        return await literature.talk(db, collection, body.message, user=user)
    except LLMError as exc:
        # 502: the request was fine, the model behind it was not. Nothing was
        # stored, so the page keeps the message for a second try.
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/api/literature/{collection_id}/rounds", dependencies=[Depends(rate_limit)])
async def search_round(collection_id: CollectionId, db: DbDep, user: UserDep) -> dict:
    """Run one search round and return its report with the papers it added."""
    collection = await _owned(db, collection_id, user)
    try:
        report = await literature.run_round(db, collection, user=user)
    except literature.LiteratureError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "report": report,
        "papers": await store.list_papers(db, collection_id),
    }


@router.post("/api/literature/{collection_id}/screen")
async def screen(
    collection_id: CollectionId, body: ScreenRequest, db: DbDep, user: UserDep
) -> dict:
    """Summaries for a handful of papers, read against the brief."""
    collection = await _owned(db, collection_id, user)
    try:
        summaries = await literature.screen(
            db, collection, body.paper_ids, user=user, force=body.force
        )
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    # JSON object keys are strings; the page looks them up by paper id.
    return {"summaries": {str(k): v for k, v in summaries.items()}}


@router.patch("/api/literature/{collection_id}/papers/{paper_id}")
async def decide(
    collection_id: CollectionId, paper_id: int, body: DecideRequest, db: DbDep, user: UserDep
) -> dict:
    await _owned(db, collection_id, user)
    if not await store.decide(db, collection_id, paper_id, decision=body.decision, note=body.note):
        raise HTTPException(status_code=404, detail="Unknown paper")
    return {"paper": await store.get_paper(db, collection_id, paper_id)}


@router.get("/api/literature/{collection_id}/export.bib")
async def export(
    collection_id: CollectionId,
    db: DbDep,
    user: UserDep,
    include: Annotated[str, Query(pattern=r"^(relevant|maybe)(,(relevant|maybe))?$")] = "relevant",
) -> Response:
    collection = await _owned(db, collection_id, user)
    chosen = tuple(sorted(set(include.split(","))))
    text, stats = await literature.export_bibtex(db, collection, chosen)
    filename = re.sub(r"[^A-Za-z0-9._-]+", "-", collection["name"]).strip("-")[:60] or "literature"
    return Response(
        content=text,
        media_type="application/x-bibtex; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}.bib"',
            # The page reads these to say how many entries came from the registry.
            "X-Bib-Entries": str(stats["entries"]),
            "X-Bib-Registry": str(stats["registry"]),
            "X-Bib-Built": str(stats["built"]),
            "Cache-Control": "no-store",
        },
    )


# ------------------------------------------------------------------ full texts


@router.post("/api/literature/{collection_id}/files")
async def upload_file(
    collection_id: CollectionId,
    request: Request,
    db: DbDep,
    user: UserDep,
    paper_id: int | None = None,
    own: bool = False,
) -> dict:
    """One PDF as the raw request body, its name in `X-Filename`.

    A raw body rather than a form, as the redact tool does it: multipart would
    be a ninth dependency for one field, and the browser sends a File as a body
    as readily as inside a form. `paper_id` is the card it was dropped on;
    `own` makes it a paper of the list when it matches none
    (`fulltext.adopt`).
    """
    collection = await _owned(db, collection_id, user)
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="The upload was empty.")
    if len(data) > fulltext_module.MAX_BYTES:
        raise HTTPException(status_code=413, detail="The PDF is too large.")
    name = unquote(request.headers.get("x-filename", "")) or "paper.pdf"
    try:
        return await fulltext.upload(
            db, collection, name, data, user=user, paper_id=paper_id, own=own
        )
    except fulltext_module.FulltextError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.patch("/api/literature/{collection_id}/files/{file_id}")
async def assign_file(
    collection_id: CollectionId, file_id: FileId, body: AssignRequest, db: DbDep, user: UserDep
) -> dict:
    collection = await _owned(db, collection_id, user)
    try:
        file = await fulltext.assign(db, collection, file_id, body.paper_id, user=user)
    except fulltext_module.FulltextError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if file is None:
        raise HTTPException(status_code=404, detail="Unknown file")
    return {"file": file}


@router.post("/api/literature/{collection_id}/files/{file_id}/paper")
async def file_as_paper(
    collection_id: CollectionId, file_id: FileId, db: DbDep, user: UserDep
) -> dict:
    """A waiting PDF made a paper of its own, for one that is none of the list's."""
    collection = await _owned(db, collection_id, user)
    try:
        result = await fulltext.add_as_paper(db, collection, file_id, user=user)
    except fulltext_module.FulltextError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if result is None:
        raise HTTPException(status_code=404, detail="Unknown file")
    return result


@router.delete("/api/literature/{collection_id}/files/{file_id}")
async def delete_file(
    collection_id: CollectionId, file_id: FileId, db: DbDep, user: UserDep
) -> dict:
    await _owned(db, collection_id, user)
    if not await library.delete_file(db, collection_id, file_id):
        raise HTTPException(status_code=404, detail="Unknown file")
    return {"ok": True}


@router.post("/api/literature/{collection_id}/index")
async def index_files(collection_id: CollectionId, db: DbDep, user: UserDep) -> dict:
    """Embed passages that have no vector from the current embedding model."""
    collection = await _owned(db, collection_id, user)
    return {
        "index": await fulltext.index(db, collection, user=user),
        "files": await fulltext.files(db, collection_id, user=user),
    }


async def _owned_file(db, file_id: str, user: str) -> None:
    owner = await library.owner_of(db, file_id)
    if owner is None or owner[1] != user:
        raise HTTPException(status_code=404, detail="Unknown file")


@router.get("/api/literature/files/{file_id}/pdf")
async def read_file(file_id: FileId, db: DbDep, user: UserDep) -> Response:
    """The file as uploaded. Named for the PDF it nearly always is; a Word,
    Markdown or text file attached to a chat comes back as itself."""
    await _owned_file(db, file_id, user)
    found = await library.original_of(db, file_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Unknown file")
    name, kind, data = found
    return Response(
        content=data,
        media_type=documents.MEDIA_TYPES.get(kind, "application/octet-stream"),
        headers={
            # Inline, so the browser opens its viewer on the cited page.
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(name)}",
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/api/literature/files/{file_id}")
async def file_info(file_id: FileId, db: DbDep, user: UserDep) -> dict:
    """What the reader shows around a PDF: the file and the paper it is."""
    await _owned_file(db, file_id, user)
    owner = await library.owner_of(db, file_id)
    file = await library.get_file(db, owner[0], file_id)  # type: ignore[index]
    paper = (
        await store.get_paper(db, owner[0], file["paper_id"])  # type: ignore[index]
        if file and file.get("paper_id")
        else None
    )
    return {"file": file, "paper": paper}


@router.get("/api/literature/files/{file_id}/pages")
async def file_pages(file_id: FileId, db: DbDep, user: UserDep) -> dict:
    """The text the file was read as, page by page or part by part: what the
    reader shows for a file pdf.js cannot draw, and exactly the text its
    citations were checked against."""
    await _owned_file(db, file_id, user)
    return {"pages": await library.pages_of(db, file_id)}


@router.get("/library/{file_id}/{page}", include_in_schema=False)
async def cited_page(
    file_id: FileId, page: Annotated[int, Path(ge=1, le=100_000)], user: UserDep
) -> FileResponse:
    """Where a citation of a PDF page points: the reader (web/reader.html),
    which renders the PDF on that page with the cited passages marked. The
    passages travel in the fragment, so this page is the same for every
    citation, and what it may read is checked by the API it calls."""
    return FileResponse(
        WEB_DIR / "reader.html", headers={"Cache-Control": "no-cache, must-revalidate"}
    )
