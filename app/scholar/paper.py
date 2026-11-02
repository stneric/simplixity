"""One paper, whichever database described it.

`Paper` is the only shape the rest of the feature knows about, exactly as
`SearchResult` is for the web search: each source module turns its own JSON or
XML into this, and nothing downstream learns that OpenAlex stores an abstract as
an inverted index or that Crossref wraps one in JATS.

Identity is the hard part. The same paper arrives from four databases under four
ids, and as a preprint on arXiv and a proceedings paper at IEEE with two DOIs.
`key` is what the collection deduplicates on, and `merge` folds the copies into
one record that keeps the best of each, the abstract from whichever had one, the
citation count from whichever counts, and the name of every database that found
it, which is the cheapest agreement signal there is.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

# Kinds, coarse: they choose the BibTeX entry type and the badge, and
# a finer split than journal / conference / preprint / book / other is something
# no database agrees with any other database about.
JOURNAL = "journal"
CONFERENCE = "conference"
PREPRINT = "preprint"
BOOK = "book"
THESIS = "thesis"
OTHER = "other"

ABSTRACT_MAX = 6000
AUTHORS_MAX = 30


@dataclass(slots=True)
class Paper:
    title: str
    abstract: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str = ""
    kind: str = OTHER
    doi: str = ""
    arxiv: str = ""
    url: str = ""
    pdf: str = ""
    citations: int | None = None
    # A one-sentence summary Semantic Scholar generates. Shown as a fallback
    # when there is no abstract; it is the database's sentence, not ours.
    tldr: str = ""
    publisher: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    # Database-specific ids: `openalex` (W…), `s2` (paperId), `ieee` (article
    # number), `pmid`. Kept because the follow-up calls, recommendations and
    # the citation graph, are addressed by them.
    ids: dict[str, str] = field(default_factory=dict)
    # Which databases returned it.
    sources: list[str] = field(default_factory=list)
    # OpenAlex ids of the works this one cites, for snowballing. Not persisted:
    # a long reference list is a lot of bytes to keep for one ranking step.
    references: list[str] = field(default_factory=list, repr=False)
    # How the round found it: `search`, `recommended` (Semantic Scholar, from
    # the screened papers), `references` (a relevant paper cites it) or
    # `citations` (it cites a relevant paper).
    origin: str = "search"
    # How many of the collection's relevant papers point at this one, through
    # the citation graph. Filled in by the snowball step.
    links: int = 0
    score: float = 0.0

    @property
    def key(self) -> str:
        """Identity in a collection. A DOI when there is one, then arXiv, then
        the title, which is what a DOI-less conference paper has to go on."""
        if self.doi:
            return f"doi:{self.doi}"
        if self.arxiv:
            return f"arxiv:{self.arxiv}"
        return f"title:{title_key(self.title)}"

    @property
    def link(self) -> str:
        """Where the person goes to read it. The DOI resolver first: it is the
        publisher's own page, and it survives a site redesign."""
        if self.doi:
            return f"https://doi.org/{self.doi}"
        if self.arxiv:
            return f"https://arxiv.org/abs/{self.arxiv}"
        return self.url

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("references", None)
        data.pop("score", None)
        data.pop("links", None)
        data["link"] = self.link
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Paper:
        names = {f for f in cls.__dataclass_fields__ if f not in ("references",)}
        return cls(**{k: v for k, v in data.items() if k in names})


# ------------------------------------------------------------------ cleaning

_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_DOI = re.compile(r"10\.\d{4,9}/\S+", re.IGNORECASE)
_ARXIV = re.compile(r"(\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?", re.IGNORECASE)
# Crossref and IEEE prefix abstracts with a heading the paper did not write.
_ABSTRACT_LABEL = re.compile(r"^\s*(abstract|summary)\s*[:.\-–—]?\s*", re.IGNORECASE)


