"""The paper databases: OpenAlex, Semantic Scholar, Crossref, arXiv, IEEE Xplore.

Each source is a `search(query, …)` that returns `Paper`s or raises
`SourceError` with a sentence the person can read. Parsing is a separate pure
function per database, so the tests run against recorded JSON and XML with no
network, and the odd shapes (OpenAlex's inverted-index abstracts, Crossref's
JATS, arXiv's Atom) are handled in exactly one place each.

Why these five:

* **OpenAlex** is the broadest open index, it carries most abstracts, and it
  has the citation graph in both directions, which is what snowballing needs.
* **Semantic Scholar** has abstracts and TL;DRs, and the one API built for
  "more like these, not like those": recommendations from positive and
  negative examples.
* **Crossref** is the DOI registry itself, the authority on metadata, but its
  abstracts are patchy, so it ranks as corroboration more than as a read.
* **arXiv** for preprints the others index late or not at all.
* **IEEE Xplore** only with a key, since without one it does not answer. IEEE
  papers still arrive through the others by DOI; Xplore adds the abstracts
  those often lack.

Failures are per source and never raised past the round: one database
rate-limiting is a line in the round's report, not a failed search. The error
text never includes the URL, because the IEEE key travels in the query string.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app import net
from app.config import settings
from app.scholar.paper import (
    AUTHORS_MAX,
    BOOK,
    CONFERENCE,
    JOURNAL,
    OTHER,
    PREPRINT,
    THESIS,
    Paper,
    clean_text,
    normalise_arxiv,
    normalise_doi,
    year_of,
)

log = logging.getLogger(__name__)

OPENALEX = "openalex"
SEMANTIC = "semantic_scholar"
CROSSREF = "crossref"
ARXIV = "arxiv"
IEEE = "ieee"

LABELS = {
    OPENALEX: "OpenAlex",
    SEMANTIC: "Semantic Scholar",
    CROSSREF: "Crossref",
    ARXIV: "arXiv",
    IEEE: "IEEE Xplore",
}

OPENALEX_URL = "https://api.openalex.org/works"
S2_URL = "https://api.semanticscholar.org/graph/v1"
S2_RECOMMEND_URL = "https://api.semanticscholar.org/recommendations/v1/papers"
CROSSREF_URL = "https://api.crossref.org/works"
ARXIV_URL = "https://export.arxiv.org/api/query"
IEEE_URL = "https://ieeexploreapi.ieee.org/api/v1/search/articles"

S2_FIELDS = (
    "paperId,externalIds,title,abstract,year,venue,publicationVenue,authors,"
    "citationCount,tldr,url,openAccessPdf,publicationTypes,journal"
)
# The recommendations endpoint takes fewer fields than search and answers 400
# for the whole request when it meets one it does not know: `tldr` is the one.
S2_RECOMMEND_FIELDS = (
    "paperId,externalIds,title,abstract,year,venue,publicationVenue,authors,"
    "citationCount,url,openAccessPdf,publicationTypes,journal"
)
OPENALEX_SELECT = (
    "id,doi,display_name,publication_year,authorships,primary_location,cited_by_count,"
    "type,type_crossref,abstract_inverted_index,open_access,ids,referenced_works,biblio"
)


class SourceError(RuntimeError):
    """A database could not answer. The message is for the round report."""


Years = tuple[int | None, int | None]


def available() -> list[str]:
    """The sources this install can query, in the order they are asked."""
    names = [OPENALEX, SEMANTIC, CROSSREF, ARXIV]
    if settings.ieee_api_key:
        names.insert(0, IEEE)
    return names


async def search(source: str, query: str, *, limit: int, years: Years) -> list[Paper]:
    handler = _SEARCH.get(source)
    if handler is None:
        raise SourceError(f"Unknown source {source}")
    return await handler(query, limit, years)


# ------------------------------------------------------------------ transport


# The gap a database asks for between requests from one client. arXiv states
# three seconds in its API terms and answers a faster client with 429s for a
# while; Semantic Scholar's keyless pool allows about one a second. A round
# asks each database several queries, so without this the later ones fail.
_PACE = {ARXIV: 3.0, SEMANTIC: 1.1}
_last_call: dict[str, float] = {}
_pace_locks: dict[str, asyncio.Lock] = {}


async def _paced(name: str) -> None:
    interval = _PACE.get(name, 0.0)
    if name == SEMANTIC and settings.semantic_scholar_api_key:
        interval = 0.0  # a key has its own allowance
    if not interval:
        return
    lock = _pace_locks.setdefault(name, asyncio.Lock())
    async with lock:
        wait = _last_call.get(name, 0.0) + interval - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call[name] = time.monotonic()


def _retry_after(response: httpx.Response) -> float:
    try:
        seconds = float(response.headers.get("Retry-After", ""))
    except ValueError:
        seconds = 2.0
    return min(max(seconds, 1.0), 5.0)


async def _send(method: str, url: str, *, name: str, **kwargs: Any) -> httpx.Response:
    """One request to a database, paced, with one retry after a 429.

    One, because a rate limit that has not lifted after a few seconds will
    not lift within the round, and the round has other databases to report.
    """
    label = LABELS[name]
    for attempt in (1, 2):
        await _paced(name)
        try:
            response = await net.client().request(
                method, url, timeout=settings.scholar_timeout_s, **kwargs
            )
        except httpx.TimeoutException:
            raise SourceError(f"{label} did not answer within {settings.scholar_timeout_s:.0f}s")
        except httpx.HTTPError as exc:
            raise SourceError(f"{label} could not be reached ({type(exc).__name__})")
        if response.status_code == 429 and attempt == 1:
            await asyncio.sleep(_retry_after(response))
            continue
        _check(response, name)
        return response
    raise SourceError(f"{label} is rate-limiting this install")  # unreachable


async def _get(
    url: str, *, params: dict[str, Any], headers: dict[str, str] | None = None, name: str
) -> httpx.Response:
    return await _send("GET", url, name=name, params=params, headers=headers or {})


async def _post(
    url: str, *, params: dict[str, Any], json: Any, headers: dict[str, str], name: str
) -> httpx.Response:
    return await _send("POST", url, name=name, params=params, json=json, headers=headers)


def _check(response: httpx.Response, name: str) -> None:
    status = response.status_code
    if status < 400:
        return
    label = LABELS[name]
    if status == 429:
        raise SourceError(f"{label} is rate-limiting this install, try again in a minute")
    if status in (401, 403):
        # IEEE says why in a short XML body ("Developer Inactive" for a key that
        # has been issued but not activated yet, "Account Over Rate Limit"),
        # and that sentence is what the person needs to act on.
        reason = re.sub(r"<[^>]+>", " ", response.text or "").strip()[:80]
        if reason and len(reason.split()) <= 6:
            raise SourceError(f"{label} refused the API key: {reason}")
        raise SourceError(f"{label} refused the API key")
    raise SourceError(f"{label} answered HTTP {status}")


def _json(response: httpx.Response, name: str) -> Any:
    try:
        return response.json()
    except ValueError:
        raise SourceError(f"{LABELS[name]} sent something that is not JSON")


def _mailto(params: dict[str, Any]) -> dict[str, Any]:
    if settings.scholar_mailto:
        params["mailto"] = settings.scholar_mailto
    return params


# ------------------------------------------------------------------- OpenAlex


async def _openalex(query: str, limit: int, years: Years) -> list[Paper]:
    params: dict[str, Any] = {
        "search": query,
        "per-page": min(limit, 50),
        "select": OPENALEX_SELECT,
    }
    year_filter = _openalex_years(years)
    if year_filter:
        params["filter"] = year_filter
    return await _openalex_works(params)


async def _openalex_works(params: dict[str, Any]) -> list[Paper]:
    params = _mailto(dict(params))
    if settings.openalex_api_key:
        params["api_key"] = settings.openalex_api_key
    response = await _get(OPENALEX_URL, params=params, name=OPENALEX)
    data = _json(response, OPENALEX)
    return [p for p in (parse_openalex(item) for item in data.get("results") or []) if p]


def _openalex_years(years: Years) -> str:
    start, end = years
    parts = []
    if start:
        parts.append(f"from_publication_date:{start}-01-01")
    if end:
        parts.append(f"to_publication_date:{end}-12-31")
    return ",".join(parts)


def openalex_abstract(index: dict[str, list[int]] | None) -> str:
    """OpenAlex stores an abstract as {word: [positions]}, for licensing
    reasons; putting the words back in position order is the abstract."""
    if not index:
        return ""
    placed: dict[int, str] = {}
    for word, positions in index.items():
        for position in positions or ():
            placed[position] = word
    return " ".join(placed[i] for i in sorted(placed))


_OPENALEX_KINDS = {
    "journal-article": JOURNAL,
    "proceedings-article": CONFERENCE,
    "posted-content": PREPRINT,
    "book": BOOK,
    "book-chapter": BOOK,
    "monograph": BOOK,
    "dissertation": THESIS,
}


def parse_openalex(item: dict[str, Any]) -> Paper | None:
    title = clean_text(item.get("display_name") or item.get("title"), limit=500)
    if not title:
        return None
    location = item.get("primary_location") or {}
    source = location.get("source") or {}
    ids = item.get("ids") or {}
    kind = _OPENALEX_KINDS.get(item.get("type_crossref") or "", "")
    if not kind:
        kind = {"preprint": PREPRINT, "dissertation": THESIS, "book": BOOK}.get(
            item.get("type") or "", JOURNAL if source.get("type") == "journal" else OTHER
        )
    if source.get("type") == "conference":
        kind = CONFERENCE
    biblio = item.get("biblio") or {}
    pages = "–".join(p for p in (biblio.get("first_page"), biblio.get("last_page")) if p)
    openalex_id = (item.get("id") or "").rsplit("/", 1)[-1]
    paper = Paper(
        title=title,
        abstract=clean_text(openalex_abstract(item.get("abstract_inverted_index"))),
        authors=[
            a["author"]["display_name"]
            for a in (item.get("authorships") or [])
            if (a.get("author") or {}).get("display_name")
        ][:AUTHORS_MAX],
        year=year_of(item.get("publication_year")),
        venue=clean_text(source.get("display_name"), limit=300),
        kind=kind,
        doi=normalise_doi(item.get("doi")),
        url=location.get("landing_page_url") or "",
        pdf=(item.get("open_access") or {}).get("oa_url") or location.get("pdf_url") or "",
        citations=item.get("cited_by_count"),
        publisher=clean_text(source.get("host_organization_name"), limit=200),
        volume=str(biblio.get("volume") or ""),
        issue=str(biblio.get("issue") or ""),
        pages=pages,
        sources=[OPENALEX],
        references=[r.rsplit("/", 1)[-1] for r in item.get("referenced_works") or []],
    )
    if openalex_id:
        paper.ids["openalex"] = openalex_id
    if ids.get("pmid"):
        paper.ids["pmid"] = str(ids["pmid"]).rsplit("/", 1)[-1]
    if "arxiv.org" in paper.url:
        paper.arxiv = normalise_arxiv(paper.url)
    return paper


async def openalex_by_ids(openalex_ids: list[str]) -> list[Paper]:
    """Works by OpenAlex id, fifty to a request, which is the filter's limit."""
    found: list[Paper] = []
    for start in range(0, len(openalex_ids), 50):
        batch = openalex_ids[start : start + 50]
        found += await _openalex_works(
            {"filter": f"openalex:{'|'.join(batch)}", "per-page": 50, "select": OPENALEX_SELECT}
        )
    return found


