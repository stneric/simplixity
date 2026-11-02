"""BibTeX for a reading list.

Two ways to get an entry, best first:

* **From the DOI.** doi.org answers `Accept: application/x-bibtex` with the
  registry's own record, so a DOI'd paper gets the metadata the publisher
  deposited: the exact venue name, volume, pages. That beats anything rebuilt
  from a search result, which is a database's paraphrase of the same record.
* **Built here**, from the `Paper`, when there is no DOI or doi.org does not
  answer. Clearly second best, and never silently worse: the fields it has are
  the ones the person screened.

Either way the citation key is ours, `vaswani2017attention`: surname, year, first
word of the title that is not an article. A registry's keys are
`Vaswani_2017`, which collides the moment one author has two papers in a year,
and a key that changes between exports breaks every `\\cite` already written.
Keys are made unique per export with a, b, c suffixes, in collection order, so
they are stable as long as the list is.
"""

from __future__ import annotations

import logging
import re
import unicodedata

import httpx

from app import net
from app.config import settings
from app.scholar.paper import BOOK, CONFERENCE, JOURNAL, PREPRINT, THESIS, Paper

log = logging.getLogger(__name__)

# Words a key must not be built from. Short: it only has to skip what
# titles open with.
_SKIP = frozenset(
    {
        *("a", "an", "the", "on", "of", "in", "for", "to", "and", "with", "towards"),
        *("toward", "via", "using", "from", "at", "by", "is", "are"),
        *("der", "die", "das", "ein", "eine", "zur", "zum", "und", "mit", "über", "von"),
    }
)
_ENTRY_HEAD = re.compile(r"^\s*@(\w+)\s*\{\s*([^,\s]*)\s*,", re.MULTILINE)


def ascii_fold(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text or "")
    folded = folded.replace("ß", "ss").replace("ø", "o").replace("Ø", "O").replace("ł", "l")
    return (
        "".join(c for c in folded if not unicodedata.combining(c))
        .encode("ascii", "ignore")
        .decode()
    )


def surname(author: str) -> str:
    """The family name of `First Middle Last` or `Last, First`."""
    author = (author or "").strip()
    if "," in author:
        return author.split(",", 1)[0].strip()
    parts = author.split()
    if not parts:
        return ""
    # `van der Berg`, `de la Cruz`: the particle belongs to the surname.
    tail = [parts[-1]]
    for word in reversed(parts[:-1]):
        if word.lower() in ("van", "von", "der", "den", "de", "la", "le", "da", "del", "di", "du"):
            tail.insert(0, word)
        else:
            break
    return " ".join(tail)


def base_key(paper: Paper) -> str:
    who = (
        re.sub(r"[^a-z]", "", ascii_fold(surname(paper.authors[0])).lower())
        if paper.authors
        else ""
    )
    words = re.findall(r"[a-z0-9]+", ascii_fold(paper.title).lower())
    first = next((w for w in words if w not in _SKIP and len(w) > 1), words[0] if words else "")
    return f"{who or 'anon'}{paper.year or ''}{first}"[:60] or "paper"


def assign_keys(papers: list[Paper]) -> list[str]:
    """One unique key per paper, in order. The first of a clash keeps the bare
    key; the rest get b, c, … the way biblatex styles letter them."""
    keys: list[str] = []
    used: dict[str, int] = {}
    for paper in papers:
        key = base_key(paper)
        count = used.get(key, 0)
        used[key] = count + 1
        keys.append(key if count == 0 else f"{key}{_letter(count)}")
    return keys


def _letter(n: int) -> str:
    """1 → b, 25 → z, 26 → ab: the bare key is the implicit `a`."""
    letters = ""
    n += 1
    while n:
        n, rest = divmod(n - 1, 26)
        letters = chr(97 + rest) + letters
    return letters


def rekey(entry: str, key: str) -> str:
    """Replace the citation key of a fetched entry with ours."""
    return _ENTRY_HEAD.sub(lambda m: f"@{m.group(1)}{{{key},", entry, count=1)


# ------------------------------------------------------------------ fetching


async def fetch(doi: str) -> str | None:
    """The registry's BibTeX for a DOI, or None. Never raises: a missing entry
    falls back to one built here, which the export says it did."""
    if not doi:
        return None
    try:
        response = await net.client().get(
            f"https://doi.org/{doi}",
            headers={"Accept": "application/x-bibtex; charset=utf-8"},
            timeout=settings.scholar_timeout_s,
        )
    except httpx.HTTPError as exc:
        log.info("doi.org did not answer for a DOI: %s", type(exc).__name__)
        return None
    text = response.text.strip() if response.status_code == 200 else ""
    # A resolver that does not know the format answers with the landing page.
    if not text.startswith("@") or "{" not in text[:40]:
        return None
    return _tidy(text)


def _tidy(entry: str) -> str:
    """doi.org returns one entry on one line for some registries; one field
    per line is what a person diffing a .bib expects."""
    if "\n" in entry.strip():
        return entry.strip()
    head = _ENTRY_HEAD.match(entry)
    if not head:
        return entry.strip()
    body = entry[head.end() :].rstrip().removesuffix("}").strip()
    fields = _split_fields(body)
    lines = [f"@{head.group(1)}{{{head.group(2)},"]
    lines += [f"  {f.strip()}," for f in fields if f.strip()]
    lines[-1] = lines[-1].rstrip(",")
    return "\n".join(lines) + "\n}"


