"""Which corpora a run is allowed to search.

Depth decides *how much* work a run does. A lens decides *where it looks*, and
that is a different axis: "is creatine worth taking" wants PubMed and Cochrane,
"what changed in the AI Act" wants EUR-Lex and a newswire, "why does asyncio
deadlock here" wants the language's own documentation. Searching the general web
for all three is what produces a confident answer built on a supplement shop and
a consultancy blog.

Engines are named explicitly rather than by category alone, for two reasons:

* **Predictability.** A category is whatever the instance happens to enable, so
  the same question can search a different web after an image rebuild. A named
  set is a decision this repo makes and can be read.
* **Reliability.** Several general engines CAPTCHA a self-hosted instance within
  minutes (`startpage`, `brave`, `qwant`), and several good ones are disabled by
  default (`bing`, `mojeek`). Listing them turns both facts into a choice
  instead of a surprise. Unknown names are ignored by SearXNG, so a lens is safe
  against an instance that is missing an engine, and a lens that comes back
  empty is reported, never silently dropped.

Lenses combine. `web + scholar` searches both and ranks the results against each
other, which is exactly what a question with an evidence base and a news cycle
needs.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.search import quality


@dataclass(frozen=True, slots=True)
class Lens:
    id: str
    label: str
    description: str
    # SearXNG selectors. `engines` is the operative one; `categories` is a
    # fallback for instances where the named engines are absent.
    engines: tuple[str, ...]
    categories: tuple[str, ...] = ()
    # Tier this corpus is expected to yield, used to explain the choice in the
    # UI rather than to score anything; scoring reads the actual domain.
    expects: str = quality.WEB
    # A line for the classifier, so queries are phrased the way this corpus
    # indexes: keyword soup for the web, terminology for a paper database.
    query_hint: str = ""
    # Whether a recency window is meaningless here, and destructive for being
    # applied. A time range is a *filter* at the engine, not a preference in the
    # ranking: what it excludes cannot be ranked back in. On the open web that is
    # usually what was wanted. In a paper database it is a different question:
    # "restricted to the past year" turns the state of the evidence into this
    # year's publications, and the 2005 paper that answers the question is simply
    # not in the response. The ranking's own freshness term still applies to
    # these corpora; only the hard cut does not.
    timeless: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "expects": self.expects,
            "tier_label": quality.TIER_LABELS.get(self.expects, "Web"),
        }


WEB = Lens(
    id="web",
    label="Web",
    description="General search across several independent engines.",
    # `fireball` is here for breadth rather than quality: a self-hosted instance
    # loses engines under load: scraping from one address gets Brave, Qwant,
    # Startpage and Google CSE to rate-limit or CAPTCHA within a working day,
    # measured at one of six still answering, and a run that falls through to a
    # single survivor cites whatever that one happened to have. It needs no API
    # key, indexes German, and fails independently of the majors. Anything more
    # than this wants real keys; see README.
    engines=(
        "google cse",
        "duckduckgo",
        "bing",
        "mojeek",
        "brave",
        "startpage",
        "qwant",
        "wikipedia",
        "fireball",
    ),
    categories=("general",),
    expects=quality.WEB,
    query_hint="Short keyword queries, the way you would type them into a search box.",
)

SCHOLAR = Lens(
    id="scholar",
    label="Science",
    description="Peer-reviewed papers and preprints: PubMed, arXiv, Crossref, OpenAlex.",
    engines=(
        "arxiv",
        "crossref",
        "pubmed",
        "semantic scholar",
        "openalex",
        "google scholar",
        "openairepublications",
    ),
    categories=("science",),
    expects=quality.ACADEMIC,
    query_hint=(
        "Write these in English whatever language the question is in: Crossref, "
        "arXiv, OpenAlex and PubMed index English titles and abstracts, and a "
        "German query returns whatever happened to share a word with it. Use the "
        "field's own terminology and name the population, intervention or "
        "mechanism. These are bibliographic databases, not a search box."
    ),
    timeless=True,
)

NEWS = Lens(
    id="news",
    label="News",
    description="Newswires and established newsrooms, for anything still developing.",
    engines=(
        "reuters",
        "bing news",
        "duckduckgo news",
        "google news",
        "mojeek news",
        "wikinews",
        "tagesschau",
    ),
    categories=("news",),
    expects=quality.NEWS,
    query_hint="Name the event, the organisation and the period being asked about.",
)

DOCS = Lens(
    id="docs",
    label="Docs & code",
    description="Official documentation, standards and source: MDN, GitHub, Stack Overflow.",
    engines=(
        "github",
        "stackoverflow",
        "mdn",
        "microsoft learn",
        "arch linux wiki",
        "askubuntu",
        "superuser",
        "hackernews",
        "lobste.rs",
        "docker hub",
        "pypi",
        "npm",
    ),
    categories=("it",),
    expects=quality.TECHNICAL,
    query_hint="Name the exact API, error message, version or package.",
)

REFERENCE = Lens(
    id="reference",
    label="Reference",
    description="Encyclopaedias and definitions, for concepts and background.",
    # Wikipedia first, and then a general engine, but *not* the rest of
    # the Wikimedia family. Openlibrary is a book catalogue, Wikisource is scanned
    # primary texts and Wikibooks is textbooks; all three keyword-match anything,
    # so asking them for "the ship of Theseus" returns books that merely contain
    # the words. Wikipedia answers only through its infobox (see
    # `searxng._results_of`), so a general engine has to back it up or this lens
    # returns nothing at all on any subject without an exact-title article.
    #
    # Wiktionary belongs to that same family and is the worst of it: it matches
    # a *word*, so "wie wird das Wetter" returns the dictionary entry for "wie".
    # It was named here while the instance had it disabled, which is the only
    # reason it never showed, enabling the engine turned this line into
    # dictionary entries in the source list of every reference question.
    engines=("wikipedia", "duckduckgo", "google cse", "bing", "mojeek"),
    categories=("general",),
    expects=quality.REFERENCE,
    query_hint=(
        "Name the subject as an encyclopaedia would title its article: the bare "
        "concept, without question words."
    ),
    # An encyclopaedia article is not stale for being old, and the article on a
    # subject is *the* article on it whenever it was last edited. Note that this
    # only takes effect where the corpus has engines of its own to search:
    # every general engine here is also named by the web lens, and an engine in
    # a windowed corpus keeps the window (see `searxng._request_groups`).
    timeless=True,
)

DATA = Lens(
    id="data",
    label="Data",
    description="Computed figures: prices, conversions, statistics, with their as-of date.",
    # Wolfram Alpha is the one engine here that answers a quantity *as a
    # quantity* ("$302.25 (AAPL | NASDAQ | 12 August 2026)") rather than
    # linking to a page that has it somewhere. It carries its own as-of date,
    # which is exactly what a figure needs and what a web page rarely states.
    # The weather engines (`duckduckgo weather`, `wttr.in`) crash on this SearXNG
    # release, so weather still goes through the general web.
    engines=("wolframalpha", "currency", "google cse", "bing", "duckduckgo"),
    categories=("general",),
    expects=quality.PRIMARY,
    query_hint=(
        "Name the quantity the way a calculator would take it (`AAPL stock "
        "price`, `100 USD in EUR`, `population of Berlin`), not as a sentence."
    ),
)

IMAGES = Lens(
    id="images",
    label="Images",
    description="Pictures, with the page and the licence they came from.",
    # Wikimedia Commons first: for anything factual (a flag, a species, a
    # building) it is the source with a provenance, while the image engines
    # return whichever wallpaper site ranked. Ordering here does not decide the
    # ranking (provenance does), but it decides who is asked.
    engines=(
        "wikicommons.images",
        "openverse",
        "duckduckgo images",
        "bing images",
        "google cse images",
    ),
    categories=("images",),
    expects=quality.REFERENCE,
    query_hint=(
        "Name the thing itself as an image caption would (`flag of Argentina`, "
        "`Andean condor`), with no question words."
    ),
)

# A literature project's own papers (`search/library.py`). Not in `LENSES`: it
# cannot be picked, it is what a chat in such a project searches, and it has no
# SearXNG engines, so nothing that iterates the catalogue may ever send it
# there. It is a lens at all so the classifier writes queries for it and the
# footer names it like any other corpus.
LIBRARY = Lens(
    id="library",
    label="Your papers",
    description="The full texts and abstracts of the papers you marked relevant.",
    engines=(),
    expects=quality.ACADEMIC,
    query_hint=(
        "These are the person's own papers, mostly in English: write the queries in "
        "English, in the field's terminology, one concept or claim per query, the way "
        "the papers themselves would phrase it."
    ),
    timeless=True,
)

LENSES: dict[str, Lens] = {
    lens.id: lens for lens in (WEB, SCHOLAR, NEWS, DOCS, REFERENCE, DATA, IMAGES)
}
# The web lens already carries Wikipedia, so the default needs nothing else.
DEFAULT_LENSES: tuple[str, ...] = (WEB.id,)

# Not a corpus: the selection "let the classifier pick". It travels as an id
# because that is what the picker, the settings row and the API already carry,
# and it is not in `LENSES`: nothing may ever *search* it.
AUTO = "auto"

# What the classifier may choose from when it does. Images are excluded on
# purpose: pictures are retrieved by their own search when the answer wants one
# (`research.search_images`), and letting them into the evidence means the
# answer gets written from stock-photo pages.
CHOOSABLE: tuple[str, ...] = (WEB.id, SCHOLAR.id, NEWS.id, DOCS.id, REFERENCE.id, DATA.id)


def wants_auto(ids: list[str] | tuple[str, ...] | None) -> bool:
    return AUTO in (ids or ())


def normalise(ids: list[str] | tuple[str, ...] | None) -> list[str]:
    """The ids to store and echo back. `auto` survives; everything else resolves.

    Kept apart from `resolve` because the two answer different questions: this
    one is what the user chose, `resolve` is what a search may draw on.
    """
    return [AUTO] if wants_auto(ids) else ids_of(resolve(ids))


def choices() -> str:
    """The corpora the classifier picks between, as prompt text."""
    return "\n".join(
        f'  - "{LENSES[i].id}": {LENSES[i].label}, {LENSES[i].description}' for i in CHOOSABLE
    )


def resolve(ids: list[str] | tuple[str, ...] | None) -> list[Lens]:
    """Lens objects for a set of ids, in catalogue order.

    Unknown ids are dropped and an empty selection falls back to the default:
    an old client, or a hand-written API call, must degrade rather than search
    nothing and blame the model for the empty answer.
    """
    chosen = [LENSES[i] for i in LENSES if i in set(ids or ())]
    return chosen or [LENSES[i] for i in DEFAULT_LENSES]


def ids_of(lenses: list[Lens]) -> list[str]:
    return [lens.id for lens in lenses]


def catalog() -> list[dict[str, object]]:
    """What the UI needs to render the source selector, from the definitions the
    searches actually run on."""
    return [lens.as_dict() for lens in LENSES.values()]


def query_hints(lenses: list[Lens]) -> str:
    """The classifier's instructions for the selected corpora."""
    return "\n".join(f"- {lens.label}: {lens.query_hint}" for lens in lenses if lens.query_hint)