def clean_text(raw: str | None, *, limit: int = ABSTRACT_MAX) -> str:
    """Markup out, entities decoded, whitespace collapsed.

    Abstracts arrive as JATS XML (Crossref), as HTML with `<sub>` and `<i>`
    (IEEE), and with entities escaped once or twice. What is wanted is the text
    a person would read, and the browser renders it with `textContent`, so a
    tag that survived here would show up as literal angle brackets.
    """
    text = _TAGS.sub(" ", raw or "")
    # Twice: Crossref double-escapes often enough (`&amp;lt;`) to matter.
    text = html.unescape(html.unescape(text))
    text = _SPACE.sub(" ", text).strip()
    text = _ABSTRACT_LABEL.sub("", text)
    if len(text) > limit:
        cut = text.rfind(" ", 0, limit)
        text = text[: cut if cut > limit // 2 else limit] + "…"
    return text


def normalise_doi(raw: str | None) -> str:
    """`https://doi.org/10.1109/ABC.2020.1` → `10.1109/abc.2020.1`.

    Lowercase because DOIs are case-insensitive by specification and the
    databases disagree about case, which would otherwise make one paper two.
    """
    match = _DOI.search(raw or "")
    if not match:
        return ""
    return match.group(0).rstrip(".,;)").lower()


def normalise_arxiv(raw: str | None) -> str:
    """`http://arxiv.org/abs/2101.00001v2` → `2101.00001`. The version goes:
    v1 and v3 are one paper in a reading list."""
    text = (raw or "").strip()
    text = re.sub(r"^(https?://)?(export\.)?arxiv\.org/(abs|pdf)/", "", text, flags=re.I)
    text = re.sub(r"^arxiv:", "", text, flags=re.I).removesuffix(".pdf")
    match = _ARXIV.fullmatch(text) or _ARXIV.match(text)
    return match.group(1) if match else ""


def title_key(title: str) -> str:
    """A title reduced to what two databases agree on: letters and digits,
    accents folded, case dropped, cut at 90 so that one database's subtitle
    does not make it a different paper."""
    folded = unicodedata.normalize("NFKD", title or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", folded.lower())[:90]


def year_of(raw: Any) -> int | None:
    try:
        year = int(str(raw)[:4])
    except (TypeError, ValueError):
        return None
    return year if 1000 < year < 3000 else None


# ------------------------------------------------------------------- merging


def merge(papers: list[Paper]) -> list[Paper]:
    """One record per paper, in the order each was first seen.

    Matched on the DOI, then the arXiv id, then the title. The title match is
    what joins a preprint to its published version, which carry different ids
    and are, for anyone building a reading list, the same paper; the published
    one's DOI wins, because that is the one to cite.
    """
    out: list[Paper] = []
    index: dict[str, Paper] = {}
    for paper in papers:
        if not paper.title.strip():
            continue
        names = identities(paper)
        found = next((index[n] for n in names if n in index), None)
        if found is None:
            out.append(paper)
            found = paper
        else:
            _fold(found, paper)
        for name in identities(found):
            index[name] = found
    return out


def identities(paper: Paper) -> list[str]:
    names = []
    if paper.doi:
        names.append(f"doi:{paper.doi}")
    if paper.arxiv:
        names.append(f"arxiv:{paper.arxiv}")
    key = title_key(paper.title)
    # A short title is "Introduction" or "Editorial", and two of those are not
    # one paper.
    if len(key) >= 24:
        names.append(f"title:{key}")
    return names


def _fold(keep: Paper, other: Paper) -> None:
    """Fill `keep` in from `other`. The longer abstract wins, it is the one
    that was not truncated, and the larger citation count, since the databases
    count from different coverage and the smaller one is the less complete."""
    if len(other.abstract) > len(keep.abstract):
        keep.abstract = other.abstract
    if not keep.doi and other.doi:
        keep.doi = other.doi
        # A DOI is the published version; its venue and kind describe the paper
        # to cite better than the preprint's do.
        keep.venue = other.venue or keep.venue
        keep.kind = other.kind if other.kind != OTHER else keep.kind
        keep.year = other.year or keep.year
    for name in ("arxiv", "url", "pdf", "tldr", "venue", "publisher", "volume", "issue", "pages"):
        if not getattr(keep, name) and getattr(other, name):
            setattr(keep, name, getattr(other, name))
    if keep.kind == OTHER:
        keep.kind = other.kind
    keep.year = keep.year or other.year
    if len(other.authors) > len(keep.authors):
        keep.authors = other.authors
    if other.citations is not None:
        keep.citations = max(keep.citations or 0, other.citations)
    for name, value in other.ids.items():
        keep.ids.setdefault(name, value)
    for source in other.sources:
        if source not in keep.sources:
            keep.sources.append(source)
    if other.references and not keep.references:
        keep.references = other.references
    keep.links = max(keep.links, other.links)
    # Found by the search *and* through the citation graph is the stronger
    # claim, so the graph's origin is kept; it is what the badge explains.
    if keep.origin == "search" and other.origin != "search":
        keep.origin = other.origin
