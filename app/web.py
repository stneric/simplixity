"""The frontend's files, and the one way they are served.

Nothing under `web/` is content-hashed: `blocks.js` is `blocks.js` in every
build. Starlette sends an ETag and a Last-Modified but no `Cache-Control`, and a
browser with neither falls back to *heuristic* freshness: it reuses a file for a
while without asking. The shell then loads the new HTML and yesterday's
JavaScript, which is how a deploy turns into a bug report about a feature that
is present in the file being served.

So everything here answers `no-cache, must-revalidate`. That does not mean "do
not cache": the copy is kept and revalidated, so an unchanged file still answers
304 and costs no bytes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

REVALIDATE = {"Cache-Control": "no-cache, must-revalidate"}


def page(path: str, *, media_type: str | None = None) -> FileResponse:
    """One file of the frontend, by its path under `web/`, revalidated on every
    load. For the routes that serve a file outside the `/static` mount."""
    return FileResponse(WEB_DIR / path, media_type=media_type, headers=REVALIDATE)


class RevalidatingFiles(StaticFiles):
    """The `/static` mount, with the same header on every file."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        for header, value in REVALIDATE.items():
            response.headers.setdefault(header, value)
        return response
