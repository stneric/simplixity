"""What a source is worth before anything has been read.

A search engine ranks for *relevance to a query*. Research needs relevance to a
*question*, which is not the same thing: the page that best matches "EU AI Act
2026 changes" is frequently a consultancy's lead-generation post that paraphrases
the regulation, while the regulation itself sits three results lower. Handing the
first N results to the model is how an answer ends up sourced to
`euaiactchecklist.com` when `eur-lex.europa.eu` was on the same page of results.

So every result is assessed on evidence available *without* fetching it:

* **Provenance.** Who publishes it. A statute on a `.europa.eu` host, a paper
  with a DOI and a marketing post are not interchangeable, and the tier is worth
  showing the user as well as the ranker.
* **Agreement.** How many independent engines returned it, and how many of the
  run's queries it answered. One engine finding one page proves very little;
  three engines and two queries agreeing is the cheapest corroboration there is.
* **Position.** SearXNG's own merged score, which already folds in each engine's
  ranking. Trusted but not decisive: it is what over-ranks the content farm.
* **Freshness**, only when the question is time-sensitive. An undated page is
  not evidence about what changed this year.

None of this is a blocklist. Junk is *out-ranked* rather than banned, because a
question about a consultancy's opinion should still be able to find it. The one
hard rule is the per-domain cap: no single site may occupy the whole answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

# ---------------------------------------------------------------------- tiers
# Ordered best to worst. The id is what the UI badges and the prompt both use,
# so a source's provenance is legible to the reader and to the model.

PRIMARY = "primary"
ACADEMIC = "academic"
TECHNICAL = "technical"
NEWS = "news"
REFERENCE = "reference"
PROJECT = "project"
WEB = "web"
AGGREGATOR = "aggregator"

TIER_TRUST = {
    PRIMARY: 1.00,  # the thing itself: legislation, a regulator, the vendor's own docs
    ACADEMIC: 0.92,  # peer-reviewed or a preprint with a DOI
    TECHNICAL: 0.80,  # official documentation, standards, source code
    REFERENCE: 0.70,  # encyclopaedic summary of something else
    NEWS: 0.68,  # an established newsroom reporting on it
    # An answer this deployment wrote earlier, retrieved from a project. Above
    # an unknown page because it was researched and cited at the time, and well
    # below anything primary because it is a *derivation*. Trusting it as
    # evidence would let one run's mistake become the next run's premise.
    PROJECT: 0.55,
    WEB: 0.42,  # an ordinary page of unknown provenance
    AGGREGATOR: 0.18,  # UGC, scrapers, social, slide dumps
}

TIER_LABELS = {
    PRIMARY: "Official",
    ACADEMIC: "Peer-reviewed",
    TECHNICAL: "Docs",
    REFERENCE: "Reference",
    NEWS: "News",
    PROJECT: "This project",
    WEB: "Web",
    AGGREGATOR: "Aggregator",
}

# ------------------------------------------------------------------- registries
# Curated because there is no algorithm for "this is the authority on the
# subject". Kept short: the suffix and prefix rules below generalise,
# and the list only has to cover what a generic rule cannot.

_PRIMARY_SUFFIXES = (
    ".gov",
    ".mil",
    ".int",
    ".gov.uk",
    ".gov.au",
    ".gov.ca",
    ".gc.ca",
    ".gov.in",
    ".gov.br",
    ".gouv.fr",
    ".admin.ch",
    ".europa.eu",
    ".bund.de",
    ".gv.at",
)

_PRIMARY_HOSTS = frozenset(
    {
        "eur-lex.europa.eu",
        "europarl.europa.eu",
        "consilium.europa.eu",
        "ec.europa.eu",
        "edps.europa.eu",
        "who.int",
        "un.org",
        "unesco.org",
        "oecd.org",
        "imf.org",
        "worldbank.org",
        "wto.org",
        "nato.int",
        "iso.org",
        "iec.ch",
        "bis.doc.gov",
        "gesetze-im-internet.de",
        "bundesanzeiger.de",
        "legislation.gov.uk",
        "supremecourt.gov",
        "federalregister.gov",
        "sec.gov",
        "fda.gov",
        "cdc.gov",
        "nih.gov",
        "ecb.europa.eu",
        "bundesbank.de",
        "destatis.de",
        "eurostat.ec.europa.eu",
    }
)

_ACADEMIC_SUFFIXES = (".edu", ".ac.uk", ".edu.au", ".ac.jp", ".ac.at", ".edu.cn", ".ac.in")

_ACADEMIC_HOSTS = frozenset(
    {
        "arxiv.org",
        "biorxiv.org",
        "medrxiv.org",
        "ssrn.com",
        "papers.ssrn.com",
        "doi.org",
        "dx.doi.org",
        "pubmed.ncbi.nlm.nih.gov",
        "ncbi.nlm.nih.gov",
        "pmc.ncbi.nlm.nih.gov",
        "nature.com",
        "science.org",
        "sciencedirect.com",
        "springer.com",
        "link.springer.com",
        "wiley.com",
        "onlinelibrary.wiley.com",
        "tandfonline.com",
        "cambridge.org",
        "academic.oup.com",
        "plos.org",
        "journals.plos.org",
        "frontiersin.org",
        "mdpi.com",
        "jstor.org",
        "dl.acm.org",
        "acm.org",
        "ieeexplore.ieee.org",
        "ieee.org",
        "semanticscholar.org",
        "openalex.org",
        "crossref.org",
        "cochranelibrary.com",
        "nejm.org",
        "thelancet.com",
        "bmj.com",
        "jamanetwork.com",
        "elifesciences.org",
        "openreview.net",
        "proceedings.neurips.cc",
        "aclanthology.org",
    }
)

_TECHNICAL_HOSTS = frozenset(
    {
        "github.com",
        "gitlab.com",
        "codeberg.org",
        "stackoverflow.com",
        "developer.mozilla.org",
        "learn.microsoft.com",
        "docs.microsoft.com",
        "rfc-editor.org",
        "datatracker.ietf.org",
        "ietf.org",
        "w3.org",
        "whatwg.org",
        "kernel.org",
        "man7.org",
        "postgresql.org",
        "sqlite.org",
        "python.org",
        "nodejs.org",
        "react.dev",
        "developer.apple.com",
        "developer.android.com",
        "cloud.google.com",
        "developers.google.com",
        "pytorch.org",
        "huggingface.co",
        "kubernetes.io",
        "docker.com",
        "rust-lang.org",
        "go.dev",
        "mariadb.com",
        "redis.io",
        "nginx.org",
    }
)

# `docs.*`, `developer.*`, `*.readthedocs.io`: a vendor's own documentation is
# a primary source about its own product, whoever the vendor is.
_TECHNICAL_PREFIXES = ("docs.", "developer.", "dev.", "devdocs.", "api.")
_TECHNICAL_SUFFIXES = (".readthedocs.io", ".readthedocs.org", ".github.io", ".gitbook.io")

_NEWS_HOSTS = frozenset(
    {
        "reuters.com",
        "apnews.com",
        "afp.com",
        "bloomberg.com",
        "ft.com",
        "wsj.com",
        "nytimes.com",
        "washingtonpost.com",
        "theguardian.com",
        "bbc.com",
        "bbc.co.uk",
        "economist.com",
        "politico.eu",
        "politico.com",
        "npr.org",
        "cnbc.com",
        "axios.com",
        "arstechnica.com",
        "theverge.com",
        "wired.com",
        "technologyreview.com",
        "spiegel.de",
        "zeit.de",
        "faz.net",
        "sueddeutsche.de",
        "handelsblatt.com",
        "tagesschau.de",
        "heise.de",
        "golem.de",
        "netzpolitik.org",
        "nzz.ch",
        "derstandard.at",
        "lemonde.fr",
        "elpais.com",
        "corriere.it",
    }
)

_REFERENCE_HOSTS = frozenset(
    {
        "wikipedia.org",
        "wikimedia.org",
        "commons.wikimedia.org",
        "wikidata.org",
        "wikisource.org",
        "wikibooks.org",
        # No wiktionary: a dictionary is listed with the aggregators below,
        # because it matches a word rather than a subject.
        "britannica.com",
        "plato.stanford.edu",
        "openlibrary.org",
        "wolframalpha.com",
    }
)

# Demoted, not excluded: these are real pages, but a research answer sourced to
# a slide dump or a Pinterest board is not one the user can check.
_AGGREGATOR_HOSTS = frozenset(
    {
        "pinterest.com",
        "quora.com",
        "answers.com",
        "ask.com",
        "scribd.com",
        "coursehero.com",
        "studocu.com",
        "chegg.com",
        "slideshare.net",
        "academia.edu",
        "researchgate.net",
        "linkedin.com",
        "facebook.com",
        "instagram.com",
        "tiktok.com",
        "x.com",
        "twitter.com",
        "youtube.com",
        "youtu.be",
        "reddit.com",
        "medium.com",
        "blogspot.com",
        "wordpress.com",
        "wixsite.com",
        "weebly.com",
        "webflow.io",
        "tumblr.com",
        "issuu.com",
        "yumpu.com",
        # Dictionaries. They match a *word*, so any question long enough to
        # contain a common one retrieves the entry for it: "wie wird das
        # Wetter morgen" came back with the definition of "wie". A definition
        # is occasionally the answer and never the evidence for one, so this
        # demotes rather than blocks.
        "wiktionary.org",
        "dict.cc",
        "dictionary.com",
        "wordreference.com",
        "linguee.com",
        "leo.org",
        # Stock libraries. A picture search must be able to reach them, and an
        # answer must never rest on them: the page around the photograph is
        # keywords and a price. The picture widget is told to prefer archival
        # sources, and this is that preference expressed where ranking can act
        # on it rather than left to the model.
        "istockphoto.com",
        "shutterstock.com",
        "gettyimages.com",
        "gettyimages.de",
        "alamy.com",
        "alamy.de",
        "dreamstime.com",
        "depositphotos.com",
        "123rf.com",
        "freepik.com",
        "pexels.com",
        "unsplash.com",
        "vecteezy.com",
    }
)

# Titles shaped by SEO rather than by an author. A soft penalty only: plenty of
# legitimate pages carry a "what you need to know" headline.
_SEO_TITLE = re.compile(
    r"(ultimate|complete|comprehensive|definitive|beginner'?s)\s+guide"
    r"|\btop\s+\d+\b"
    r"|\b\d+\s+(best|things|tips|ways|reasons|steps)\b"
    r"|everything you need to know"
    r"|\((updated|guide)\s*20\d\d\)",
    re.IGNORECASE,
)

# A hostname assembled out of the query's own keywords: the exact-match domain
# an affiliate registers to rank for one search. Three or more hyphenated words
# before the TLD is the tell.
_KEYWORD_DOMAIN = re.compile(r"^[a-z]{2,}(-[a-z]{2,}){2,}$")


@dataclass(frozen=True, slots=True)
class Assessment:
    """Why a source scored what it did. Carried onto the result so both the
    ranker and the UI work from the same judgement."""

    tier: str
    trust: float
    reasons: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return TIER_LABELS.get(self.tier, TIER_LABELS[WEB])


def assess(
    domain: str,
    *,
    title: str = "",
    url: str = "",
    category: str = "",
    doi: str = "",
    journal: str = "",
) -> Assessment:
    """Classify a source by provenance, before it has been read.

    `domain` is the hostname without `www.`. The academic signals (a DOI, a
    journal name, the science category) outrank the hostname: a paper hosted on
    a university's own server is a paper, not a `.edu` homepage.
    """
    host = (domain or "").lower().removeprefix("www.")
    reasons: list[str] = []

    if doi or journal or category in ("science", "scientific publications"):
        reasons.append("indexed publication")
        return Assessment(ACADEMIC, TIER_TRUST[ACADEMIC], tuple(reasons))

    tier = _tier_of(host, reasons)
    trust = TIER_TRUST[tier]

    # Adjustments. Each is small; together they separate a hand-written page
    # from one assembled to rank for a keyword.
    if tier in (WEB, NEWS) and _SEO_TITLE.search(title or ""):
        trust *= 0.72
        reasons.append("SEO-shaped title")
    if tier == WEB and _KEYWORD_DOMAIN.match(_registrable_label(host)):
        trust *= 0.75
        reasons.append("keyword domain")
    if tier == WEB and url.lower().endswith(".pdf"):
        # A PDF on an unknown host is usually a whitepaper or a slide deck, and
        # is at least a document rather than a page of links.
        trust *= 1.08
        reasons.append("document")

    return Assessment(tier, round(min(trust, 1.0), 3), tuple(reasons))


def _tier_of(host: str, reasons: list[str]) -> str:
    if _matches(host, _PRIMARY_HOSTS, _PRIMARY_SUFFIXES):
        reasons.append("official publisher")
        return PRIMARY
    if _matches(host, _ACADEMIC_HOSTS, _ACADEMIC_SUFFIXES):
        reasons.append("academic publisher")
        return ACADEMIC
    if (
        _matches(host, _TECHNICAL_HOSTS, _TECHNICAL_SUFFIXES)
        or host.startswith(_TECHNICAL_PREFIXES)
        or ".docs." in host
    ):
        reasons.append("official documentation")
        return TECHNICAL
    if _matches(host, _REFERENCE_HOSTS, ()):
        reasons.append("reference work")
        return REFERENCE
    if _matches(host, _NEWS_HOSTS, ()):
        reasons.append("established newsroom")
        return NEWS
    if _matches(host, _AGGREGATOR_HOSTS, ()):
        reasons.append("user-generated or aggregated")
        return AGGREGATOR
    return WEB


def _registrable_label(host: str) -> str:
    """The name the owner actually registered: `eu-ai-act-guide` out of
    `blog.eu-ai-act-guide.com`. Subdomains and the TLD carry no signal here."""
    without_tld = host.rsplit(".", 1)[0]
    return without_tld.rsplit(".", 1)[-1]


def _matches(host: str, hosts: frozenset[str], suffixes: tuple[str, ...]) -> bool:
    """True if `host` is one of `hosts`, a subdomain of one, or ends in one of
    `suffixes`. Subdomains count: `digital-strategy.ec.europa.eu` is the EU."""
    if host in hosts:
        return True
    if any(host.endswith(f".{known}") for known in hosts):
        return True
    return host.endswith(suffixes) if suffixes else False


# --------------------------------------------------------------------- ranking

# Weights sum to 1.0. Trust leads, but not so far that a mediocre official page
# beats a well-corroborated one; `position` is the engines' own opinion and is
# deliberately the smallest term that still matters.
_W_TRUST = 0.38
_W_AGREEMENT = 0.20
_W_RELEVANCE = 0.16
_W_POSITION = 0.16
_W_FRESHNESS = 0.10

# Above this many merged engines/queries, more agreement tells us nothing new.
_AGREEMENT_SATURATION = 3.0
# SearXNG's merged score is roughly a sum of reciprocal ranks; ~2.0 is a result
# every engine put near the top.
_POSITION_SATURATION = 2.0

_FRESH_DAYS = 400
_STALE_DAYS = 1500


def rank_score(
    *,
    trust: float,
    engines: int,
    hits: int,
    engine_score: float,
    relevance: float = 1.0,
    published: str = "",
    prefer_recent: bool = False,
) -> float:
    """One number, 0..1, for "put this in front of the model first".

    `hits` is how many of the run's queries returned this URL, independent from
    `engines`, and just as much a corroboration signal.

    `relevance` is how much of the question's subject the result covers, and it
    belongs *here* rather than only in the gate that precedes ranking. Two cases
    are indistinguishable by word frequency alone: results that share only the
    year in "EU AI Act 2026", which are noise, and results that share only the
    surname in "Wer ist Zohran Mamdani", which are the answer. A gate has to
    guess, and guessing wrong either way is expensive: drop and the run has one
    source, keep and a marketing page holds a citation. Ranked, both are simply
    ordered: the full match takes [1] and the partial one competes for what is
    left of the source budget.
    """
    agreement = min(1.0, ((engines - 1) + (hits - 1) * 1.5) / _AGREEMENT_SATURATION)
    position = min(1.0, max(0.0, engine_score) / _POSITION_SATURATION)
    subject = min(1.0, max(0.0, relevance))

    if not prefer_recent:
        # Nothing to weigh freshness against, so the weight goes to provenance
        # rather than being silently dropped.
        return round(
            (_W_TRUST + _W_FRESHNESS) * trust
            + _W_AGREEMENT * agreement
            + _W_RELEVANCE * subject
            + _W_POSITION * position,
            4,
        )

    return round(
        _W_TRUST * trust
        + _W_AGREEMENT * agreement
        + _W_RELEVANCE * subject
        + _W_POSITION * position
        + _W_FRESHNESS * freshness(published),
        4,
    )


# ------------------------------------------------------------------- relevance

# Two characters, not three. "EU", "AI", "GDP", "C++": an acronym is very often
# the entire subject of a question, and dropping it leaves nothing behind but the
# question's own scaffolding ("changed", "act", "year"), which matches anything.
_REL_WORD = re.compile(r"[^\W_]{2,}", re.UNICODE)
# Function words in the languages this is likely to see. Not a real stop list:
# just enough that "was ist das Schiff von Theseus" reduces to {schiff, theseus}.
# What it misses, the corpus-relative pass below catches anyway.
_REL_STOP = frozenset(
    [
        # English
        "the",
        "and",
        "or",
        "of",
        "to",
        "in",
        "for",
        "on",
        "with",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "it",
        "its",
        "this",
        "that",
        "as",
        "at",
        "by",
        "from",
        "how",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "does",
        "do",
        "did",
        "can",
        "could",
        "should",
        "would",
        "will",
        "about",
        "into",
        "than",
        "then",
        "explain",
        "tell",
        "please",
        # German
        "der",
        "die",
        "das",
        "den",
        "dem",
        "des",
        "ein",
        "eine",
        "einer",
        "eines",
        "und",
        "oder",
        "von",
        "vom",
        "mit",
        "ist",
        "sind",
        "war",
        "waren",
        "sein",
        "wird",
        "werden",
        "für",
        "auf",
        "aus",
        "bei",
        "nach",
        "über",
        "unter",
        "wie",
        "wer",
        "wann",
        "warum",
        "welche",
        "welcher",
        "man",
        "sich",
        "nicht",
        "auch",
        "erklär",
        "erkläre",
        "erklaere",
        "mir",
        "bitte",
        # Two-letter function words, now that the floor admits them.
        "an",
        "am",
        "im",
        "zu",
        "so",
        "ob",
        "um",
        "je",
        "we",
        "us",
        "my",
        "no",
        "up",
        "if",
        "or",
    ]
)

# The absolute floor: below this share of the subject, nothing is worth keeping
# whatever else came back. It is deliberately near the bottom, because the bar
# that does the real work is relative; see `relevance_floor`.
MIN_RELEVANCE = 0.15

# And the relative one: a result has to reach this share of *the best result
# there is*. An absolute threshold cannot work here, because the same number has
# to serve corpora of different shapes. At 0.34 absolute, "Wer ist Zohran
# Mamdani" kept one source of six: five said "Mamdani", one said "Zohran
# Mamdani", and the rarer word carried enough weight that every result missing
# it fell under the bar. Relative, the six are within a hair of each other and
# all survive, while a weather report that shares only the year with "EU AI Act
# 2026" is half the best result and does not.
_RELATIVE_FLOOR = 0.6


def relevance_floor(scores: list[float]) -> float:
    """The bar this particular set of results has to clear.

    Zero best means nothing matched anything (the engines handed back a corpus
    about something else entirely), and the absolute floor then drops the lot,
    which is what the caller's fallback is for.
    """
    best = max(scores, default=0.0)
    if best <= 0.0:
        return MIN_RELEVANCE
    return max(MIN_RELEVANCE, best * _RELATIVE_FLOOR)


def relevance_scores(query: str, documents: list[str]) -> list[float]:
    """Score each document 0..1 on how much of the question's *subject* it covers.

    Corpus-relative, and that is the whole design. A fixed stop list cannot know
    that "worth" and "taking" in "is creatine worth taking?" are phrasing while
    "creatine" is the subject, but the results can: a term no result contains is
    not what anyone is writing about, so it is phrasing, and requiring it would
    fail every paper on the topic and leave the fallback to keep the lot.

    A term is therefore weighted by **how much of the corpus it appears in**, and
    one that appears in none of it is dropped. What survives is the subject, and
    a document is scored on the share of the subject it accounts for.

    This is the inverse of IDF. In IDF rare terms count for more,
    because the corpus is the whole web and rarity means information. Here
    the corpus is one page of results for one query, and rarity means the
    opposite: a term that only one result carries is the incidental one, and
    making it dominant punishes every result that does not. "Wer ist Zohran
    Mamdani" is the case that showed it. Six results, all about him; five said
    "Mamdani" and one said "Zohran Mamdani", so `zohran` (appearing once)
    outweighed `mamdani`, the other five scored 0.26 against a floor of 0.34,
    and the answer was written from a single source. The stricter the question's
    subject was matched, the fewer sources survived.

    Returns all-1.0 when the question has no usable subject terms, so a one-word
    or non-Latin query degrades to "keep everything" rather than "drop
    everything".
    """
    terms = _terms(query)
    texts = [_terms(text) for text in documents]
    if not terms or not texts:
        return [1.0] * len(documents)

    # How much of the corpus each term appears in, as a share of it. A term the
    # results agree on is what they are collectively about; one that appears
    # twice in forty is a coincidence, and counts for about as much.
    frequency = {term: sum(1 for text in texts if _has(text, term)) for term in terms}
    weights = {term: count / len(texts) for term, count in frequency.items() if count}
    if not weights:
        # Not one result shares a single subject term with the question. In a
        # healthy search this never happens.
        # It happens when the engines are rate limited and hand back whatever they
        # had: a run about the EU AI Act cited two pizza chains, one about
        # autoencoders cited a protein-design paper, and in both the model then
        # wrote that the sources say nothing about the question. That is worse
        # than no sources, because the empty path answers from the model and
        # says so. Zero here, and the caller falls back.
        return [0.0] * len(documents)

    total = sum(weights.values())
    return [
        round(sum(weight for term, weight in weights.items() if _has(text, term)) / total, 3)
        for text in texts
    ]


def relevance(query: str, title: str, snippet: str = "") -> float:
    """One document against a question, with no corpus to calibrate against.

    Every term counts, so this is the stricter reading, used where there is only
    one document to judge.
    """
    return relevance_scores(query, [f"{title} {snippet}"])[0]


# A query longer than this is a sentence, and a search engine answers a sentence
# by matching whatever it recognises, which for a German sub-question beginning
# "Welche neuen Bestimmungen…" is the word "welche". A run that planned five such
# sub-questions came back with eight sources about German pronouns: Duden, DWDS,
# Wiktionary, and an answer explaining that the sources do not mention the AI Act.
_QUERY_WORDS = 10


def as_query(question: str) -> str:
    """A question reduced to the keywords a search engine can act on.

    The classifier is told to write keyword queries rather than sentences, and
    it does. Nothing told the deep path's planner that, because its output is
    meant to be *read* as questions, so its sub-questions were handed to the
    engines verbatim. This is the same rule applied where that happens.

    Falls back to the original text when reduction leaves nothing: a query in a
    script this tokenizer cannot read must still be searched.
    """
    words = [word for word in _REL_WORD.findall(question or "") if word.lower() not in _REL_STOP]
    return " ".join(words[:_QUERY_WORDS]) or (question or "").strip()


def _terms(text: str) -> set[str]:
    return {word for word in _REL_WORD.findall((text or "").lower()) if word not in _REL_STOP}


# Long enough that a substring match means the same word: `schiff` inside
# `schiffes`. Below it, substrings are coincidences (`eu` inside `europe`,
# `ai` inside `said`), and they are exactly the coincidences that let a weather
# report score as a source about the EU AI Act.
_SUBSTRING_MIN = 4


def _has(text: set[str], term: str) -> bool:
    """Whole-word, plus a substring match for terms long enough to be safe.

    Stemming would be better and needs a dependency; German compounds are the
    case that makes the tolerance worth having at all.
    """
    if term in text:
        return True
    if len(term) < _SUBSTRING_MIN:
        return False
    return any(term in word for word in text)


def freshness(published: str) -> float:
    """1.0 for recent, 0.45 for undated, decaying to 0.1 for old.

    Undated sits above old: most useful pages carry no machine
    readable date, and treating "unknown" as "ancient" would delete the web.
    """
    age = _age_days(published)
    if age is None:
        return 0.45
    if age <= _FRESH_DAYS:
        return 1.0
    if age >= _STALE_DAYS:
        return 0.1
    span = _STALE_DAYS - _FRESH_DAYS
    return round(1.0 - 0.9 * (age - _FRESH_DAYS) / span, 3)


def _age_days(published: str) -> float | None:
    if not published:
        return None
    try:
        when = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - when).total_seconds() / 86400)
