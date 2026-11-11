"""Literature: build a reading list in rounds, each round better than the last.

The workflow is a systematic search done by hand, made cheap enough to do
properly:

1. **Talk.** A short conversation turns "I need sources for chapter 3" into a
   brief: the question, what the sources are for, inclusion and exclusion
   criteria, a year window, and English queries for the databases. The brief
   is shown and editable; it is the yardstick for everything after.
2. **Search a round.** The queries go to every paper database this install can
   reach (`scholar/sources.py`), the copies are merged, anything the
   collection has already shown is dropped, and the rest is ranked
   (`scholar/rank.py`) and cut to one screenable page.
3. **Screen.** The browser asks for summaries a few papers at a time; each
   abstract is read *against the brief* and comes back with a suggested
   verdict. The person decides: relevant, maybe, out.
4. **Search again.** Now the decisions steer: new queries from the language the
   relevant papers use, Semantic Scholar recommendations from the picks and
   the rejects, the citation graph around the picks, and a ranking pulled
   towards the picks and away from the rejects. Nothing already shown comes
   back, decided or not.
5. **Export** the relevant ones (and the maybes, if asked) as BibTeX.

Like the redact tool these are plain requests, not runs: nothing streams, and
the browser drives the screening calls one batch at a time, so progress and
stopping need no job store. Unlike it, everything is persisted, a reading list
is weeks of work.

Every step that can partly fail says so in the round's report, which is kept
with the collection: a database that rate-limited, an embedding model that did
not answer and left the ranking lexical, a query plan that fell back to the
brief's own queries. A round that silently searched three databases instead of
five would look exactly like a round that found less.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiosqlite
from pydantic import BaseModel, Field

from app.agent import prompts
from app.llm.client import LLM, LLMError
from app.scholar import bibtex, rank, sources
from app.scholar.paper import Paper, identities, merge
from app.search import quality
from app.store import literature as store
from app.store import settings_store
from app.store.db import now

log = logging.getLogger(__name__)

# One screenable page. Screening thirty abstracts is a sitting; a hundred is a
# chore that gets skimmed, and skimmed screening is how a reading list ends up
# with the wrong papers in it. The rest of a round's candidates are not lost:
# the next round finds them again if they still rank.
PER_ROUND = 25
PER_QUERY = 15
MAX_QUERIES = 4
# The relevant papers a round builds on. The most recently picked first: they
# reflect what the person has learned to want, which drifts as they read.
SEEDS = 15
SNOWBALL = 40
RECOMMEND = 40
# Papers per screening call. Small enough that a cheap model reads every
# abstract rather than the first three, large enough that a page of twenty-five
# is five calls.
SCREEN_BATCH = 6
SCREEN_TOKENS = 2000
TALK_TOKENS = 1400
QUERY_TOKENS = 500

# Requests in flight per database. arXiv and Semantic Scholar are also paced
# (`sources._PACE`); Crossref's public pool allows one concurrent request
# and answers a second with 429s. OpenAlex and IEEE take a little more.
_CONCURRENCY = {sources.ARXIV: 1, sources.SEMANTIC: 1, sources.CROSSREF: 1}

STRATEGY_LABELS = {
    "recommended": "Semantic Scholar recommendations",
    "references": "Cited by your relevant papers",
    "citations": "Citing your relevant papers",
}


class LiteratureError(RuntimeError):
    """A message for the person: why this step cannot run."""


class Brief(BaseModel):
    question: str = ""
    purpose: str = ""
    field: str = ""
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    year_from: int = 0
    year_to: int = 0
    queries: list[str] = Field(default_factory=list)
    language: str = ""

    def tidy(self) -> Brief:
        """Trimmed and bounded. The brief is model output, and it is sent back
        to the model on every turn, so a runaway list costs on every call."""

        def items(values: list[str], n: int, length: int) -> list[str]:
            seen, out = set(), []
            for value in values:
                text = " ".join(str(value).split())[:length]
                if text and text.casefold() not in seen:
                    seen.add(text.casefold())
                    out.append(text)
            return out[:n]

        def year(value: int) -> int:
            return value if 1800 <= value <= 2100 else 0

        return Brief(
            question=self.question.strip()[:600],
            purpose=self.purpose.strip()[:400],
            field=self.field.strip()[:120],
            include=items(self.include, 10, 200),
            exclude=items(self.exclude, 10, 200),
            year_from=year(self.year_from),
            year_to=year(self.year_to),
            queries=items(self.queries, 6, 160),
            language=self.language.strip().lower()[:8],
        )

    @property
    def years(self) -> sources.Years:
        return (self.year_from or None, self.year_to or None)

    def text(self) -> str:
        """What relevance is measured against."""
        return " ".join([self.question, self.field, *self.include, *self.queries])


class TalkReply(BaseModel):
    reply: str
    brief: Brief
    ready: bool
    title: str


class _Queries(BaseModel):
    queries: list[str]
    rationale: str


class _Screen(BaseModel):
    id: int
    summary: str
    fit: str
    verdict: str


class _Screening(BaseModel):
    items: list[_Screen]


# ------------------------------------------------------------------- models


@dataclass(frozen=True, slots=True)
class Models:
    # One model for the conversation, the query plans and the screening. Not
    # the router model, even for screening: judging an abstract against a set
    # of criteria is the hardest call this page makes, not the cheapest.
    llm: tuple[str, str]
    embed: tuple[str, str] | None


async def models_for(conn: aiosqlite.Connection, user: str) -> Models:
    saved = await settings_store.load(conn, user)
    if saved.literature_model:
        llm = (saved.literature_model, saved.literature_provider or saved.chat_provider)
    else:
        llm = (saved.chat_model, saved.chat_provider)
    embed = (
        (saved.embedding_model, saved.embedding_provider)
        if saved.embedding_enabled and saved.embedding_model
        else None
    )
    return Models(llm=llm, embed=embed)


# ------------------------------------------------------------------ language
#
# Which language the person writes in is decided here, not by the model. Left
# to the model, a small local model answered an English message in French and
# wrote `fr` into the brief, after which every summary came out French as well.
# Counting common function words is crude,
# and for this question it is enough: it needs a sentence or two, and it never
# guesses when it does not know.

LANGUAGES = {
    "en": "English",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "nl": "Dutch",
    "pt": "Portuguese",
}

_FUNCTION_WORDS = {
    "en": "the and of to in is that for it with as on are this be i my need what which from or not",
    "de": (
        "der die das und ist ich nicht zu den mit ein eine für auf sich von dem des im "
        "auch wie brauche"
    ),
    "fr": "le la les et est je pas des un une du pour que qui dans sur avec ce mon ma au",
    "es": "el la los las y es que de un una por para con no en mi se lo del al",
    "it": "il lo la gli le e è che di un una per con non in mi si del della",
    "nl": "de het een en is ik niet van op voor met dat die te zijn mijn",
    "pt": "o a os as e é que de um uma para com não em meu se do da",
}
_WORD_SETS = {code: set(words.split()) for code, words in _FUNCTION_WORDS.items()}


def detect_language(text: str) -> str:
    """The ISO code of the language `text` is written in, or "" when unsure."""
    words = re.findall(r"[^\W\d_]+", text.lower())
    if len(words) < 4:
        return ""
    hits = {code: sum(w in vocab for w in words) for code, vocab in _WORD_SETS.items()}
    ranked = sorted(hits.items(), key=lambda kv: kv[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]
    # A clear winner, and enough function words that it is a sentence.
    if top >= 2 and top >= second * 1.5 and top / len(words) >= 0.08:
        return best
    return ""


def language_name(code: str) -> str:
    return LANGUAGES.get((code or "").lower(), "")


def brief_of(collection: dict[str, Any]) -> Brief:
    try:
        return Brief.model_validate(collection.get("brief") or {})
    except ValueError:
        return Brief()


# --------------------------------------------------------------------- talk


async def talk(
    conn: aiosqlite.Connection, collection: dict[str, Any], message: str, *, user: str
) -> dict[str, Any]:
    """One turn of the intake conversation. The person's turn is stored only
    together with the reply, so a failed call leaves the conversation as it was
    and the message can simply be sent again."""
    models = await models_for(conn, user)
    turns = [*collection["talk"], {"role": "user", "content": message.strip()}]
    brief = brief_of(collection)
    # The latest message first: someone who switches language mid-conversation
    # means the new one. Then everything they wrote, for a one-line reply.
    language = detect_language(message) or detect_language(
        " ".join(t["content"] for t in turns if t["role"] == "user")
    )
    language = language or brief.language
    llm = LLM(*models.llm)
    answer = await llm.structured(
        prompts.lit_talk(brief.model_dump(), turns, language_name(language)),
        TalkReply,
        max_tokens=TALK_TOKENS,
    )
    new_brief = answer.brief.tidy()
    if language:
        new_brief.language = language
    reply = answer.reply.strip() or "…"
    turns.append({"role": "assistant", "content": reply})

    fields: dict[str, Any] = {"talk": turns, "brief": new_brief.model_dump()}
    name = collection["name"]
    if name == store.DEFAULT_NAME and answer.title.strip():
        name = " ".join(answer.title.split())[:80]
        fields["name"] = name
    await store.update(conn, collection["id"], user=user, **fields)
    return {
        "reply": reply,
        "brief": new_brief.model_dump(),
        "ready": answer.ready,
        "name": name,
        "talk": turns[-store.MAX_TALK_TURNS :],
    }


# -------------------------------------------------------------------- rounds


async def run_round(
    conn: aiosqlite.Connection, collection: dict[str, Any], *, user: str
) -> dict[str, Any]:
    """Search, merge, drop what was already shown, rank, keep one page.

    Returns the round's report, which is also appended to the collection.
    Raises `LiteratureError` only when there is nothing to search for; every
    other failure is a line in the report.
    """
    brief = brief_of(collection)
    if not (brief.queries or brief.question):
        raise LiteratureError("Describe what you are looking for first, the search needs a brief.")

    rows = await store.list_papers(conn, collection["id"])
    newest_first = sorted(rows, key=lambda r: r.get("decided_at") or "", reverse=True)
    relevant = [_row_paper(r) for r in newest_first if r["decision"] == store.RELEVANT]
    excluded = [_row_paper(r) for r in rows if r["decision"] == store.EXCLUDED]
    rounds = list(collection["rounds"])
    number = len(rounds) + 1
    models = await models_for(conn, user)
    notes: list[str] = []

    queries, rationale = await _plan(brief, rounds, rows, models, notes)

    searches = _search_all(queries, brief.years)
    strategies: list[asyncio.Future] = []
    if relevant:
        strategies = [
            asyncio.ensure_future(
                _strategy("recommended", _recommended(relevant[:SEEDS], excluded))
            ),
            asyncio.ensure_future(_strategy("graph", _snowball(relevant[:SEEDS], brief.years))),
        ]
    found, per_source = await searches
    strategy_reports: list[dict[str, Any]] = []
    for future in strategies:
        papers, report = await future
        found += papers
        strategy_reports += report

    candidates = merge(found)
    seen: set[str] = set()
    for row in rows:
        seen.update(identities(_row_paper(row)))
    fresh = [p for p in candidates if not seen.intersection(identities(p))]
    fresh = [p for p in fresh if _in_window(p, brief)]

    method = await _rank(fresh, brief, relevant, excluded, models, notes)
    chosen = choose(fresh, PER_ROUND)

    added = await store.add_papers(
        conn,
        collection["id"],
        [{"key": p.key, "data": _stored(p), "origin": p.origin, "score": p.score} for p in chosen],
        round_no=number,
    )
    report = {
        "round": number,
        "at": now(),
        "queries": queries,
        "rationale": rationale,
        "sources": per_source,
        "strategies": strategy_reports,
        "found": len(candidates),
        "new": len(fresh),
        "added": added,
        "ranking": method,
        "notes": notes,
    }
    rounds.append(report)
    await store.update(conn, collection["id"], user=user, rounds=rounds)
    log.info(
        "literature round %d: %d found, %d new, %d added (%s)",
        number,
        len(candidates),
        len(fresh),
        added,
        method,
    )
    return report


# Places each way of finding a paper is guaranteed in a round, before the rest
# go by score. Measured on a live second round: the graph's "cites your picks"
# took eighteen of twenty-five places, because those papers are found sorted by
# citation count and carry the graph bonus, and the forty Semantic Scholar
# recommendations got none. Each strategy is a different guess at what the
# person wants; a page that shows only one of them cannot tell which works.
QUOTA = 4


def choose(papers: list[Paper], n: int) -> list[Paper]:
    """The best `n`, with each origin's best few guaranteed a place, best
    first. Deterministic: ties keep the order the candidates arrived in."""
    ranked = sorted(papers, key=lambda p: p.score, reverse=True)
    taken: list[Paper] = []
    for origin in dict.fromkeys(p.origin for p in ranked):
        taken += [p for p in ranked if p.origin == origin][:QUOTA]
    taken = taken[:n]
    chosen = set(map(id, taken))
    taken += [p for p in ranked if id(p) not in chosen][: n - len(taken)]
    return sorted(taken, key=lambda p: p.score, reverse=True)


def _row_paper(row: dict[str, Any]) -> Paper:
    try:
        return Paper.from_dict(row["data"])
    except TypeError:
        return Paper(title=str(row["data"].get("title") or ""))


def _stored(paper: Paper) -> dict[str, Any]:
    data = paper.as_dict()
    # The graph count is what the badge explains ("cited by 3 of your picks"),
    # so it is kept with the record even though the score is not.
    data["links"] = paper.links
    return data


def _in_window(paper: Paper, brief: Brief) -> bool:
    """The databases filter by year themselves; the citation graph and the
    recommendations do not, and a 1998 classic is still not in a 2018-on
    window. A paper with no year is kept, it cannot be judged either way."""
    if not paper.year:
        return True
    if brief.year_from and paper.year < brief.year_from:
        return False
    return not (brief.year_to and paper.year > brief.year_to)


async def _plan(
    brief: Brief,
    rounds: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    models: Models,
    notes: list[str],
) -> tuple[list[str], str]:
    """The queries for this round.

    The first round runs the brief's own queries. Later ones ask the model for
    new ones from what was screened, and fall back to whichever brief queries
    have not run yet, saying so, when the model fails or nothing has been
    decided to learn from.
    """
    used = [q for r in rounds for q in r.get("queries") or []]
    used_keys = {q.casefold() for q in used}
    unused = [q for q in brief.queries if q.casefold() not in used_keys]
    decided = [r for r in rows if r["decision"] in (store.RELEVANT, store.EXCLUDED)]

    if not rounds or not decided:
        queries = (unused or brief.queries or [brief.question])[:MAX_QUERIES]
        if rounds and not decided:
            notes.append("Nothing screened yet, so this round reran the brief's queries.")
        return queries, ""

    def listing(decision: str) -> list[dict[str, str]]:
        return [
            {"title": str(r["data"].get("title") or ""), "note": r.get("note") or ""}
            for r in rows
            if r["decision"] == decision
        ][:30]

    try:
        llm = LLM(*models.llm)
        plan = await llm.structured(
            prompts.lit_queries(
                brief.model_dump(), used, listing(store.RELEVANT), listing(store.EXCLUDED)
            ),
            _Queries,
            max_tokens=QUERY_TOKENS,
        )
        queries = [
            " ".join(q.split())[:160]
            for q in plan.queries
            if q.strip() and q.casefold().strip() not in used_keys
        ][:MAX_QUERIES]
        if queries:
            return queries, plan.rationale.strip()[:300]
        notes.append("The model only proposed queries that had already run.")
    except LLMError as exc:
        notes.append(f"New queries could not be planned ({exc}).")
    except Exception as exc:  # the round still searches, with what it has
        log.warning("literature query planning failed: %s", exc)
        notes.append("New queries could not be planned.")
    fallback = (unused or brief.queries or [brief.question])[:MAX_QUERIES]
    notes.append("This round used the brief's queries instead.")
    return fallback, ""


async def _search_all(
    queries: list[str], years: sources.Years
) -> tuple[list[Paper], list[dict[str, Any]]]:
    """Every query against every database, bounded per database."""
    names = sources.available()
    gates = {name: asyncio.Semaphore(_CONCURRENCY.get(name, 2)) for name in names}

    async def one(name: str, query: str) -> tuple[str, list[Paper], str]:
        async with gates[name]:
            try:
                return name, await sources.search(name, query, limit=PER_QUERY, years=years), ""
            except sources.SourceError as exc:
                return name, [], str(exc)
            except Exception as exc:
                # A parser meeting a shape it has never seen must cost that
                # database this query, not the whole round.
                log.warning("literature: %s failed on a query: %s", name, type(exc).__name__)
                return name, [], f"{sources.LABELS[name]} failed unexpectedly"

    results = await asyncio.gather(*(one(n, q) for q in queries for n in names))
    found: list[Paper] = []
    tally: dict[str, dict[str, Any]] = {
        n: {"id": n, "label": sources.LABELS[n], "found": 0, "errors": []} for n in names
    }
    for name, papers, error in results:
        found += papers
        tally[name]["found"] += len(papers)
        if error and error not in tally[name]["errors"]:
            tally[name]["errors"].append(error)
    return found, list(tally.values())


def _strategy_report(strategy: str, error: str = "") -> dict[str, Any]:
    """One strategy's line of a round's report, before it has found anything."""
    return {
        "id": strategy,
        "label": STRATEGY_LABELS[strategy],
        "found": 0,
        "errors": [error] if error else [],
    }


async def _strategy(name: str, work) -> tuple[list[Paper], list[dict[str, Any]]]:
    """Run one feedback strategy, turning its failure into a report line."""
    try:
        return await work
    except sources.SourceError as exc:
        error = str(exc) or "failed"
    except Exception as exc:  # feedback is an improvement; the search still stands
        log.warning("literature strategy %s failed: %s", name, exc)
        error = "failed unexpectedly"
    # The graph is one strategy to run and two to report.
    ids = ["references", "citations"] if name == "graph" else [name]
    return [], [_strategy_report(i, error) for i in ids]


async def _recommended(
    relevant: list[Paper], excluded: list[Paper]
) -> tuple[list[Paper], list[dict[str, Any]]]:
    resolved = await sources.semantic_ids(relevant + excluded)

    def ids(papers: list[Paper]) -> list[str]:
        return [i for i in (p.ids.get("s2") or resolved.get(p.key) for p in papers) if i]

    positive = ids(relevant)
    report = _strategy_report("recommended")
    if not positive:
        report["errors"].append("None of the relevant papers is in Semantic Scholar")
        return [], [report]
    found = await sources.recommend(positive, ids(excluded), limit=RECOMMEND)
    for paper in found:
        paper.origin = "recommended"
    report["found"] = len(found)
    return found, [report]


async def _snowball(
    relevant: list[Paper], years: sources.Years
) -> tuple[list[Paper], list[dict[str, Any]]]:
    """Both directions of the citation graph around the relevant papers.

    Backward: what the picks cite, counted, so a paper three of them cite comes
    first. Forward: what cites the picks, with the count of picks each one
    cites, from its own reference list. Both counts become `links`, which the
    ranking and the badge both read.
    """
    known = [p.ids["openalex"] for p in relevant if p.ids.get("openalex")]
    dois = [p.doi for p in relevant if not p.ids.get("openalex") and p.doi]
    records: list[Paper] = []
    if known:
        records += await sources.openalex_by_ids(known)
    if dois:
        records += await sources.openalex_by_dois(dois)
    seeds = {r.ids["openalex"] for r in records if r.ids.get("openalex")}
    back, forth = _strategy_report("references"), _strategy_report("citations")
    if not seeds:
        back["errors"].append("None of the relevant papers is in OpenAlex")
        forth["errors"].append("None of the relevant papers is in OpenAlex")
        return [], [back, forth]

    counts = Counter(ref for r in records for ref in r.references if ref not in seeds)
    wanted = [work for work, _ in counts.most_common(SNOWBALL)]
    backward = await sources.openalex_by_ids(wanted) if wanted else []
    for paper in backward:
        paper.origin = "references"
        paper.links = counts.get(paper.ids.get("openalex", ""), 0)
    back["found"] = len(backward)

    try:
        forward = await sources.openalex_citing(sorted(seeds), limit=SNOWBALL, years=years)
    except sources.SourceError as exc:
        forth["errors"].append(str(exc))
        forward = []
    for paper in forward:
        paper.origin = "citations"
        paper.links = len(seeds.intersection(paper.references))
    forth["found"] = len(forward)
    return backward + forward, [back, forth]


async def _rank(
    papers: list[Paper],
    brief: Brief,
    relevant: list[Paper],
    excluded: list[Paper],
    models: Models,
    notes: list[str],
) -> str:
    """Score the candidates in place; returns which relevance measure ran."""
    if not papers:
        return "none"
    lexical = rank.lexical(brief.text(), papers)
    # Search results that share nothing with the brief are what a loose query
    # drags in. Graph and recommendation results are exempt: their relevance
    # came from the picks, and they often use words the brief never did, which
    # is the reason to have them.
    floor = quality.relevance_floor(lexical)
    keep = [i for i, p in enumerate(papers) if p.origin != "search" or lexical[i] >= floor]
    if len(keep) < len(papers):
        papers[:] = [papers[i] for i in keep]
        lexical = [lexical[i] for i in keep]
    if not papers:
        return "none"

    semantic = None
    if models.embed:
        try:
            semantic = await rank.semantic(
                brief.text(),
                papers,
                relevant[:SEEDS],
                excluded[: SEEDS * 2],
                model=models.embed[0],
                provider=models.embed[1],
            )
        except Exception as exc:  # ranking is an optimisation, never a failure
            log.info("literature: embedding ranking failed: %s", exc)
        if semantic is None:
            notes.append("The embedding model did not answer, so ranking fell back to keywords.")
    if semantic is None:
        rank.combine(papers, lexical)
        return "lexical"
    rank.combine(papers, semantic, lexical_scores=lexical)
    return "feedback" if relevant or excluded else "semantic"


# ------------------------------------------------------------------ screening

_VERDICTS = {
    "relevant": "relevant",
    "maybe": "maybe",
    "exclude": "excluded",
    "excluded": "excluded",
}


async def screen(
    conn: aiosqlite.Connection,
    collection: dict[str, Any],
    paper_ids: list[int],
    *,
    user: str,
    force: bool = False,
) -> dict[int, dict[str, Any]]:
    """Summaries for these papers, read against the brief.

    Papers that already have one are returned as they are unless `force`. A
    paper the model skipped comes back without a summary and is not stored, so
    asking again retries exactly it.
    """
    rows = [await store.get_paper(conn, collection["id"], pid) for pid in paper_ids[:SCREEN_BATCH]]
    rows = [r for r in rows if r]
    done = {r["id"]: r["summary"] for r in rows if r["summary"] and not force}
    todo = [r for r in rows if r["id"] not in done]
    if not todo:
        return done

    models = await models_for(conn, user)
    brief = brief_of(collection)
    payload = []
    for row in todo:
        data = row["data"]
        text = data.get("abstract") or ""
        if not text and data.get("tldr"):
            text = f"(No abstract. The database's one-line summary:) {data['tldr']}"
        payload.append(
            {
                "id": row["id"],
                "title": data.get("title") or "",
                "year": data.get("year"),
                "venue": data.get("venue") or "",
                "abstract": text,
            }
        )
    llm = LLM(*models.llm)
    result = await llm.structured(
        prompts.lit_screen(brief.model_dump(), payload, language_name(brief.language)),
        _Screening,
        max_tokens=SCREEN_TOKENS,
    )
    answered = {item.id: item for item in result.items}
    for row in todo:
        item = answered.get(row["id"])
        if item is None:
            continue
        data = row["data"]
        basis = "abstract" if data.get("abstract") else "tldr" if data.get("tldr") else "title"
        verdict = _VERDICTS.get(item.verdict.strip().lower(), "maybe")
        summary = " ".join(item.summary.split())[:1200]
        if basis == "title":
            # The prompt says so too; this is the guarantee. A summary of a
            # paper nobody has read is a paragraph of invention.
            summary = ""
            verdict = "maybe" if verdict == "relevant" else verdict
        screened = {
            "summary": summary,
            "fit": " ".join(item.fit.split())[:600],
            "verdict": verdict,
            "basis": basis,
            "model": models.llm[0],
        }
        await store.set_summary(conn, collection["id"], row["id"], screened)
        done[row["id"]] = screened
    return done


# --------------------------------------------------------------------- export


async def export_bibtex(
    conn: aiosqlite.Connection, collection: dict[str, Any], include: tuple[str, ...]
) -> tuple[str, dict[str, int]]:
    """The .bib file for the chosen decisions, and how each entry was made."""
    rows = await store.list_papers(conn, collection["id"], decisions=include)
    papers = [_row_paper(r) for r in rows]
    cached = await store.bibtex_of(conn, collection["id"])
    gate = asyncio.Semaphore(4)

    async def registry(row: dict[str, Any], paper: Paper) -> str | None:
        if row["id"] in cached:
            return cached[row["id"]]
        if not paper.doi:
            return None
        async with gate:
            fetched = await bibtex.fetch(paper.doi)
        if fetched:
            await store.set_bibtex(conn, collection["id"], row["id"], fetched)
        return fetched

    # Fetched first and keyed after: an entry can name the author a database
    # did not, and the key is built from the author.
    fetched = await asyncio.gather(*(registry(r, p) for r, p in zip(rows, papers, strict=True)))
    for paper, entry in zip(papers, fetched, strict=True):
        if entry:
            bibtex.fill_from(paper, entry)
    keys = bibtex.assign_keys(papers)
    made = [
        (bibtex.rekey(bibtex.complete(entry, paper), key), "registry")
        if entry
        else (bibtex.build(paper, key), "built")
        for paper, entry, key in zip(papers, fetched, keys, strict=True)
    ]
    stats = {
        "entries": len(made),
        "registry": sum(1 for _, how in made if how == "registry"),
        "built": sum(1 for _, how in made if how == "built"),
    }
    header = (
        f"% {collection['name']}\n"
        f"% Exported from Simplixity on {datetime.now(UTC).date().isoformat()}: "
        f"{stats['entries']} entries, {stats['registry']} from the DOI registry, "
        f"{stats['built']} built from database metadata.\n"
    )
    return header + "\n" + "\n\n".join(text for text, _ in made) + "\n", stats
