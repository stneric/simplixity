"""Tools: the redact tool's three steps, each a plain request.

No run, no stream, no Redis. The browser walks the excerpts one call at a time
(see app/tools/redact.py), which is what lets it show progress, stop, and retry
a single excerpt without the server holding any of the document between calls.
Nothing here is persisted, and nothing here is logged beyond sizes and counts.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.deps import UserDep
from app.llm.client import LLMError
from app.tools import redact

router = APIRouter()


class PlanRequest(BaseModel):
    text: str = Field(min_length=1, max_length=redact.MAX_TEXT_CHARS)


class ScanRequest(BaseModel):
    provider: str = Field(max_length=32)
    model: str = Field(min_length=1, max_length=200)
    # Twice an excerpt's size: a generous bound that still refuses a whole
    # document sent as one "excerpt", which a 4k window would silently truncate.
    text: str = Field(min_length=1, max_length=redact.MAX_CHUNK_CHARS * 2)


@router.post("/api/tools/redact/pdf")
async def pdf_text(request: Request, _user: UserDep) -> dict:
    """The text layer of an uploaded PDF.

    The raw body rather than a multipart form: one file, no fields, and
    multipart parsing in FastAPI needs a dependency this app does not carry.
    """
    declared = int(request.headers.get("content-length") or 0)
    if declared > redact.MAX_PDF_BYTES:
        raise HTTPException(status_code=413, detail="The PDF is larger than 25 MB.")
    data = await request.body()
    if len(data) > redact.MAX_PDF_BYTES:
        raise HTTPException(status_code=413, detail="The PDF is larger than 25 MB.")
    if not data.startswith(b"%PDF"):
        raise HTTPException(status_code=415, detail="That file is not a PDF.")
    try:
        text, pages = await asyncio.to_thread(redact.pdf_text, data)
    except redact.RedactError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"text": text, "pages": pages}


@router.post("/api/tools/redact/plan")
async def plan(body: PlanRequest, _user: UserDep) -> dict:
    """Where the excerpts start and end, sized for this install's local window."""
    size = redact.chunk_chars()
    return {"chunks": redact.chunks(body.text, size), "chunk_chars": size}


@router.post("/api/tools/redact/scan")
async def scan(body: ScanRequest, _user: UserDep) -> dict:
    """The spans one excerpt contains, as the local model found them."""
    try:
        findings, proposed = await redact.scan(body.provider, body.model, body.text)
    except redact.RedactError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except LLMError as exc:
        # 502: the request was fine, the model behind it was not. The browser
        # marks the excerpt unchecked and carries on with the next one.
        raise HTTPException(status_code=502, detail=str(exc))
    return {"findings": findings, "proposed": proposed}
