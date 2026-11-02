"""Shared search types.

`SearchResult` is deliberately the *only* shape the agent knows about, so
swapping SearXNG for Tavily or Brave later is a new module, not a refactor.

Two jobs beyond holding fields:

* **Normalising titles.** Engines hand back whatever a page put in its `<title>`,
  truncated to their own width: `PDF EU AI Act - fpf.org`, `EU AI Act
  Timeline: New 2026 to 20...`, `AI Act | Shaping Europe's digital future -
  European Union`. Rendering those verbatim is what makes a source list look
  broken. Cleaning happens here, once, at the boundary, and never in the renderer,
  which would leave the model reading the mess the user does not see.
* **Merging duplicates into evidence.** The same URL coming back from three
  engines and two queries is the cheapest corroboration signal available, so
  `dedupe` accumulates that instead of discarding it.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.search import quality


class SearchError(RuntimeError):
    """The backend could not be reached or refused the query."""


# What kind of thing a source is, for the badge and for the prompt. Coarse on
# purpose: the tier says how much to trust it, this says how to read it.
KIND_PAGE = "page"
KIND_PAPER = "paper"
KIND_PDF = "pdf"
KIND_NEWS = "news"


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    engine: str = ""
    score: float = 0.0
    # Every engine that returned this URL, and how many of the run's queries it
    # answered. Both are corroboration, and both are worth showing the user who
    # asked which search engine actually found this.
    engines: list[str] = field(default_factory=list)
    hits: int = 1
    # Provenance, filled in by `assess()` at the search boundary.
    category: str = ""
    kind: str = KIND_PAGE
    published: str = ""
    # An image the engine attached to this result. Kept because a widget may
    # only show a picture that was actually retrieved: a model must never be
    # in a position to name an image URL itself.
    image: str = ""
    thumbnail: str = ""
    authors: list[str] = field(default_factory=list)
    doi: str = ""
    journal: str = ""
    tier: str = quality.WEB
    trust: float = quality.TIER_TRUST[quality.WEB]
    reasons: tuple[str, ...] = ()
    rank: float = 0.0
    relevance: float = 1.0
    # Filled in later by the content step, when the depth budget allows it.
    content: str = field(default="", repr=False)
    # For a page of the person's papers: the passages of it the answer was
    # written from, and the exact words of it the citation check found carrying
    # a claim. Both are highlighted in the reader (web/reader.js).
    passages: list[str] = field(default_factory=list, repr=False)
    evidence_texts: list[str] = field(default_factory=list, repr=False)
    # For a page of the person's papers: the page cut into the passages it was
    # indexed as, the ones the library search matched first. The reranker takes
    # these instead of cutting `content` again (see `rerank.passages_from`):
    # a PDF page has no blank lines, so that cut fell every 1,200 characters,
    # mid-word, and the passage the search had found was no longer one of them.
    excerpts: list[str] = field(default_factory=list, repr=False)
    # How many of `excerpts`, from the front, the library search matched.
    matched: int = 0
    # For a page of the person's papers: how a thesis names the work, "Vaswani
    # et al. 2017", or a file's name. The prose cites such a page as
    # "[Vaswani et al. 2017, S. 5]" rather than as a bare number, because that
    # is the form the reader will copy into their own text (web/markdown.js).
    byline: str = ""

    @property
    def domain(self) -> str:
        try:
            return (urlparse(self.url).hostname or "").removeprefix("www.")
        except ValueError:
            return ""

    @property
    def evidence(self) -> str:
        """The best text available for this source: page content if it was
        fetched, otherwise the search snippet."""
        return self.content or self.snippet

    @property
    def tier_label(self) -> str:
        return quality.TIER_LABELS.get(self.tier, quality.TIER_LABELS[quality.WEB])

    def assess(self) -> None:
        """Classify provenance from what the engine told us. Idempotent, so a
        merged duplicate can be re-assessed without side effects."""
        verdict = quality.assess(
            self.domain,
            title=self.title,
            url=self.url,
            category=self.category,
            doi=self.doi,
            journal=self.journal,
        )
        self.tier = verdict.tier
        self.trust = verdict.trust
        self.reasons = verdict.reasons

    def score_rank(self, *, prefer_recent: bool = False) -> float:
        self.rank = quality.rank_score(
            trust=self.trust,
            engines=max(1, len(self.engines)),
            hits=self.hits,
            engine_score=self.score,
            relevance=self.relevance,
            published=self.published,
            prefer_recent=prefer_recent,
        )
        return self.rank

    def as_source(self, index: int) -> dict[str, object]:
        """The UI-facing shape. `index` is the citation number."""
        return {
            "index": index,
            "title": self.title or self.domain or self.url,
            "url": self.url,
            "snippet": self.snippet[:280],
            "domain": self.domain,
            # Provenance, so the reader can judge a source without opening it,
            # and so "which engine found this" is answerable after the fact.
            "engines": list(self.engines),
            "tier": self.tier,
            "tier_label": self.tier_label,
            "kind": self.kind,
            "published": self.published,
            "read": bool(self.content),
            "image": self.image,
            "thumbnail": self.thumbnail or self.image,
            **({"passages": list(self.passages)} if self.passages else {}),
            **({"evidence": list(self.evidence_texts)} if self.evidence_texts else {}),
            **({"byline": self.byline} if self.byline else {}),
        }


# ------------------------------------------------------------------- normalising

# Trailing site brand: `… | Shaping Europe's digital future - European Union`.
# Only stripped when the tail looks like the publisher, never blindly.
_BRAND_TAIL = re.compile(r"\s*[|·•‧–—-]\s*([^|·•‧–—]{2,45})\s*$")
# An engine's own truncation, and the dangling word it usually leaves behind.
_TRUNCATED = re.compile(r"[\s.]*(\.{2,}|…)\s*$")
_DANGLING = re.compile(
    r"\s+(and|or|the|a|an|of|for|with|in|on|to|by|from|at|as|is|are|new|its|their)$",
    re.IGNORECASE,
)
# `[PDF]`, `PDF `, `(PDF)`: the engine's document marker, not part of the title.
_PDF_MARKER = re.compile(r"^\s*[\[(]?\s*pdf\s*[\])]?\s*[-–—:]?\s*", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")

TITLE_MAX = 120


def clean_title(raw: str, *, domain: str = "") -> tuple[str, bool]:
    """Return (title, looks_like_pdf).

    Deterministic and conservative: it removes markers engines add and brands
    that repeat the domain, and it never rewrites the words an author chose.
    """
    title = html.unescape(raw or "")
    title = _WHITESPACE.sub(" ", title).strip().strip("|-–—·").strip()

    is_pdf = bool(_PDF_MARKER.match(title))
    if is_pdf:
        title = _PDF_MARKER.sub("", title, count=1)

    title = _strip_brand(title, domain)

    if _TRUNCATED.search(title):
        title = _TRUNCATED.sub("", title)
        # `… New 2026 to 20`: the engine cut mid-token, so the fragment goes too.
        title = _DANGLING.sub("", title)

    if len(title) > TITLE_MAX:
        head = title[:TITLE_MAX]
        cut = head.rfind(" ")
        title = f"{head[:cut] if cut > TITLE_MAX // 2 else head}…"

    return title.strip(), is_pdf


def _strip_brand(title: str, domain: str) -> str:
    """Drop a trailing `| Publisher` when the publisher is the site itself.

    The comparison is on squashed alphanumerics, so `fpf.org` matches `fpf` and
    `European Union` does not match `europa.eu`, which is the point: a brand
    that merely *describes* the publisher is information, a brand that repeats
    the domain shown next to it is noise.
    """
    if not domain:
        return title
    host = _squash(domain.rsplit(".", 1)[0])
    if not host:
        return title
    for _ in range(2):  # `Title | Section | Site` needs two passes at most
        match = _BRAND_TAIL.search(title)
        if not match:
            break
        tail = _squash(match.group(1))
        if not tail or (tail not in host and host not in tail):
            break
        remainder = title[: match.start()].strip()
        if len(remainder) < 12:  # the brand *was* the title
            break
        title = remainder
    return title


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def classify_kind(*, url: str, category: str, template: str, is_pdf: bool, doi: str) -> str:
    if doi or template == "paper.html" or category in ("science", "scientific publications"):
        return KIND_PAPER
    if is_pdf or url.lower().endswith(".pdf"):
        return KIND_PDF
    if category == "news":
        return KIND_NEWS
    return KIND_PAGE


# ---------------------------------------------------------------------- dedupe


def dedupe(results: list[SearchResult]) -> list[SearchResult]:
    """Collapse duplicate URLs, accumulating what the copies prove.

    The same page routinely comes back from several engines and from several
    sub-questions. Paying to fetch and embed it twice is waste, but *discarding*
    the duplicate loses the one signal that separates a page three engines agree
    on from a page one engine found once, so engines and hit counts are merged.
    """
    best: dict[str, SearchResult] = {}
    for result in results:
        key = canonical_url(result.url)
        if not key:
            continue
        current = best.get(key)
        if current is None:
            best[key] = result
            continue
        best[key] = _merge(current, result)
    return list(best.values())


def _merge(current: SearchResult, other: SearchResult) -> SearchResult:
    """Fold `other` into whichever copy is worth keeping."""
    keep, drop = _survivor(current, other)

    keep.engines = _union(keep.engines, drop.engines)
    # Additive: SearXNG already merges duplicates inside one response, so two
    # copies reaching this point came from two different queries, which is a
    # second, independent finding of the same page.
    keep.hits = current.hits + other.hits
    # Max rather than sum: a page every query returns should not out-rank a
    # better page on volume alone. `hits` is where that evidence is recorded.
    keep.score = max(current.score, other.score)
    keep.content = keep.content or drop.content
    keep.snippet = keep.snippet or drop.snippet
    keep.published = keep.published or drop.published
    keep.image = keep.image or drop.image
    keep.thumbnail = keep.thumbnail or drop.thumbnail
    keep.doi = keep.doi or drop.doi
    keep.journal = keep.journal or drop.journal
    keep.authors = keep.authors or drop.authors
    keep.assess()
    return keep


def _survivor(current: SearchResult, other: SearchResult) -> tuple[SearchResult, SearchResult]:
    """Which of two copies of one source is kept. Score decides, except between
    Wikimedia language editions.

    There the score is the wrong judge: it comes from whichever general engine
    ranked the link, and that engine has no opinion about which language the
    reader asked in. SearXNG's own `wikipedia` engine does (it answers in the
    language of the search), so the edition it returned wins over a
    higher-scored link to the same article in a language nobody asked for.
    """
    ranked = (current, other) if current.score >= other.score else (other, current)
    if current.domain == other.domain:
        return ranked

    native = [result for result in ranked if "wikipedia" in result.engines]
    if len(native) == 1:
        keep = native[0]
        return keep, (other if keep is current else current)
    return ranked


def _union(first: list[str], second: list[str]) -> list[str]:
    seen = list(first)
    for name in second:
        if name not in seen:
            seen.append(name)
    return seen


# A source that lives in this deployment rather than on the web: an earlier
# answer, retrieved from the chat's project. Relative, because the
# transcript is stored and may be read from a different host than wrote it,
# which is exactly why the http-only rule below has to be told about it.
INTERNAL_PREFIX = "/#chat="
# A page of a PDF in a literature project's library (`search/library.py`).
# Relative for the same reason, and a source of its own: each page is cited.
LIBRARY_PREFIX = "/library/"


# Wikimedia's language editions are one article behind a hostname per language,
# and a single query routinely returns two or three of them: `de.wikipedia.org`
# and `fi.wikipedia.org` for the same title. They are not independent sources:
# citing both spends two numbers on one encyclopaedia, inflates the agreement
# signal that is supposed to mean *independent* corroboration, and hands the
# writer an article in a language the reader did not ask a question in. Same
# family and same article title is one source.
_WIKI_FAMILIES = (
    "wikipedia.org",
    "wikibooks.org",
    "wikinews.org",
    "wikiquote.org",
    "wikisource.org",
    "wikiversity.org",
    "wikivoyage.org",
)


def _wiki_family(host: str) -> str:
    """`de.wikipedia.org` → `wikipedia.org`, for a host that is a language edition.

    Only a single-label subdomain counts, which is what a language code is.
    `commons.wikimedia.org` is not in the families above and keeps its own
    identity: Commons is a media archive, not an edition of anything.
    """
    for family in _WIKI_FAMILIES:
        prefix = host.removesuffix(family).rstrip(".")
        if host.endswith(family) and prefix and "." not in prefix:
            return family
    return ""


def canonical_url(url: str) -> str:
    """Identity for deduplication: host without `www.`, path without a trailing
    slash, everything else ignored. Returns "" for anything not worth fetching.

    Wikimedia language editions collapse onto their family, so the same article
    in three languages is one source rather than three.
    """
    if url.startswith((INTERNAL_PREFIX, LIBRARY_PREFIX)):
        return url
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    host = parsed.hostname.removeprefix("www.")
    path = parsed.path.rstrip("/")
    return f"{_wiki_family(host) or host}{path}"


# --------------------------------------------------------------------- ordering

# Two pages from one site is a section and its subsection; five is a sitemap.
MAX_PER_DOMAIN = 2


def keep_relevant(results: list[SearchResult], query: str) -> tuple[list[SearchResult], int]:
    """Drop results that are about something else. Returns (kept, dropped).

    This runs before provenance is weighed, and it has to: a tier says how much
    to trust a source *about the subject*, and a book catalogue answering "the
    ship of Theseus" with "The pig that wants to be eaten" is a genuine reference
    work about the wrong thing. Trusting it because of what it is, rather than
    dropping it for what it says, is how ten irrelevant sources end up numbered
    next to an answer.

    Nothing is kept when nothing clears the bar. "Weak sources labelled as such
    beat an empty page" sounds reasonable and holds right up until the engines
    start refusing: a search that comes back with pizza
    chains for a question about the EU AI Act has not found weak evidence, it
    has found none, and numbering it produces an answer whose every citation
    points at something unrelated. The empty path is honest and already exists:
    the run says no usable sources were found and answers from the model.

    The bar is set by the best result rather than by a constant, because a
    constant has to be wrong in one direction or the other for every corpus it
    meets. Set high enough to drop a weather report from an AI Act search, it
    also dropped five of the six results for "Wer ist Zohran Mamdani", leaving
    a run that cited one source and an answer with nothing to corroborate it.
    """
    scores = quality.relevance_scores(query, [f"{r.title} {r.snippet}" for r in results])
    for result, score in zip(results, scores, strict=True):
        result.relevance = score
    floor = quality.relevance_floor(scores)
    kept = [r for r in results if r.relevance >= floor]
    return kept, len(results) - len(kept)


def order(results: list[SearchResult], *, prefer_recent: bool = False) -> list[SearchResult]:
    """Best evidence first, with no domain allowed to run away with the answer.

    The cap is the one hard rule in the ranking: six pages from the same site
    corroborate nothing, however well each one scores, and an answer sourced to a
    single domain cannot be checked against anything.

    Nothing about presentation enters here. Ordering decides which evidence the
    answer is written from and which citation number each source gets. Letting
    a picture-bearing blog outrank an encyclopaedia because the run wants an
    image to show would trade the accuracy of the answer for the look of it.
    The picture is chosen separately, from
    whichever sources happen to carry one, at whatever number they landed on.
    """
    for result in results:
        result.score_rank(prefer_recent=prefer_recent)
    ranked = sorted(results, key=lambda r: r.rank, reverse=True)

    taken: list[SearchResult] = []
    overflow: list[SearchResult] = []
    per_domain: dict[str, int] = {}
    for result in ranked:
        domain = result.domain
        # No host is a source from this install, a project's chat or a page of
        # a paper, and the cap is about sites. A paper's pages are capped where
        # the library hands them out.
        if not domain:
            taken.append(result)
            continue
        count = per_domain.get(domain, 0)
        if count >= MAX_PER_DOMAIN:
            overflow.append(result)
            continue
        per_domain[domain] = count + 1
        taken.append(result)
    return taken + overflow