async def openalex_by_dois(dois: list[str]) -> list[Paper]:
    found: list[Paper] = []
    for start in range(0, len(dois), 50):
        batch = dois[start : start + 50]
        found += await _openalex_works(
            {"filter": f"doi:{'|'.join(batch)}", "per-page": 50, "select": OPENALEX_SELECT}
        )
    return found


async def openalex_citing(openalex_ids: list[str], *, limit: int, years: Years) -> list[Paper]:
    """Works that cite any of these, most cited first. The caller counts how
    many of the seeds each one cites, from its reference list."""
    filters = [f"cites:{'|'.join(openalex_ids[:50])}"]
    year_filter = _openalex_years(years)
    if year_filter:
        filters.append(year_filter)
    return await _openalex_works(
        {
            "filter": ",".join(filters),
            "per-page": min(limit, 50),
            "sort": "cited_by_count:desc",
            "select": OPENALEX_SELECT,
        }
    )


# ----------------------------------------------------------- Semantic Scholar


def _s2_headers() -> dict[str, str]:
    key = settings.semantic_scholar_api_key
    return {"x-api-key": key} if key else {}


async def _semantic(query: str, limit: int, years: Years) -> list[Paper]:
    params: dict[str, Any] = {"query": query, "limit": min(limit, 100), "fields": S2_FIELDS}
    start, end = years
    if start or end:
        params["year"] = f"{start or ''}-{end or ''}"
    response = await _get(
        f"{S2_URL}/paper/search", params=params, headers=_s2_headers(), name=SEMANTIC
    )
    data = _json(response, SEMANTIC)
    return [p for p in (parse_semantic(item) for item in data.get("data") or []) if p]


