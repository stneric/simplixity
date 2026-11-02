"""Reading a file attached to a chat: a PDF, a Word document, Markdown or text.

Pure, bytes in and pages out, like `scholar.fulltext`, which reads the PDFs and
is used here unchanged for them.

**Only a PDF has pages.** Word lays a document out when it is opened, on the
printer and the paper size of whoever opens it, and a text file has no layout
at all. So the other kinds are cut into *parts* of about a page's length, at
paragraph boundaries, numbered from 1, and cited as parts, never as pages: a
"p. 4" that is Word's page 6 on the reader's screen is a citation that cannot
be checked. Word does record where it last broke the pages
(`w:lastRenderedPageBreak`), but only as it rendered them on the author's
machine, which is precisely the number that differs.

Nothing here needs a dependency. A .docx is a zip of XML, and what a question
can be answered from is the text of its paragraphs and table cells.
"""

from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree

from app.scholar import fulltext
from app.scholar.fulltext import Extracted, FulltextError

PDF = "pdf"
DOCX = "docx"
MARKDOWN = "md"
TEXT = "txt"
KINDS = (PDF, DOCX, MARKDOWN, TEXT)

# What the browser is told a file is when it is downloaded again.
MEDIA_TYPES = {
    PDF: "application/pdf",
    DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    MARKDOWN: "text/markdown; charset=utf-8",
    TEXT: "text/plain; charset=utf-8",
}

# A text or Word file, at most. A novel is about a megabyte of text; anything
# far beyond that is not something one chat answers questions about.
MAX_TEXT_BYTES = 20 * 1024 * 1024
# The XML inside a .docx, unpacked. Checked before unpacking, so a zip that
# inflates a few kilobytes into gigabytes is refused rather than read.
MAX_DOCX_XML = 60 * 1024 * 1024
# A part is about a printed page: long enough that a citation narrows the
# place down, short enough that the reader finds the words on it.
PART_CHARS = 3000

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_TEXT_SUFFIXES = {".txt": TEXT, ".text": TEXT, ".md": MARKDOWN, ".markdown": MARKDOWN}

UNSUPPORTED = "Only PDF, Word (.docx), Markdown and plain text files can be attached."


@dataclass(slots=True)
class Document:
    kind: str
    extracted: Extracted


def kind_of(name: str, data: bytes) -> str:
    """What a file is, from its bytes first and its name second. A PDF saved
    as `.txt` is still a PDF; a `.docx` that is not a zip is not Word."""
    lower = (name or "").lower()
    if data.startswith(b"%PDF"):
        return PDF
    if data.startswith(b"PK\x03\x04"):
        if lower.endswith(".docx"):
            return DOCX
        raise FulltextError(UNSUPPORTED)
    suffix = lower[lower.rfind(".") :] if "." in lower else ""
    if suffix in _TEXT_SUFFIXES:
        return _TEXT_SUFFIXES[suffix]
    if lower.endswith(".pdf"):
        raise FulltextError("This is not a PDF.")
    raise FulltextError(UNSUPPORTED)


def read(name: str, data: bytes) -> Document:
    """The file's text, as pages (a PDF) or parts (everything else).
    Blocking: callers run it in a thread."""
    kind = kind_of(name, data)
    if kind == PDF:
        return Document(PDF, fulltext.extract(data))
    if len(data) > MAX_TEXT_BYTES:
        raise FulltextError(f"The file is larger than {MAX_TEXT_BYTES // (1024 * 1024)} MB.")
    paragraphs = _docx_paragraphs(data) if kind == DOCX else _text_paragraphs(data)
    parts = split(paragraphs)
    if not parts:
        raise FulltextError("This file has no text in it.")
    return Document(kind, Extracted(pages=parts))


def _text_paragraphs(data: bytes) -> list[str]:
    if b"\x00" in data[:4096]:
        raise FulltextError(UNSUPPORTED)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        # The other encoding a text file from a Windows machine still comes
        # in. Every byte decodes in it, so this never fails, it can only be
        # wrong about a few letters, which is better than refusing the file.
        text = data.decode("cp1252", errors="replace")
    return _paragraphs(text)


