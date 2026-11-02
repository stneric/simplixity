"""A paper's full text: read out of its PDF, matched to the paper, cut up.

Everything here is pure, bytes and strings in, values out, so the parts that
decide what a passage says and which paper it belongs to can be tested without
a database or a model.

Three rules, each because the alternative produces a citation nobody can check:

* **Text stays on its page.** A passage carries the PDF page it came from, and
  the chat cites that page. Extracting a document as one string and chunking it
  afterwards loses the one fact a thesis needs next to every quotation.
* **A PDF is matched to a paper only when that is certain.** A DOI or an arXiv
  id printed on the first pages, or the paper's full title. Two candidates, or
  none, and the file waits for the person to assign it. A full text filed under
  the wrong paper turns every answer drawn from it into a confident citation of
  a paper that does not say it.
* **Nothing is repaired that cannot be repaired faithfully.** Words broken at a
  line end are joined and whitespace is collapsed. Columns that pypdf reads in
  the wrong order, tables and formulas are left as they come, and pages with
  almost no text are reported, because quietly "fixing" extraction is how a
  passage ends up saying something the page does not.
"""

from __future__ import annotations

import io
import logging
import re
import unicodedata
from dataclasses import dataclass, field

from app.scholar.paper import normalise_arxiv, normalise_doi

log = logging.getLogger(__name__)

# One PDF, at most. A paper is a few megabytes; a scanned book is the case this
# protects against, and it has no text layer to search anyway.
MAX_BYTES = 60 * 1024 * 1024
# A page with less text than this is a figure, a blank, or a scan. Counted and
# reported, never dropped, it may still be the page a figure caption is on.
THIN_PAGE_CHARS = 200
# A PDF with less text than this overall has no usable text layer.
MIN_TEXT_CHARS = 500
# Passages are cut to roughly a paragraph: long enough to carry a claim and its
# qualification, short enough that the passage a search returns is the part
# that matched rather than the page around it.
CHUNK_CHARS = 900
CHUNK_OVERLAP = 150
# Where a paper's identity is printed. The reference list at the back is full
# of other papers' DOIs, which is why the search stops early.
IDENTITY_PAGES = 2
# Titles shorter than this match too much ("Introduction", "Deep Learning").
MIN_TITLE_CHARS = 20

# Warnings, as stable codes the page translates into sentences.
SCANNED = "scanned"
THIN_PAGES = "thin_pages"
TITLE_NOT_FOUND = "title_not_found"
COLUMNS = "columns"


class FulltextError(ValueError):
    """The upload cannot be used. The message is shown as it is."""


@dataclass(slots=True)
class Extracted:
    """A PDF's text, one string per page, and what was wrong with it."""

    pages: list[str]
    warnings: list[str] = field(default_factory=list)
    # The document information dictionary's title and author, as the producer
    # wrote them. Only a paper made from its own PDF (`describe`) reads these,
    # and only after checking them: a Word export titles itself after the file.
    meta_title: str = ""
    meta_author: str = ""

    @property
    def text_pages(self) -> int:
        return sum(1 for page in self.pages if len(page) >= THIN_PAGE_CHARS)

    @property
    def head(self) -> str:
        """The pages a paper's identity is printed on."""
        return "\n".join(self.pages[:IDENTITY_PAGES])


@dataclass(slots=True)
class Match:
    """Which paper a PDF is, and how that was decided. `paper_id` is None when
    it could not be decided; `candidates` then lists who it might be."""

    paper_id: int | None
    how: str = ""
    candidates: list[int] = field(default_factory=list)


# ------------------------------------------------------------------- reading


def extract(data: bytes) -> Extracted:
    """The text of every page. Blocking: callers run it in a thread."""
    if not data.startswith(b"%PDF"):
        raise FulltextError("This is not a PDF.")
    if len(data) > MAX_BYTES:
        raise FulltextError(f"The PDF is larger than {MAX_BYTES // (1024 * 1024)} MB.")

    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise FulltextError("The PDF is password protected. Remove the password first.")
        pages, reordered = [], 0
        for page in reader.pages:
            plain = page.extract_text() or ""
            columns = by_columns(plain, page)
            reordered += columns is not None
            pages.append(clean(columns if columns is not None else plain))
    except FulltextError:
        raise
    except PdfReadError as exc:
        raise FulltextError(f"Could not read the PDF: {exc}")
    except Exception as exc:
        # pypdf raises a zoo of types on malformed files. The person can still
        # download the paper again, which is usually what fixes it.
        log.warning("pdf extraction failed: %s", type(exc).__name__)
        raise FulltextError("Could not read this PDF. Try downloading it again.")

    result = Extracted(pages=pages, **_info(reader))
    if reordered:
        result.warnings.append(COLUMNS)
    total = sum(len(page) for page in pages)
    if total < MIN_TEXT_CHARS:
        result.warnings.append(SCANNED)
    elif result.text_pages < len(pages) * 0.75:
        result.warnings.append(THIN_PAGES)
    return result