def parse_semantic(item: dict[str, Any] | None) -> Paper | None:
    if not item:
        return None
    title = clean_text(item.get("title"), limit=500)
    if not title:
        return None
    external = _mapping(item.get("externalIds"))
    types = item.get("publicationTypes") or []
    # An object in search results and, from the recommendations endpoint, now
    # and then a bare string. Read as an object only when it is one.
    venue_info = _mapping(item.get("publicationVenue"))
    if "Conference" in types or venue_info.get("type") == "conference":
        kind = CONFERENCE
    elif "JournalArticle" in types or venue_info.get("type") == "journal":
        kind = JOURNAL
    elif external.get("ArXiv") and not external.get("DOI"):
        kind = PREPRINT
    else:
        kind = OTHER
    journal = _mapping(item.get("journal"))
    paper = Paper(
        title=title,
        abstract=clean_text(item.get("abstract")),
        authors=[
            a["name"] for a in item.get("authors") or [] if isinstance(a, dict) and a.get("name")
        ][:AUTHORS_MAX],
        year=year_of(item.get("year")),
        venue=clean_text(item.get("venue") or journal.get("name"), limit=300),
        kind=kind,
        doi=normalise_doi(external.get("DOI")),
        arxiv=normalise_arxiv(external.get("ArXiv")),
        url=item.get("url") or "",
        pdf=_mapping(item.get("openAccessPdf")).get("url") or "",
        citations=item.get("citationCount"),
        tldr=clean_text(_mapping(item.get("tldr")).get("text"), limit=600),
        volume=str(journal.get("volume") or "").strip(),
        pages=str(journal.get("pages") or "").strip().replace("-", "–"),
        sources=[SEMANTIC],
    )
    if item.get("paperId"):
        paper.ids["s2"] = item["paperId"]
    if external.get("PubMed"):
        paper.ids["pmid"] = str(external["PubMed"])
    return paper


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