def _paragraphs(text: str) -> list[str]:
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    out = []
    for block in re.split(r"\n\s*\n", text):
        lines = [" ".join(line.split()) for line in block.split("\n")]
        joined = "\n".join(line for line in lines if line)
        if joined:
            out.append(joined)
    return out


def _docx_paragraphs(data: bytes) -> list[str]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            try:
                info = archive.getinfo("word/document.xml")
            except KeyError:
                raise FulltextError("This is not a Word document.")
            if info.file_size > MAX_DOCX_XML:
                raise FulltextError("The Word document is too large to read.")
            xml = archive.read(info)
    except FulltextError:
        raise
    except (zipfile.BadZipFile, OSError, ValueError):
        raise FulltextError("Could not read this Word document. Try saving it again.")
    # A document has no business declaring entities, and refusing any
    # declaration is the whole defence against the ones that expand forever.
    if b"<!DOCTYPE" in xml[:4096] or b"<!ENTITY" in xml:
        raise FulltextError("Could not read this Word document. Try saving it again.")
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        raise FulltextError("Could not read this Word document. Try saving it again.")
    body = root.find(f"{_W}body")
    if body is None:
        return []
    out: list[str] = []
    for element in _blocks(body):
        if element.tag == f"{_W}p":
            text = _run_text(element)
            if text.strip():
                out.append(text.strip())
        elif element.tag == f"{_W}tbl":
            # A row a line, its cells apart: enough for a figure in a table to
            # be found next to the label in its row.
            rows = []
            for row in element.iter(f"{_W}tr"):
                cells = [
                    " ".join(_run_text(p).split())
                    for cell in row.iter(f"{_W}tc")
                    for p in cell.iter(f"{_W}p")
                ]
                cells = [c for c in cells if c]
                if cells:
                    rows.append(" | ".join(cells))
            if rows:
                out.append("\n".join(rows))
    return out


def _blocks(container: ElementTree.Element):
    """The paragraphs and tables of a container, in order, including those
    inside content controls: a cover page or a table of contents built from a
    template is wrapped in one, and skipping it skipped its text."""
    for element in container:
        if element.tag in (f"{_W}p", f"{_W}tbl"):
            yield element
        elif element.tag in (f"{_W}sdt", f"{_W}sdtContent", f"{_W}customXml"):
            yield from _blocks(element)


def _run_text(paragraph: ElementTree.Element) -> str:
    parts = []
    for node in paragraph.iter():
        if node.tag == f"{_W}t" and node.text:
            parts.append(node.text)
        elif node.tag == f"{_W}tab":
            parts.append(" ")
        elif node.tag in (f"{_W}br", f"{_W}cr"):
            parts.append("\n")
    return unicodedata.normalize("NFC", "".join(parts))


def split(paragraphs: list[str], size: int = PART_CHARS) -> list[str]:
    """Paragraphs gathered into parts of about `size` characters, broken only
    between paragraphs, unless one paragraph alone is longer, which is then
    cut at a sentence or a space."""
    parts: list[str] = []
    current: list[str] = []
    length = 0
    for paragraph in paragraphs:
        for piece in _pieces(paragraph, size):
            if current and length + len(piece) > size:
                parts.append("\n".join(current))
                current, length = [], 0
            current.append(piece)
            length += len(piece) + 1
    if current:
        parts.append("\n".join(current))
    return parts


def _pieces(paragraph: str, size: int) -> list[str]:
    out = []
    while len(paragraph) > size:
        cut = max(paragraph.rfind(". ", size // 2, size), paragraph.rfind(" ", size // 2, size))
        cut = cut + 1 if cut > 0 else size
        out.append(paragraph[:cut].strip())
        paragraph = paragraph[cut:].strip()
    if paragraph:
        out.append(paragraph)
    return out
