"""SearXNG search backend.

Free and already in the compose file, at the cost of snippets only: SearXNG
proxies public engines and returns what they show a human, so anything past a
shallow answer needs the content step in `content.py`.

Two things this module refuses to hide:

* **Which engines answered.** SearXNG reports `unresponsive_engines`, and on a
  self-hosted instance that list is rarely empty: Startpage and Brave CAPTCHA
  within minutes of use, Google News suspends itself. A run that searched one
  engine and a run that searched five look identical in the answer and are not
  remotely the same evidence, so both the engines that contributed and the ones
  that refused travel back with the results and end up in the run's footer.
* **Which corpus was searched.** Engines are named per lens rather than left to
  whatever the instance enables by default, so "Science" means PubMed and arXiv
  on every deployment.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field

import httpx

from app import net
from app.config import settings
from app.search.base import SearchError, SearchResult, classify_kind, clean_title, dedupe
from app.search.lenses import Lens
from app.search.lenses import resolve as resolve_lenses

log = logging.getLogger(__name__)

_gate = asyncio.Semaphore(settings.search_concurrency)

# SearXNG's own vocabulary for `time_range`. Anything else is dropped rather
# than passed through, because an invalid value makes it search nothing.
TIME_RANGES = frozenset({"day", "week", "month", "year"})

# Of those, the ones narrow enough to be worth a *filter*. "year" is deliberately
# absent, and the reason is in the ranking: `quality.freshness` scores everything
# published within `_FRESH_DAYS` (400, roughly thirteen months) at a flat 1.0.
# A one-year window therefore filters for precisely the set the ranking already
# rewards at full marks, and deletes the rest of the web to do it. It is the one
# window that buys nothing and costs the most: the paper from 2005, the
# standard's original text, the explanation that has been right for a decade.
#
# Day, week and month are narrower than that plateau, and there the filter does
# something no ranking can. Every page from the past thirteen months scores the
# same 1.0, so for "what happened yesterday" the ranking genuinely cannot tell
# yesterday's report from last spring's. Only the engine can, before it answers.
#
# "year" still travels as a recency *signal*: it sets `prefer_recent`, and the
# freshness term then does what a preference should do: order the results
# instead of choosing which ones exist.
HARD_WINDOWS = frozenset({"day", "week", "month"})


@dataclass(slots=True)
class SearchReport:
    """What actually happened during the searches of one run.

    Accumulated across every phase (a deep run searches a dozen times), so the
    footer can state, once, which engines the answer rests on.
    """

    queries: int = 0
    results: int = 0
    engines: Counter[str] = field(default_factory=Counter)
    unresponsive: dict[str, str] = field(default_factory=dict)
    lenses: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    time_range: str = ""

    def merge(self, other: SearchReport) -> None:
        self.queries += other.queries
        self.results += other.results
        self.engines.update(other.engines)
        # First reason wins: an engine that failed once and then answered is
        # still worth reporting, but the ten repeats of the same CAPTCHA are not.
        for engine, reason in other.unresponsive.items():
            self.unresponsive.setdefault(engine, reason)
        for lens in other.lenses:
            if lens not in self.lenses:
                self.lenses.append(lens)
        self.failures.extend(other.failures)
        self.time_range = self.time_range or other.time_range

    @property
    def engine_names(self) -> list[str]:
        """Engines that contributed results, most productive first."""
        return [name for name, _count in self.engines.most_common()]

    @property
    def refused(self) -> dict[str, str]:
        """Engines that answered nothing at all, all run.

        A run makes several requests and SearXNG reports refusals per request,
        so an engine that is rate limited on the third query still contributed
        to the first two. Listing it under "No answer from" next to its own
        twenty results is a footer contradicting itself; the reader's question
        is which engines this answer rests on and which never showed up.
        """
        return {
            name: reason for name, reason in self.unresponsive.items() if name not in self.engines
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "queries": self.queries,
            "results": self.results,
            "engines": [
                {"name": name, "results": count} for name, count in self.engines.most_common()
            ],
            "unresponsive": [
                {"name": name, "reason": reason} for name, reason in sorted(self.refused.items())
            ],
            "lenses": list(self.lenses),
            "failures": list(self.failures),
            "time_range": self.time_range,
        }


async def search(
    query: str,
    *,
    limit: int = 10,
    lenses: list[Lens] | None = None,
    time_range: str = "",
) -> tuple[list[SearchResult], SearchReport]:
    """Run one query against the selected corpora.

    Raises `SearchError`; callers decide whether that is fatal. The report comes
    back even on a partial success, because "3 of 8 engines answered" is the
    single most useful fact about a thin answer.
    """
    selected = lenses or resolve_lenses(None)
    window = time_range if time_range in TIME_RANGES else ""

    # `engines` and `categories` are a union in SearXNG, not an intersection, so
    # sending both would quietly re-enable every engine in the category and undo
    # the lens. Named engines it is, with the category as a fallback for an
    # instance that does not have those names.
    groups = _request_groups(selected, window)
    payloads = await asyncio.gather(
        *(_get(_params(query, applied, engines=engines)) for engines, applied in groups)
    )
    results = [result for payload in payloads for result in _results_of(payload)]
    unresponsive = _all_unresponsive(payloads)

    if not results and not unresponsive:
        log.info(
            "no engine answered by name for lens %s, falling back to categories",
            [lens.id for lens in selected],
        )
        payload = await _get(_params(query, window, categories=_categories_of(selected)))
        results = _results_of(payload)
        unresponsive = _unresponsive(payload)

    report = SearchReport(
        queries=1,
        lenses=[lens.id for lens in selected],
        time_range=window if any(applied for _engines, applied in groups) else "",
        unresponsive=unresponsive,
    )
    for result in results:
        report.engines.update(result.engines or [result.engine or "unknown"])

    merged = dedupe(results)[:limit]
    report.results = len(merged)
    return merged, report


def _params(
    query: str,
    time_range: str,
    *,
    engines: list[str] | None = None,
    categories: list[str] | None = None,
) -> dict[str, str]:
    params = {
        "q": query,
        "format": "json",
        "language": settings.search_language,
        "safesearch": "0",
    }
    if engines:
        params["engines"] = ",".join(engines)
    if categories:
        params["categories"] = ",".join(categories)
    if time_range:
        params["time_range"] = time_range
    return params


def _results_of(payload: dict) -> list[SearchResult]:
    """Everything in the response that is a source.

    `results` is not all of it. SearXNG's Wikipedia engine never populates
    `results` at all. It answers with an *infobox*, and only on an exact title
    match, so a reference search that reads `results` alone gets nothing from
    the one engine that would have answered it, and falls through to whichever
    book catalogue keyword-matched the query instead. The infobox is the
    encyclopaedia article; it belongs in the list.
    """
    parsed = [_parse(raw) for raw in payload.get("results") or []]
    parsed.extend(_parse_infobox(box) for box in payload.get("infoboxes") or [])
    return [result for result in parsed if result is not None]


def _parse_infobox(box: dict) -> SearchResult | None:
    """An infobox as a source. Its first URL is the article it summarises."""
    urls = [u for u in (box.get("urls") or []) if isinstance(u, dict)]
    url = box.get("url") or next((u.get("url", "") for u in urls if u.get("url")), "")
    content = _infobox_text(box)
    if not str(url).startswith(("http://", "https://")) or not content:
        return None

    engines = [name for name in (box.get("engines") or []) if name]
    result = SearchResult(
        title="",
        url=url,
        snippet=content,
        engine=(box.get("engine") or (engines[0] if engines else "")).strip(),
        engines=engines or [(box.get("engine") or "").strip()],
        # An infobox is the engine's own top answer, so it enters ranking as one
        # , high enough to compete, not so high that it cannot be out-ranked by
        # a primary source.
        score=1.0,
        category="general",
    )
    result.title = clean_title(box.get("infobox") or "", domain=result.domain)[0]
    result.assess()
    return result


async def search_all(
    queries: list[str],
    *,
    limit_per_query: int = 8,
    lenses: list[Lens] | None = None,
    time_range: str = "",
) -> tuple[list[SearchResult], SearchReport]:
    """Run several queries concurrently.

    Returns (results, report). One dead engine must not take the run with it, so
    failures land in the report rather than raising, and a time-ranged search
    that finds nothing is retried without the window, because "no results in the
    last month" is usually the filter's fault, not the web's.
    """
    selected = lenses or resolve_lenses(None)
    results, report = await _gather(queries, limit_per_query, selected, time_range)

    if not results and time_range:
        log.info("no results within time_range=%s, retrying unfiltered", time_range)
        widened, wider_report = await _gather(queries, limit_per_query, selected, "")
        report.merge(wider_report)
        report.time_range = ""
        results = widened

    return dedupe(results), report


async def _gather(
    queries: list[str], limit: int, lenses: list[Lens], time_range: str
) -> tuple[list[SearchResult], SearchReport]:
    report = SearchReport(lenses=[lens.id for lens in lenses])
    if not queries:
        return [], report

    outcomes = await asyncio.gather(
        *(search(query, limit=limit, lenses=lenses, time_range=time_range) for query in queries),
        return_exceptions=True,
    )

    results: list[SearchResult] = []
    for query, outcome in zip(queries, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            log.warning("search failed for %r: %s", query, outcome)
            report.queries += 1
            report.failures.append(f"{query}: {outcome}")
            continue
        found, one = outcome
        report.merge(one)
        results.extend(found)
    return results, report


async def _get(params: dict[str, str]) -> dict:
    try:
        async with _gate:
            response = await net.client().get(
                f"{settings.searxng_api_url.rstrip('/')}/search",
                params=params,
                timeout=settings.search_timeout_s,
            )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        # A 403 here is nearly always SearXNG's limiter or a missing `json`
        # entry under `search.formats`, and that is worth saying out loud.
        raise SearchError(
            f"SearXNG returned HTTP {exc.response.status_code}. Check that JSON output is enabled"
        ) from exc
    except httpx.TimeoutException as exc:
        # Not the same failure as an unreachable instance, and saying so matters:
        # SearXNG answers only once its slowest engine has, so one engine that
        # hangs instead of refusing takes the whole request past this timeout
        # while the instance itself is healthy. Reported as "not reachable" it
        # sends you looking for a network fault that is not there.
        raise SearchError(
            f"SearXNG did not answer within {settings.search_timeout_s:g}s "
            "(an engine is likely hanging rather than refusing)"
        ) from exc
    except httpx.HTTPError as exc:
        raise SearchError(f"SearXNG is not reachable at {settings.searxng_api_url}") from exc
    except ValueError as exc:
        raise SearchError("SearXNG returned a response that was not JSON") from exc


def _parse(raw: dict) -> SearchResult | None:
    """One SearXNG result into a `SearchResult`, or None if it is not a source.

    Everything the engine knows about provenance is kept: the paper templates
    carry a DOI, a journal and a date, and those are what make the difference
    between "a study found" and a citation the reader can check.
    """
    url = (raw.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return None

    engines = [name for name in (raw.get("engines") or []) if name]
    primary = (raw.get("engine") or (engines[0] if engines else "")).strip()
    category = (raw.get("category") or "").strip()
    doi = (raw.get("doi") or "").strip()

    result = SearchResult(
        title="",
        url=url,
        snippet=" ".join((raw.get("content") or "").split()),
        engine=primary,
        engines=engines or ([primary] if primary else []),
        score=float(raw.get("score") or 0.0),
        category=category,
        published=_published(raw),
        image=_https_only(raw.get("img_src")),
        thumbnail=_https_only(raw.get("thumbnail")),
        authors=[a for a in (raw.get("authors") or []) if a][:6],
        doi=doi,
        journal=(raw.get("journal") or "").strip(),
    )
    title, is_pdf = clean_title(raw.get("title") or "", domain=result.domain)
    result.title = title
    result.kind = classify_kind(
        url=url,
        category=category,
        template=(raw.get("template") or "").strip(),
        is_pdf=is_pdf,
        doi=doi,
    )
    result.assess()
    return result


# An infobox's labelled rows. Wolfram Alpha puts everything there and leaves
# `content` empty, and those rows are exactly the qualifying figures a price or a
# statistic needs: the change, the previous close, the range, the estimate year.
_INFOBOX_ATTRS = 8
_INFOBOX_VALUE_CHARS = 160


def _infobox_text(box: dict) -> str:
    """The infobox as a paragraph: its prose, then its labelled rows.

    Rows are kept as `label: value` on one line each, because that is the shape
    the answer will be laid out in and flattening them into a sentence loses
    which number is which.
    """
    parts = []
    prose = " ".join((box.get("content") or "").split())
    if prose:
        parts.append(prose)

    for attribute in (box.get("attributes") or [])[:_INFOBOX_ATTRS]:
        if not isinstance(attribute, dict):
            continue
        label = " ".join(str(attribute.get("label") or "").split())
        # An attribute whose value is a chart is a link to a picture of the data,
        # which is worth nothing to a text model.
        value = " ".join(str(attribute.get("value") or "").split())
        if not label or not value:
            continue
        parts.append(f"{label}: {value[:_INFOBOX_VALUE_CHARS]}")

    return " · ".join(parts)


def _https_only(value: object) -> str:
    """An image URL, or "". `http:` and `data:` are dropped.

    Mixed content is blocked by the browser anyway, and a `data:` URL from a
    search engine is a payload of unknown size that would be stored in the
    transcript. Both are better as no image than as a broken one.

    A protocol-relative `//host/path` is neither: it is an https URL written the
    way half the web writes them, and several image engines do. SearXNG's own
    HTML path prepends the scheme and moves on; this one dropped them, which is
    a picture search that finds pictures and reports none.
    """
    url = str(value or "").strip()
    if url.startswith("//"):
        url = f"https:{url}"
    return url if url.startswith("https://") else ""


def _published(raw: dict) -> str:
    """An ISO date, or "". SearXNG fills `publishedDate` for some engines and
    `pubdate` for others, and paper engines nest it a third way."""
    for value in (
        raw.get("publishedDate"),
        (raw.get("date_of_publication") or {}).get("datetime")
        if isinstance(raw.get("date_of_publication"), dict)
        else None,
        raw.get("pubdate"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip().replace(" ", "T", 1)
    return ""


def _unresponsive(payload: dict) -> dict[str, str]:
    """`[["startpage", "CAPTCHA"], …]`: the most useful field in the response."""
    entries = payload.get("unresponsive_engines") or []
    out: dict[str, str] = {}
    for entry in entries:
        if isinstance(entry, list | tuple) and entry:
            name = str(entry[0])
            reason = str(entry[1]) if len(entry) > 1 else "no response"
            out[name] = reason
        elif isinstance(entry, str):
            out[entry] = "no response"
    return out


def _request_groups(lenses: list[Lens], window: str) -> list[tuple[list[str], str]]:
    """Engine groups and the time window each one is searched with.

    One group (one request) in every case but the one that needs two: a
    recency window meeting a corpus that must not be filtered by it. The window
    is a filter at the engine, so "the past year" against a paper database
    deletes the 2005 paper that answers the question before anything here can
    rank it; the corpora that say so carry `Lens.timeless`.

    The split is by engine and not by corpus, and subtractive. Only
    engines named *exclusively* by timeless corpora drop the window: the general
    engines are shared with the web lens, and asking the same rate-limited engine
    twice per query (once windowed, once not) is how a self-hosted instance
    talks itself into a CAPTCHA. An engine that belongs to a windowed corpus
    keeps the window.
    """
    if not window:
        return [(_engines_of(lenses), "")]

    windowed = _engines_of([lens for lens in lenses if not lens.timeless])
    unfiltered = [name for name in _engines_of(lenses) if name not in set(windowed)]
    groups = [(windowed, window), (unfiltered, "")]
    return [(engines, applied) for engines, applied in groups if engines]


def _all_unresponsive(payloads: list[dict]) -> dict[str, str]:
    """Refusals across every request this search made. First reason wins, the
    same rule `SearchReport.merge` uses across a run."""
    merged: dict[str, str] = {}
    for payload in payloads:
        for engine, reason in _unresponsive(payload).items():
            merged.setdefault(engine, reason)
    return merged


def _engines_of(lenses: list[Lens]) -> list[str]:
    """Every engine the lenses name, once each, in the order first named."""
    return list(dict.fromkeys(engine for lens in lenses for engine in lens.engines))


def _categories_of(lenses: list[Lens]) -> list[str]:
    names = list(dict.fromkeys(category for lens in lenses for category in lens.categories))
    return names or ["general"]