async def semantic_ids(papers: list[Paper]) -> dict[str, str]:
    """Semantic Scholar's paperId for each paper that has none yet, by DOI or
    arXiv id, in one batch call. Returns {paper.key: paperId}."""
    wanted: dict[str, str] = {}
    for paper in papers:
        if paper.ids.get("s2"):
            continue
        if paper.doi:
            wanted[f"DOI:{paper.doi}"] = paper.key
        elif paper.arxiv:
            wanted[f"ARXIV:{paper.arxiv}"] = paper.key
    if not wanted:
        return {}
    names = list(wanted)[:500]
    response = await _post(
        f"{S2_URL}/paper/batch",
        params={"fields": "paperId"},
        json={"ids": names},
        headers=_s2_headers(),
        name=SEMANTIC,
    )
    found: dict[str, str] = {}
    for name, item in zip(names, _json(response, SEMANTIC) or [], strict=False):
        if item and item.get("paperId"):
            found[wanted[name]] = item["paperId"]
    return found


async def recommend(positive: list[str], negative: list[str], *, limit: int) -> list[Paper]:
    """Papers like the positives and unlike the negatives, by S2 paperId."""
    response = await _post(
        S2_RECOMMEND_URL,
        params={"fields": S2_RECOMMEND_FIELDS, "limit": min(limit, 100)},
        json={"positivePaperIds": positive[:100], "negativePaperIds": negative[:100]},
        headers=_s2_headers(),
        name=SEMANTIC,
    )
    data = _json(response, SEMANTIC)
    return [p for p in (parse_semantic(i) for i in data.get("recommendedPapers") or []) if p]