def _split_fields(body: str) -> list[str]:
    """Split on commas outside braces and quotes."""
    fields, depth, quoted, current = [], 0, False, []
    for char in body:
        if char == '"' and depth == 0:
            quoted = not quoted
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        if char == "," and depth == 0 and not quoted:
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    if "".join(current).strip():
        fields.append("".join(current))
    return fields


# --------------------------------------------------------------- completing


def _field_pattern(name: str) -> re.Pattern[str]:
    return re.compile(
        rf"^\s*{name}\s*=\s*(?:\{{((?:[^{{}}]|\{{[^{{}}]*\}})*)\}}|\"([^\"]*)\"|(\w+))",
        re.IGNORECASE | re.MULTILINE,
    )


def field(entry: str, name: str) -> str:
    """A field's value in an entry, braces stripped, or ""."""
    match = _field_pattern(name).search(entry)
    if not match:
        return ""
    return next((g for g in match.groups() if g is not None), "").strip()


def complete(entry: str, paper: Paper) -> str:
    """Fill what a registry entry leaves out from the screened record.

    Registries are only as good as what the publisher deposited, and some
    deposit an entry with no title and no author at all. A .bib entry without a
    title is worse than one built here, so the missing fields are added rather
    than the whole entry being trusted or thrown away. En dashes in page
    ranges become BibTeX's `--`, which every style expects.

    Every entry carries a `url`: the .bib doubles as the person's download
    list, since the full texts sit behind publisher logins this app does not
    (and should not) hold, so each entry has to say where to go.
    """
    additions = []
    if not field(entry, "title") and paper.title:
        additions.append(f"  title = {{{_title(paper.title)}}},")
    if not field(entry, "author") and paper.authors:
        additions.append(f"  author = {{{' and '.join(_name(a) for a in paper.authors)}}},")
    if not field(entry, "year") and paper.year:
        additions.append(f"  year = {{{paper.year}}},")
    if not field(entry, "url") and paper.link:
        additions.append(f"  url = {{{paper.link}}},")
    if additions:
        head = _ENTRY_HEAD.search(entry)
        if head:
            entry = entry[: head.end()] + "\n" + "\n".join(additions) + entry[head.end() :]
    return re.sub(
        r"^(\s*pages\s*=\s*\{[^}]*)",
        lambda m: m.group(1).replace("–", "--").replace("—", "--"),
        entry,
        flags=re.IGNORECASE | re.MULTILINE,
    )


def fill_from(paper: Paper, entry: str) -> None:
    """The other direction: an author and year the databases lacked, read from
    the registry entry, so the citation key is not `anon`."""
    if not paper.authors:
        authors = field(entry, "author")
        if authors:
            paper.authors = [a.strip() for a in re.split(r"\s+and\s+", authors) if a.strip()]
    if not paper.year:
        year = re.search(r"\d{4}", field(entry, "year"))
        if year:
            paper.year = int(year.group(0))


# ------------------------------------------------------------------ building

_ESCAPE = {"&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_"}
# An acronym, a word with an inner capital (`WiFi`, `IoT`), or a word with a
# digit (`5G`): BibTeX styles lowercase titles, and these must survive it.
_PROTECT = re.compile(r"\b(?=\w*[A-Z]\w*[A-Z]|\w*\d\w*[A-Za-z]|\w*[a-z]\w*[A-Z])\w+\b")


def _escape(value: str) -> str:
    text = "".join(_ESCAPE.get(c, c) for c in value)
    # Braces are BibTeX's own syntax; one unbalanced brace corrupts the file.
    return text.replace("{", "").replace("}", "")


def _title(value: str) -> str:
    return _PROTECT.sub(lambda m: "{" + m.group(0) + "}", _escape(value))


def _name(author: str) -> str:
    """`Ada Lovelace` → `Lovelace, Ada`, which BibTeX reads unambiguously
    whatever the number of given names."""
    author = author.strip()
    if "," in author:
        return _escape(author)
    family = surname(author)
    given = author[: len(author) - len(family)].strip()
    return _escape(f"{family}, {given}" if given else family)


def build(paper: Paper, key: str) -> str:
    kind = {
        JOURNAL: "article",
        CONFERENCE: "inproceedings",
        BOOK: "book",
        THESIS: "phdthesis",
        PREPRINT: "misc",
    }.get(paper.kind, "misc")
    fields: list[tuple[str, str]] = [("title", _title(paper.title))]
    if paper.authors:
        fields.append(("author", " and ".join(_name(a) for a in paper.authors)))
    if paper.year:
        fields.append(("year", str(paper.year)))
    if paper.venue and paper.venue != "arXiv":
        venue_field = {"article": "journal", "inproceedings": "booktitle"}.get(kind)
        if venue_field:
            fields.append((venue_field, _escape(paper.venue)))
        elif kind == "misc":
            fields.append(("howpublished", _escape(paper.venue)))
    for name in ("volume", "pages"):
        value = getattr(paper, name)
        if value:
            fields.append((name, _escape(value.replace("–", "--"))))
    if paper.issue:
        fields.append(("number", _escape(paper.issue)))
    if paper.publisher and kind in ("book", "inproceedings"):
        fields.append(("publisher", _escape(paper.publisher)))
    if paper.doi:
        fields.append(("doi", paper.doi))
    if paper.arxiv:
        fields += [("eprint", paper.arxiv), ("archiveprefix", "arXiv")]
    if paper.link:
        fields.append(("url", paper.link))
    body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields)
    return f"@{kind}{{{key},\n{body}\n}}"