def _info(reader) -> dict[str, str]:
    """Title and author from the PDF's information dictionary, or blanks. Read
    with its own guard: a damaged dictionary costs the metadata, not the file."""
    try:
        info = reader.metadata or {}
        return {
            "meta_title": clean(str(info.get("/Title") or "")).replace("\n", " ")[:300],
            "meta_author": clean(str(info.get("/Author") or "")).replace("\n", " ")[:300],
        }
    except Exception:
        return {}


# ------------------------------------------------------------------- columns
#
# pypdf reads text in the order the PDF draws it. For most two-column papers
# that is column by column; for some it is row by row across the page, and
# every line then reads "first half of a left sentence second half of a right
# one". A passage cut from that matches nothing anyone wrote, a quotation from
# it fails the word-for-word check, and the search finds neither column.
#
# Repaired only where it can be done faithfully: pypdf's layout mode keeps each
# line's horizontal position, so a page laid out in two columns shows a gutter
# of blanks at the same place on nearly every line. Split there, the left
# column is read top to bottom and then the right one. And only when the plain
# reading demonstrably interleaved them; a page pypdf already read column by
# column is left exactly as it was.

# The gutter is looked for in the middle of the line, never at its edges.
_GUTTER_FROM, _GUTTER_TO = 0.3, 0.7
# Share of the lines long enough to reach the gutter that must be blank there.
GUTTER_BLANK = 0.9
# Share of the lines that must carry text on both sides of it.
TWO_SIDED = 0.4
# Share of those rows the plain reading must show run together.
INTERLEAVED = 0.5
_BLANK = 2  # columns of blanks either side of the gutter
# Share of the two-sided rows that must hold a few words on each side.
PROSE = 0.7


def _gutter(lines: list[str]) -> int | None:
    """The character column every line of a two-column layout leaves blank."""
    width = max((len(line) for line in lines), default=0)
    if width < 40:
        return None
    best, best_score = None, 0.0
    for at in range(int(width * _GUTTER_FROM), int(width * _GUTTER_TO)):
        reaching = [line for line in lines if len(line) > at + _BLANK]
        if len(reaching) < 6:
            continue
        blank = sum(1 for line in reaching if not line[at - _BLANK : at + _BLANK + 1].strip())
        sided = sum(
            1 for line in reaching if line[: at - _BLANK].strip() and line[at + _BLANK :].strip()
        )
        if blank / len(reaching) < GUTTER_BLANK or sided / len(lines) < TWO_SIDED:
            continue
        score = sided / len(lines)
        if score > best_score:
            best, best_score = at, score
    return best


def _flat(text: str) -> str:
    return " ".join(text.split())


def split_columns(plain: str, layout: str) -> str | None:
    """The text column by column, when the layout has two and the plain
    reading interleaved them; None otherwise."""
    lines = [line for line in layout.splitlines() if line.strip()]
    at = _gutter(lines)
    if at is None:
        return None
    rows = [(line[:at].strip(), line[at:].strip()) for line in lines]
    both = [(left, right) for left, right in rows if left and right]
    # Prose on both sides, not a table: a table's rows also run together in the
    # plain reading, and reading one column by column would scramble it.
    prose = sum(1 for left, right in both if len(left.split()) >= 4 and len(right.split()) >= 4)
    if not both or prose / len(both) < PROSE:
        return None
    flat = _flat(plain)
    run_together = sum(1 for left, right in both if _flat(f"{left} {right}") in flat)
    if not both or run_together / len(both) < INTERLEAVED:
        return None
    left = [left for left, _ in rows if left]
    right = [right for _, right in rows if right]
    return "\n".join(left + right)


def by_columns(plain: str, page) -> str | None:
    """A page's text read column by column, or None to keep the plain reading."""
    if len(plain) < THIN_PAGE_CHARS:
        return None
    try:
        layout = page.extract_text(extraction_mode="layout") or ""
    except Exception:  # layout mode is an extra; the plain reading stands
        return None
    return split_columns(plain, layout)


_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")
_SPACES = re.compile("[ \t\u00a0]+")
_LINE_BREAKS = re.compile(r"\s*\n\s*")