# -------------------------------------------------------------------- Crossref


async def _crossref(query: str, limit: int, years: Years) -> list[Paper]:
    params: dict[str, Any] = {
        "query.bibliographic": query,
        "rows": min(limit, 50),
        "select": (
            "DOI,title,author,issued,container-title,abstract,type,"
            "is-referenced-by-count,URL,publisher,volume,issue,page"
        ),
    }
    start, end = years
    filters = []
    if start:
        filters.append(f"from-pub-date:{start}")
    if end:
        filters.append(f"until-pub-date:{end}")
    if filters:
        params["filter"] = ",".join(filters)
    response = await _get(CROSSREF_URL, params=_mailto(params), name=CROSSREF)
    data = _json(response, CROSSREF)
    items = (data.get("message") or {}).get("items") or []
    return [p for p in (parse_crossref(item) for item in items) if p]


_CROSSREF_KINDS = {
    "journal-article": JOURNAL,
    "proceedings-article": CONFERENCE,
    "posted-content": PREPRINT,
    "book": BOOK,
    "book-chapter": BOOK,
    "monograph": BOOK,
    "dissertation": THESIS,
}


def parse_crossref(item: dict[str, Any]) -> Paper | None:
    title = clean_text(" ".join(item.get("title") or []), limit=500)
    if not title:
        return None
    authors = []
    for author in item.get("author") or []:
        name = " ".join(p for p in (author.get("given"), author.get("family")) if p)
        name = name or author.get("name") or ""
        if name:
            authors.append(name)
    issued = ((item.get("issued") or {}).get("date-parts") or [[None]])[0]
    return Paper(
        title=title,
        abstract=clean_text(item.get("abstract")),
        authors=authors[:AUTHORS_MAX],
        year=year_of(issued[0] if issued else None),
        venue=clean_text(" ".join((item.get("container-title") or [])[:1]), limit=300),
        kind=_CROSSREF_KINDS.get(item.get("type") or "", OTHER),
        doi=normalise_doi(item.get("DOI")),
        url=item.get("URL") or "",
        citations=item.get("is-referenced-by-count"),
        publisher=clean_text(item.get("publisher"), limit=200),
        volume=str(item.get("volume") or ""),
        issue=str(item.get("issue") or ""),
        pages=str(item.get("page") or "").replace("-", "–"),
        sources=[CROSSREF],
    )


# ----------------------------------------------------------------------- arXiv

_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV_NS = "{http://arxiv.org/schemas/atom}"
_QUERY_WORD = re.compile(r"[\w-]{2,}", re.UNICODE)


def arxiv_query(query: str) -> str:
    """arXiv's own syntax: every word required, in any field. Its search does
    no ranking worth the name on a loose phrase, and a quoted phrase finds
    only papers that use those exact words in that order."""
    words = _QUERY_WORD.findall(query)[:8]
    return " AND ".join(f"all:{w}" for w in words) or "all:*"


async def _arxiv(query: str, limit: int, years: Years) -> list[Paper]:
    params = {
        "search_query": arxiv_query(query),
        "max_results": min(limit, 50),
        "sortBy": "relevance",
    }
    response = await _get(ARXIV_URL, params=params, name=ARXIV)
    papers = parse_arxiv(response.text)
    start, end = years
    # The API has a date filter, but in a syntax that silently matches nothing
    # when written slightly wrong; filtering here cannot fail that way.
    return [
        p
        for p in papers
        if (not start or (p.year or 0) >= start) and (not end or (p.year or 9999) <= end)
    ]


def parse_arxiv(xml_text: str) -> list[Paper]:
    """arXiv's Atom feed. ElementTree does not resolve external entities, and
    the feed comes from arXiv over TLS, which is the extent of the trust here."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        raise SourceError("arXiv sent a feed that does not parse")
    papers = []
    for entry in root.findall(f"{_ATOM}entry"):
        title = clean_text(entry.findtext(f"{_ATOM}title"), limit=500)
        arxiv_id = normalise_arxiv(entry.findtext(f"{_ATOM}id"))
        if not title or not arxiv_id:
            continue
        pdf = ""
        for link in entry.findall(f"{_ATOM}link"):
            if link.get("title") == "pdf":
                pdf = link.get("href") or ""
        doi = normalise_doi(entry.findtext(f"{_ARXIV_NS}doi"))
        journal = clean_text(entry.findtext(f"{_ARXIV_NS}journal_ref"), limit=300)
        papers.append(
            Paper(
                title=title,
                abstract=clean_text(entry.findtext(f"{_ATOM}summary")),
                authors=[
                    clean_text(a.findtext(f"{_ATOM}name"), limit=200)
                    for a in entry.findall(f"{_ATOM}author")
                ][:AUTHORS_MAX],
                year=year_of(entry.findtext(f"{_ATOM}published")),
                venue=journal or "arXiv",
                kind=JOURNAL if doi and journal else PREPRINT,
                doi=doi,
                arxiv=arxiv_id,
                url=f"https://arxiv.org/abs/{arxiv_id}",
                pdf=pdf,
                sources=[ARXIV],
            )
        )
    return papers


# ------------------------------------------------------------------ IEEE Xplore


async def _ieee(query: str, limit: int, years: Years) -> list[Paper]:
    if not settings.ieee_api_key:
        raise SourceError("IEEE Xplore needs IEEE_API_KEY")
    params: dict[str, Any] = {
        "apikey": settings.ieee_api_key,
        "format": "json",
        "querytext": query,
        "max_records": min(limit, 200),
        "sort_order": "desc",
        "sort_field": "relevance",
    }
    start, end = years
    if start:
        params["start_year"] = start
    if end:
        params["end_year"] = end
    response = await _get(IEEE_URL, params=params, name=IEEE)
    data = _json(response, IEEE)
    return [p for p in (parse_ieee(item) for item in data.get("articles") or []) if p]


_IEEE_KINDS = {
    "Journals": JOURNAL,
    "Early Access Articles": JOURNAL,
    "Magazines": JOURNAL,
    "Conferences": CONFERENCE,
    "Books": BOOK,
}


def parse_ieee(item: dict[str, Any]) -> Paper | None:
    title = clean_text(item.get("title"), limit=500)
    if not title:
        return None
    authors = ((item.get("authors") or {}).get("authors")) or []
    pages = "–".join(
        str(p) for p in (item.get("start_page"), item.get("end_page")) if p and str(p).strip()
    )
    paper = Paper(
        title=title,
        abstract=clean_text(item.get("abstract")),
        authors=[a["full_name"] for a in authors if a.get("full_name")][:AUTHORS_MAX],
        year=year_of(item.get("publication_year")),
        venue=clean_text(item.get("publication_title"), limit=300),
        kind=_IEEE_KINDS.get(item.get("content_type") or "", OTHER),
        doi=normalise_doi(item.get("doi")),
        url=item.get("html_url") or item.get("abstract_url") or "",
        pdf=item.get("pdf_url") or "",
        citations=item.get("citing_paper_count"),
        publisher=clean_text(item.get("publisher"), limit=200) or "IEEE",
        volume=str(item.get("volume") or ""),
        issue=str(item.get("issue") or ""),
        pages=pages,
        sources=[IEEE],
    )
    if item.get("article_number"):
        paper.ids["ieee"] = str(item["article_number"])
    return paper


_SEARCH: dict[str, Callable[[str, int, Years], Awaitable[list[Paper]]]] = {
    OPENALEX: _openalex,
    SEMANTIC: _semantic,
    CROSSREF: _crossref,
    ARXIV: _arxiv,
    IEEE: _ieee,
}