def unsurrogate(text: str) -> str:
    """The text with UTF-16 surrogates mended or gone.

    pypdf hands some fonts' characters outside the Basic Multilingual Plane,
    the mathematical italics of a formula (U+1D465 for an italic x) above all, as the two halves
    of a UTF-16 pair rather than as one character, and sometimes only one
    half. Python holds that happily, and then nothing can write it: SQLite,
    JSON and every UTF-8 encoder refuse a surrogate, so a single formula used
    to fail the whole upload. A pair is joined into the character it encodes;
    a lone half stands for nothing and is dropped.
    """
    if not any("\ud800" <= c <= "\udfff" for c in text):
        return text
    joined = text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    # `replace` marks each lone half with U+FFFD; those are the dropped ones.
    # A U+FFFD the PDF really held goes with them, and was never readable.
    return joined.replace("\ufffd", "")


def clean(text: str) -> str:
    """Join words broken at a line end and collapse whitespace.

    A hyphen at a line end is taken to be a break, not a compound, which is
    wrong for "state-\\nof-the-art" and right for almost everything else; a
    stray "stateof-the-art" in a passage is a cosmetic loss, while leaving every
    broken word broken defeats both the keyword search and any quotation check.
    """
    text = unicodedata.normalize("NFC", unsurrogate(text).replace("\r", "\n"))
    text = text.replace("\u00ad", "")  # soft hyphens
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    text = _LINE_BREAKS.sub("\n", text)
    text = _SPACES.sub(" ", text)
    return text.strip()


# ------------------------------------------------------------------ matching

_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>{}]+)", re.IGNORECASE)
_ARXIV = re.compile(r"arxiv[:\s/]*(\d{4}\.\d{4,5})(v\d+)?", re.IGNORECASE)
_ARXIV_FILE = re.compile(r"^(\d{4}\.\d{4,5})(v\d+)?$")


def dois_in(text: str) -> set[str]:
    """Every DOI printed in a text, normalised, trailing punctuation removed."""
    found = set()
    for match in _DOI.finditer(text):
        doi = match.group(1).rstrip(".,;:)]}'")
        found.add(normalise_doi(doi))
    return found


def arxiv_in(text: str) -> set[str]:
    return {normalise_arxiv(m.group(1)) for m in _ARXIV.finditer(text)}


def _letters(text: str) -> str:
    """Lowercase letters and digits only. Extraction loses spaces, gains line
    breaks and swaps ligatures, and none of that should decide whether a title
    is on a page."""
    folded = unicodedata.normalize("NFKD", text)
    return "".join(c for c in folded.lower() if c.isalnum())


def match(extracted: Extracted, filename: str, papers: list[dict]) -> Match:
    """Which of these papers the PDF is.

    `papers` are rows of the reading list: `id` and `data` (the paper record).
    Identifiers are tried before titles, and the filename counts as an
    identifier, since publishers and arXiv name downloads after them
    (`2106.09685v2.pdf`, `10.1109_TSG.2021.123.pdf`).
    """
    head = extracted.head
    stem = filename.rsplit("/", 1)[-1].removesuffix(".pdf").removesuffix(".PDF")
    dois = dois_in(head) | dois_in(stem.replace("_", "/", 1))
    arxivs = arxiv_in(head)
    if _ARXIV_FILE.match(stem):
        arxivs.add(normalise_arxiv(stem))

    by_doi = [p["id"] for p in papers if (p["data"].get("doi") or "") in dois]
    if len(by_doi) == 1:
        return Match(by_doi[0], "doi")
    by_arxiv = [p["id"] for p in papers if (p["data"].get("arxiv") or "") in arxivs]
    if len(by_arxiv) == 1:
        return Match(by_arxiv[0], "arxiv")

    text = _letters(head)
    by_title = [
        p["id"]
        for p in papers
        if len(_letters(p["data"].get("title") or "")) >= MIN_TITLE_CHARS
        and _letters(p["data"]["title"]) in text
    ]
    if len(by_title) == 1:
        return Match(by_title[0], "title")
    candidates = list(dict.fromkeys(by_doi + by_arxiv + by_title))
    return Match(None, "", candidates)


def title_found(extracted: Extracted, title: str) -> bool:
    """Whether a paper's title is printed on the PDF's first pages, the check
    a manual assignment is held to so a wrong one is flagged, not refused."""
    wanted = _letters(title)
    return bool(wanted) and wanted in _letters(extracted.head)


# ------------------------------------------------------- a paper of its own
#
# A PDF the person brings that no search round found: their own draft, a paper
# from a supervisor, a report no database lists. It becomes a paper of the
# reading list, described by the registry where the PDF names itself (a DOI or
# an arXiv id on its first pages) and otherwise by what the file says about
# itself. The guess is labelled as a guess (`Described.how`), because its title
# is what every citation of the paper will print.

# A producer's title that is really a file name ("Microsoft Word - draft3.docx",
# "main.dvi", "untitled").
_FILE_TITLE = re.compile(
    r"(\.(pdf|docx?|dvi|tex|odt|pptx?|indd)\s*$)|^(microsoft (word|powerpoint) - )|^untitled\b",
    re.IGNORECASE,
)
# First-page lines that are a running head, not a title.
_HEADER_LINE = re.compile(
    r"\b(journal|proceedings|vol\.|volume|issn|isbn|doi|https?://|www\.|arxiv|preprint|"
    r"copyright|licen[cs]e|received|accepted|published|conference|©)\b|©",
    re.IGNORECASE,
)


@dataclass(slots=True)
class Described:
    """What can be said about a PDF without asking anyone: the identifiers it
    prints, and a title and authors that are the producer's or a guess."""

    title: str
    authors: list[str] = field(default_factory=list)
    doi: str = ""
    arxiv: str = ""
    # "meta" (the producer's title), "page" (the first line that reads like
    # one) or "file" (the file name): how far the title is to be trusted.
    how: str = "file"


def _stem(filename: str) -> str:
    stem = filename.rsplit("/", 1)[-1]
    stem = re.sub(r"\.pdf$", "", stem, flags=re.IGNORECASE)
    return re.sub(r"[_\s]+", " ", stem).strip() or "Untitled"


def _page_title(first_page: str) -> str:
    """The first line of the first page that could be a title: a few words,
    not a running head, not a sentence of the abstract."""
    for line in first_page.split("\n")[:12]:
        line = line.strip()
        words = line.split()
        if not 3 <= len(words) <= 30 or len(line) > 250 or _HEADER_LINE.search(line):
            continue
        if line.endswith(".") and len(words) > 15:
            continue  # a sentence, so the abstract has started
        return line
    return ""


def _authors(raw: str) -> list[str]:
    parts = re.split(r"\s*(?:;|,\s*and\s+|\s+and\s+|,)\s*", raw)
    return [p for p in (part.strip() for part in parts) if p and len(p) <= 80][:30]


def describe(extracted: Extracted, filename: str) -> Described:
    """A paper record for a PDF from the PDF alone."""
    head = extracted.head
    stem = _stem(filename)
    dois = sorted(dois_in(head))
    arxivs = sorted(arxiv_in(head))
    if _ARXIV_FILE.match(stem.replace(" ", "")):
        arxivs = [normalise_arxiv(stem.replace(" ", ""))]
    # The first DOI on the page is not always the paper's own (a cited one in
    # the abstract), so it is only a lead: the registry's title must then be on
    # the page (`confirmed`) before the paper takes it.
    described = Described(
        title=stem,
        authors=_authors(extracted.meta_author),
        doi=dois[0] if len(dois) == 1 else "",
        arxiv=arxivs[0] if len(arxivs) == 1 else "",
    )
    meta = extracted.meta_title
    if len(_letters(meta)) >= 10 and not _FILE_TITLE.search(meta):
        described.title, described.how = meta, "meta"
    elif title := _page_title(extracted.pages[0] if extracted.pages else ""):
        described.title, described.how = title, "page"
    return described


def confirmed(extracted: Extracted, title: str) -> bool:
    """Whether a registry's record is this PDF: its title is printed on the
    first pages. A DOI lifted from the abstract names someone else's paper."""
    return len(_letters(title)) >= MIN_TITLE_CHARS and title_found(extracted, title)


# ------------------------------------------------------------------ chunking


@dataclass(slots=True)
class Chunk:
    page: int  # 1-based, as a PDF viewer counts
    text: str


def chunks(pages: list[str], size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[Chunk]:
    """Passages, each within one page, cut at a sentence or a space.

    Never across a page boundary: a passage that starts on page 4 and ends on
    page 5 has no page to cite, and citing the first one is wrong half the time.
    """
    out: list[Chunk] = []
    for number, text in enumerate(pages, start=1):
        text = text.replace("\n", " ").strip()
        start = 0
        while start < len(text):
            end = min(len(text), start + size)
            if end < len(text):
                cut = max(
                    text.rfind(". ", start + size // 2, end),
                    text.rfind(" ", start + size // 2, end),
                )
                if cut > start:
                    end = cut + 1
            piece = text[start:end].strip()
            if piece:
                out.append(Chunk(number, piece))
            if end >= len(text):
                break
            start = max(end - overlap, start + 1)
            # Begin on a word, not in the middle of one.
            space = text.find(" ", start, end)
            if space != -1:
                start = space + 1
    return out
