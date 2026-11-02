"""A literature project's own papers, as a search backend.

When a project has a reading list, its chats answer from the papers the person
screened, not from the web (unless they switch the web on). This module is the
search engine for that: it finds the PDF pages that bear on a query and hands
them over as `SearchResult`s, so everything after it, ranking, numbering, the
writer, the citation check, is the pipeline a web answer goes through, with no
second path that could drift from it.

**One result is one PDF page.** The page is what a thesis cites, so it is what
gets a citation number: `[4] Vaswani et al., Attention Is All You Need (PDF p.
5)`, linking to the PDF opened on that page. A paper cited from three pages
holds three numbers, which is exactly as many places a reader has to look.

**Two searches, fused by rank.** Keywords (BM25) find the exact term, the
acronym, the dataset name; embeddings find the passage that says it in other
words, or in another language. Neither score means anything on the other's
scale, so they are combined by reciprocal rank rather than by adding numbers,
which needs no calibration and degrades to keywords alone when there are no
vectors (no embedding model, or the PDFs were indexed with a different one).

**Only what was vouched for.** Pages of full texts filed under papers marked
relevant, and, for a relevant paper with no PDF yet, its abstract, labelled as
such. Earlier answers in the project are deliberately not searched: they were
written from the web, and citing them would launder it back in.

**And the files attached to the chat** (`store.attachments`), in any chat, in a
project or not. Attaching a file is vouching for it as plainly as marking a
paper relevant. Such a file stands in for a paper with its name as the title;
a PDF is cited by page, anything else by part (`tools.documents`), and a
chat with attachments and no project is a library of those files alone.
"""

from __future__ import annotations

import asyncio
import logging
import math
import operator
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from app.config import settings
from app.embed.embedder import DOCUMENT, QUERY, embed, passage_text, prepare, vector_tag
from app.llm.client import Usage
from app.search import quality
from app.search.base import KIND_PAGE, KIND_PAPER, KIND_PDF, SearchResult
from app.store import attachments, literature
from app.store import library as store

log = logging.getLogger(__name__)

# Where a cited page is opened. Relative, like the project's own chats: the
# transcript may be read from a different host than wrote it.
PAGE_URL = "/library/{file_id}/{page}"
ENGINE = "library"

# BM25's usual constants. Passages are of similar length, so length
# normalisation matters little, and the textbook values are as good as any.
_K1 = 1.2
_B = 0.75
# Reciprocal-rank fusion's damping. 60 is the value from the paper that
# introduced it and the one everybody uses; it keeps the first place of one
# ranking from drowning out agreement further down both.
_RRF = 60
# How deep each ranking is read. Below this a passage is not evidence.
_DEPTH = 60
# A passage is a keyword hit only if it holds this share of the query's terms.
# One shared word is how "Eiffel Tower height" finds the page that mentions the
# height of a feature map.
LEXICAL_COVERAGE = 0.5
# A query is about the library at all only if its best passage stands this far
# above its median one, in cosine similarity (`settings.library_semantic_gap`,
# 0.12 by default). First measured against two NLP papers: off-topic questions
# (the Eiffel Tower, a recipe, a share price) had their best passage 0.05-0.07
# above the median, on-topic ones 0.2-0.45. A gap rather than an absolute
# similarity, because models differ in their range. A narrow corpus, forty
# papers on one subject, bunches every similarity together and may need it
# lower: each search records its gaps (`Library.probes`), a run that finds
# nothing says how close it came, and `tools/calibrate_library.py` measures a
# real project.
SEMANTIC_GAP = 0.12
_HOW_CLOSE = "Nothing in your papers matched closely enough."
# Of such a query, passages in the upper half of that gap count as hits.
SEMANTIC_SHARE = 0.5
# Once one query of a run has cleared the bar, the run is about these documents,
# and its other queries need to clear only this share of it. The bar asks "is
# this question about the library at all", and that has been answered; what it
# went on to do was refuse the narrower queries of an on-topic run. Observed on
# a single attached paper asked for its key takeaways: every passage is about
# the same subject, so every similarity bunches together, and three of eight
# sub-questions stood 0.10 above the median against a bar of 0.12 and found
# nothing, while the other five, on the same paper, cleared it. Off-topic
# questions measured 0.05 to 0.07, below even the lowered bar.
SEMANTIC_ON_TOPIC = 0.75
# Papers named in the catalog the query-writing calls see, and abstracts handed
# over for a request about the whole library. Past these a library is large
# enough that "all of it" is not something one answer can be written from.
CATALOG_LIMIT = 120
OVERVIEW_LIMIT = 60
# An abstract is cut to this, at a sentence: enough to say what a paper is
# about, and sixty of them still fit beside the pages a search found.
OVERVIEW_CHARS = 900
# One paper may fill this many of a search's pages. More is a paper the
# question is about, but the reader is better served by a second paper.
PAGES_PER_PAPER = 3
# The same for a file attached to the chat. More, because a chat with one
# attachment is a conversation about that file, and there is no second paper
# to make room for.
PAGES_PER_ATTACHMENT = 8
# Pages of attached files handed over whole for a request about them as a
# whole ("summarise this"), shared out between the files. About 25k tokens.
OVERVIEW_PAGES = 24
# Attachments this short are handed over whole for every question, not only
# for a summary: the search could only ever return less of them, and a short
# document read in full is what a person who attached it expects.
WHOLE_PAGES = 8

_WORD = re.compile(r"\w{2,}", re.UNICODE)

# Re-embedding that outlives the run which found it necessary, one per project
# (or per chat, for a library of attachments alone).
# Held here so the tasks are not garbage-collected mid-flight, and so a second
# question asked meanwhile does not start a second copy of the same work.
_BACKGROUND: dict[str, asyncio.Task] = {}


def reindexing(key: str) -> bool:
    """Whether a project's (or a chat's) passages are being embedded in the
    background."""
    task = _BACKGROUND.get(key)
    return task is not None and not task.done()


# Function words in the languages questions arrive in. A query term that is in
# every passage carries no information, and BM25 already discounts it, but the
# questions people type are mostly these.
_STOP = frozenset(
    """
    the and for are was were with from that this these those into onto than then
    there their which what when where who how why does did has have had not but
    can could would should will may might its our your his her they them been
    der die das und oder ein eine einer eines einem einen ist sind war wie was wer
    wo warum welche welcher welches nicht auch mit von für auf aus bei durch über
    unter nach vor zum zur dem den des sich ich du er sie es wir ihr
    """.split()  # noqa: SIM905, a word list reads as a word list
)


def terms(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP and not w.isdigit()]


# A reference list, told from prose by its density: entry after entry of a year
# and an author's initials. Every paper's bibliography is pages of other papers'
# titles, so it matches a topic's keywords better than the paper's own text does,
# and a search that returned it handed the writer a page on which the claim it
# then cited is merely the title of somebody else's paper. Dense related-work
# prose cites too, "Smith et al. (2019) proposed …", but it has a sentence
# between two years; the thresholds below were set against both.
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}[a-z]?(?!\d)")
_INITIALS = re.compile(r"(?<![\w.])[A-Z]\.(?:\s?-?[A-Z]\.)*\s?(?:[A-Z][\w'-]+|,)")
REFERENCE_YEARS = 6  # per 1,000 characters
REFERENCE_INITIALS = 6


def looks_like_references(text: str) -> bool:
    """Whether a passage is part of a bibliography rather than of the paper."""
    if len(text) < 200:
        return False
    per = 1000 / len(text)
    return (
        len(_YEAR.findall(text)) * per >= REFERENCE_YEARS
        and len(_INITIALS.findall(text)) * per >= REFERENCE_INITIALS
    )


@dataclass(slots=True)
class _Doc:
    """One searchable passage: a chunk of a page, or a paper's abstract."""

    text: str
    paper: dict[str, Any]
    file_id: str = ""
    page: int = 0
    vector: Any = None  # array('f'), normalised, or None
    # The stored passage this is (lit_chunks.id), 0 for an abstract.
    chunk_id: int = 0
    # The vector is this model's, but made the older way (no prefix, no title):
    # good enough to search with, and re-made when there is room (`_heal`).
    dated: bool = False
    tf: Counter = field(default_factory=Counter)
    length: int = 0

    @property
    def place(self) -> tuple[str, Any]:
        """What one result is: a page of a file, or a paper's abstract."""
        return (
            (self.file_id, self.page) if self.file_id else ("abstract", self.paper.get("key", ""))
        )


def _byline(paper: dict[str, Any]) -> str:
    """ "Vaswani et al. 2017", as a reader names a paper."""
    authors = [str(a) for a in paper.get("authors") or [] if a]
    surname = authors[0].split()[-1] if authors else ""
    who = f"{surname} et al." if len(authors) > 2 else " & ".join(a.split()[-1] for a in authors)
    year = str(paper.get("year") or "").strip()
    return " ".join(part for part in (who, year) if part) or "Unknown"


def _trim(text: str, limit: int) -> str:
    """Cut at the last sentence end before `limit`, so a quotation taken from
    the trimmed text is still a whole sentence of the abstract."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text.rfind(". ", 0, limit)
    return text[: cut + 1] if cut > limit // 2 else text[:limit]


def _normalised(vector: Any) -> Any:
    norm = math.sqrt(sum(v * v for v in vector))
    if not norm:
        return None
    return [v / norm for v in vector]


def _cosine(a: Any, b: Any) -> float:
    # Both sides are normalised at load, so the dot product is the cosine.
    return sum(map(operator.mul, a, b))


class Library:
    """The searchable papers of one project, and the files attached to one
    chat, loaded once per run. Either may be missing, not both."""

    def __init__(
        self,
        conn: aiosqlite.Connection,
        project_id: str | None,
        *,
        chat_id: str | None = None,
        user: str = "",
        embedding_model: str | None = None,
        embedding_provider: str = "openrouter",
    ) -> None:
        self.conn = conn
        self.project_id = project_id
        self.chat_id = chat_id
        self.user = user
        self.key = project_id or f"chat:{chat_id}"
        self.embedding_model = embedding_model
        self.embedding_provider = embedding_provider
        self._docs: list[_Doc] | None = None
        # Every passage of a page, in page order, so a result can carry them.
        self._by_place: dict[Any, list[int]] = {}
        self._df: Counter = Counter()
        self._avg = 1.0
        self._lock = asyncio.Lock()
        # Said once per run, in the transcript, by the caller, which takes
        # them with `fresh_notes` so a note is never said twice.
        self.notes: list[str] = []
        self._said = 0
        # What each semantic search saw: the query, its best and median cosine,
        # and whether the gap between them cleared the bar. The evidence for
        # tuning `SEMANTIC_GAP` on a real corpus rather than guessing.
        self.probes: list[dict[str, Any]] = []
        self._healed = False
        self._papers: list[dict[str, Any]] | None = None
        # Whether a query of this run has cleared the full bar (see
        # `SEMANTIC_ON_TOPIC`). Per run, because a Library is built per run.
        self._on_topic = False

    async def _load(self) -> list[_Doc]:
        async with self._lock:
            if self._docs is not None:
                return self._docs
            docs: list[_Doc] = []
            references = 0
            tag = vector_tag(self.embedding_model) if self.embedding_model else None
            rows = (
                await store.project_passages(self.conn, self.project_id) if self.project_id else []
            )
            if self.chat_id:
                rows += await attachments.passages(self.conn, self.chat_id, self.user)
            for row in rows:
                if looks_like_references(row["text"]):
                    references += 1
                    continue
                vector, dated = None, False
                if tag and row["vector"] is not None:
                    made_by = str(row["model"] or "").split("#", 1)[0]
                    if row["model"] == tag or made_by == self.embedding_model:
                        vector = _normalised(store.unpack(row["vector"]))
                        dated = row["model"] != tag
                paper = {**row["paper"], "key": row["paper_key"]}
                docs.append(
                    _Doc(row["text"], paper, row["file_id"], row["page"], vector, row["id"], dated)
                )
            with_text = (
                await store.project_files(self.conn, self.project_id) if self.project_id else set()
            )
            for row in await self._relevant():
                if row["id"] in with_text:
                    continue
                data = {**row["data"], "key": row["key"]}
                abstract = (data.get("abstract") or data.get("tldr") or "").strip()
                if abstract and opens_at(data):
                    docs.append(_Doc(f"{data.get('title', '')}. {abstract}", data))
            for index, doc in enumerate(docs):
                self._by_place.setdefault(doc.place, []).append(index)
                words = terms(doc.text)
                doc.tf = Counter(words)
                doc.length = len(words)
                self._df.update(doc.tf.keys())
            self._avg = (sum(d.length for d in docs) / len(docs)) if docs else 1.0
            if references:
                log.info("library: %d reference-list passages left out", references)
            self._docs = docs
            return docs

    async def _heal(self, docs: list[_Doc], usage: Usage | None) -> None:
        """Embed what has no usable vector, once per run, before searching.

        Three kinds, in this order: passages with no vector from this model at
        all (another model made theirs), which would otherwise be searched by
        keyword only; the abstracts of relevant papers without a full text,
        which have no row to keep a vector in and are embedded each run, a few
        dozen at most; and passages whose vector is this model's but made the
        older way (`embedder.INDEX_VERSION`), which still search well enough
        and are re-made a slice at a time. What is stored is written back, so
        the next run finds it ready. Without this an abstract was found by
        keyword alone, so a German question never reached an English abstract,
        and a model change left every PDF searched by keyword until someone
        clicked Index on every list.

        Bounded per run (`settings.library_reembed_limit`): a free tier limits
        requests, and a library re-embedded in one go would trip it and get
        nothing at all.
        """
        if self._healed or not self.embedding_model:
            return
        self._healed = True
        wanting = [doc for doc in docs if doc.vector is None and doc.chunk_id]
        wanting += [doc for doc in docs if doc.vector is None and not doc.chunk_id]
        wanting += [doc for doc in docs if doc.dated]
        missing = wanting[: settings.library_reembed_limit]
        if not missing:
            return
        left = sum(1 for doc in wanting[len(missing) :] if doc.vector is None)
        texts = [passage_text(doc.text, title=doc.paper.get("title", "")) for doc in missing]
        try:
            vectors = await embed(
                prepare(texts, self.embedding_model, DOCUMENT),
                model=self.embedding_model,
                provider=self.embedding_provider,
                usage=usage,
            )
        except Exception as exc:  # keywords still search what has no vector
            log.info("library: could not embed missing passages: %s", exc)
            vectors = None
        stale = sum(1 for doc in missing if doc.chunk_id and doc.vector is None)
        if not vectors:
            if stale:
                self.notes.append(
                    f"{stale} passages of your PDFs have no embedding from the current model, "
                    "and the model did not answer, so they were searched by keyword only."
                )
            return
        stored = []
        for doc, vector in zip(missing, vectors, strict=True):
            doc.vector = _normalised(vector)
            doc.dated = False
            if doc.chunk_id:
                stored.append((doc.chunk_id, vector))
        if stored:
            await store.set_vectors(self.conn, stored, vector_tag(self.embedding_model))
            log.info("library: re-embedded %d passages before searching", len(stored))
        if stale:
            self.notes.append(
                f"Indexed {stale} passages of your PDFs for the current embedding model "
                "before searching; the next question finds them ready."
            )
        rest = [doc for doc in wanting[len(missing) :] if doc.chunk_id]
        if rest and not reindexing(self.key):
            _BACKGROUND[self.key] = asyncio.create_task(
                self._embed_in_background(
                    [(doc.chunk_id, doc.text, doc.paper.get("title", "")) for doc in rest]
                )
            )
        if left:
            self.notes.append(
                f"{left} more passages are being embedded in the background and were searched "
                "by keyword only this time; questions asked once that is done search them "
                "fully. The Literature page shows how far it has got."
            )

    async def _embed_in_background(self, pending: list[tuple[int, str, str]]) -> None:
        """Embed the passages a run had no room for, a slice at a time, in
        series, writing each slice back as it lands.

        Left to the next question (512 per run), a library of 4,700 passages
        would take ten questions to search fully again after a model change.
        In series, so a rate-limited tier sees one request at a time
        rather than all of them at once. A slice that fails ends the work; the
        next run starts it again from wherever it got to. Never raises: there
        is no one left to raise to.
        """
        model, provider = self.embedding_model, self.embedding_provider
        if not model:
            return
        size = max(1, settings.library_reembed_limit)
        done = 0
        try:
            for start in range(0, len(pending), size):
                part = pending[start : start + size]
                texts = [passage_text(text, title=title) for _, text, title in part]
                vectors = await embed(
                    prepare(texts, model, DOCUMENT), model=model, provider=provider
                )
                if not vectors or len(vectors) != len(part):
                    log.info("library: background embedding stopped after %d passages", done)
                    return
                await store.set_vectors(
                    self.conn,
                    [(chunk_id, v) for (chunk_id, _, _), v in zip(part, vectors, strict=True)],
                    vector_tag(model),
                )
                done += len(part)
            log.info("library: embedded %d passages in the background", done)
        except Exception as exc:  # a background task has nobody to tell but the log
            log.warning("library: background embedding failed after %d passages: %s", done, exc)
        finally:
            _BACKGROUND.pop(self.key, None)

    async def _relevant(self) -> list[dict[str, Any]]:
        if not self.project_id:
            return []
        return await literature.relevant_for_project(self.conn, self.project_id)

    async def attached(self) -> list[dict[str, Any]]:
        """The files attached to the chat, as `attachments.as_paper` dicts."""
        if not self.chat_id:
            return []
        files = await attachments.files(self.conn, self.chat_id, self.user)
        return [attachments.as_paper(f["id"], f["name"], f["kind"], f["pages"]) for f in files]

    @property
    def label(self) -> str:
        """What the corpus is called where the run names it."""
        if self.project_id:
            return "Your papers"
        return "Your files"

    @property
    def nothing_found(self) -> str:
        """Said in place of an answer when nothing here bears on the question."""
        if self.project_id:
            files = " or in the files attached to this chat" if self.chat_id else ""
            return (
                f"Nothing in your papers{files} answers this, so no answer was written. Only "
                "the full texts and abstracts of papers marked relevant are searched: upload "
                "more full texts on the Literature page, mark more papers relevant, or switch "
                "to Web."
            )
        return (
            "Nothing in the files attached to this chat answers this, so no answer was "
            "written. Switch to Web to look beyond them. A scanned PDF has no text to "
            "search; the file's chip says so when that is the case."
        )

    # ------------------------------------------------------- the whole library
    #
    # A search answers a question one passage answers. "Write the introduction"
    # is not one: no passage matches it, the semantic floor rightly finds
    # nothing, and the person had to ask paper by paper. So the run is told
    # what the library holds, cheaply and always (`catalog`, titles only, for
    # writing queries that hit), and for a request about the collection as a
    # whole it is handed every paper's abstract as a source (`overview`).

    async def papers(self) -> list[dict[str, Any]]:
        """The relevant papers, once each, with their abstract and full text."""
        if self._papers is not None:
            return self._papers
        files = (
            await store.project_first_pages(self.conn, self.project_id) if self.project_id else {}
        )
        seen: set[str] = set()
        papers = []
        for row in await self._relevant():
            data = row["data"]
            key = (data.get("doi") or data.get("title") or row["key"] or "").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            papers.append({**data, "key": row["key"], "file_id": files.get(row["id"], "")})
        self._papers = papers
        return papers

    async def catalog(self, limit: int = CATALOG_LIMIT) -> str:
        """One line per relevant paper, author, year and title, for the calls
        that write queries. About fifteen tokens a paper: the price of knowing
        what is there, never the abstracts."""
        papers = await self.papers()
        lines = [f"- {_byline(p)}: {(p.get('title') or 'Untitled')[:140]}" for p in papers[:limit]]
        if len(papers) > limit:
            lines.append(f"- … and {len(papers) - limit} more")
        files = await self.attached()
        if files:
            lines.append("Attached to this chat by the person:")
            lines += [f"- {_described(f)}" for f in files]
        return "\n".join(lines)

    async def fits_whole(self) -> bool:
        """Whether the attachments are short enough to be read whole for any
        question (`WHOLE_PAGES`). Only in a chat that is about them alone: in a
        literature project they are beside a library, and the search decides."""
        if self.project_id:
            return False
        files = await self.attached()
        return bool(files) and sum(int(f["pages"] or 0) for f in files) <= WHOLE_PAGES

    async def overview(
        self, limit: int = OVERVIEW_LIMIT, *, papers: bool = True
    ) -> list[SearchResult]:
        """Every relevant paper's abstract as a source, for a request about the
        collection as a whole. Linked to the PDF where there is one
        (`opens_at`); a paper with no link at all cannot be opened, and a
        source that cannot be opened cannot be checked.

        Then the attached files themselves, page by page, first: a file has no
        abstract, and "summarise this" is answered from what it says. Without
        `papers`, only those, for a chat whose files are read whole anyway
        (`fits_whole`)."""
        results = await self._attached_pages()
        if not papers:
            return results
        for paper in (await self.papers())[:limit]:
            abstract = (paper.get("abstract") or paper.get("tldr") or "").strip()
            url = opens_at(paper)
            if not abstract or not url:
                continue
            doc = _Doc(_trim(abstract, OVERVIEW_CHARS), paper)
            result = await self._result(doc, 0.5)
            result.title = f"{paper.get('title') or 'Paper'} (abstract)"
            result.url = url
            result.reasons = ("the paper's abstract, for an overview of your library",)
            results.append(result)
        return results

    async def _attached_pages(self) -> list[SearchResult]:
        """The pages of the attached files, every one if they fit
        `OVERVIEW_PAGES`, else that many spread over each file."""
        docs = await self._load()
        pages: dict[str, list[_Doc]] = {}
        seen: set[Any] = set()
        for doc in docs:
            if doc.paper.get("attachment") and doc.place not in seen:
                seen.add(doc.place)
                pages.setdefault(doc.file_id, []).append(doc)
        if not pages:
            return []
        share = max(2, OVERVIEW_PAGES // len(pages))
        results = []
        for found in pages.values():
            found.sort(key=lambda d: d.page)
            for doc in _spread(found, share):
                result = await self._result(doc, 0.5)
                result.reasons = ("a file attached to this chat, read as a whole",)
                results.append(result)
        return results

    async def size(self) -> tuple[int, int]:
        """(pages of full text, abstracts) the library can search."""
        docs = await self._load()
        pages = {doc.place for doc in docs if doc.file_id}
        return len(pages), sum(1 for doc in docs if not doc.file_id)

    def _bm25(self, docs: list[_Doc], query: str) -> list[int]:
        """Indices of the passages that contain enough of the query's terms,
        best first."""
        wanted = set(terms(query))
        if not wanted:
            return []
        n = len(docs)
        needed = max(1, math.ceil(len(wanted) * LEXICAL_COVERAGE))
        scores: list[tuple[float, int]] = []
        for index, doc in enumerate(docs):
            if sum(1 for term in wanted if term in doc.tf) < needed:
                continue
            score = 0.0
            for term in wanted:
                tf = doc.tf.get(term)
                if not tf:
                    continue
                df = self._df[term]
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                norm = tf + _K1 * (1 - _B + _B * doc.length / self._avg)
                score += idf * tf * (_K1 + 1) / norm
            if score > 0:
                scores.append((score, index))
        scores.sort(reverse=True)
        return [index for _, index in scores[:_DEPTH]]

    def _semantic(self, docs: list[_Doc], vector: Any) -> list[int]:
        """Indices of the passages close to a query's meaning, best first, or
        none at all when nothing in the library is.

        A nearest-neighbour search always has a nearest neighbour. Without a
        floor, a question the papers say nothing about still comes back with
        six "best" pages, and the writer, handed six pages, answers anyway,
        that is how a source-bound chat ends up stating the height of the
        Eiffel Tower. So a query must single some passages out from the rest
        (`SEMANTIC_GAP`), and only those it singles out count.
        """
        scored = [
            (_cosine(vector, doc.vector), index)
            for index, doc in enumerate(docs)
            if doc.vector is not None
        ]
        if not scored:
            return []
        scored.sort(reverse=True)
        top = scored[0][0]
        median = scored[len(scored) // 2][0]
        gap = settings.library_semantic_gap
        if self._on_topic:
            gap *= SEMANTIC_ON_TOPIC
        passed = top - median >= gap
        self.probes.append({"top": round(top, 4), "median": round(median, 4), "passed": passed})
        if not passed:
            return []
        self._on_topic = True
        floor = median + (top - median) * SEMANTIC_SHARE
        return [index for score, index in scored[:_DEPTH] if score >= floor]

    def _say_how_close(self, probes: list[dict[str, Any]]) -> None:
        """Found nothing: say how near the nearest passage came, in the
        transcript. An empty result is right for a question the papers do not
        cover, and the symptom of a bar set too high for a narrow corpus when
        they do; these numbers are what tells the two apart."""
        # Once per run. Sub-questions are searched side by side, and three of
        # them missing printed three near-identical paragraphs; the run's
        # closing notice already names every sub-question that found nothing.
        if not probes or any(n.startswith(_HOW_CLOSE) for n in self.notes):
            return
        best = max(probes, key=lambda p: p["top"] - p["median"])
        gap = settings.library_semantic_gap
        self.notes.append(
            f"{_HOW_CLOSE} The nearest passage stood "
            f"{best['top'] - best['median']:.2f} above a typical one (similarity "
            f"{best['top']:.2f} against {best['median']:.2f}); the bar is {gap:.2f}. If your "
            "papers do cover this, the bar is too high for this project "
            "(LIBRARY_SEMANTIC_GAP, see tools/calibrate_library.py)."
        )

    def fresh_notes(self) -> list[str]:
        """The notes not handed out yet. Every search adds to `notes`, and
        reading the whole list after each one repeated the earlier ones."""
        fresh, self._said = self.notes[self._said :], len(self.notes)
        return fresh

    async def _vectors(self, queries: list[str], usage: Usage | None) -> list[Any] | None:
        if not self.embedding_model:
            return None
        vectors = await embed(
            prepare(queries, self.embedding_model, QUERY),
            model=self.embedding_model,
            provider=self.embedding_provider,
            usage=usage,
        )
        if not vectors:
            return None
        return [_normalised(v) for v in vectors]

    async def search(
        self, queries: list[str], *, limit: int, usage: Usage | None = None
    ) -> list[SearchResult]:
        """The pages that bear on these queries, best first, at most `limit`."""
        docs = await self._load()
        queries = [q for q in dict.fromkeys(q.strip() for q in queries) if q]
        if not docs or not queries:
            return []

        await self._heal(docs, usage)
        rankings = [self._bm25(docs, query) for query in queries]
        probed = len(self.probes)
        if any(doc.vector is not None for doc in docs):
            try:
                vectors = await self._vectors(queries, usage)
            except Exception as exc:  # keywords alone still search
                log.info("library: could not embed the queries: %s", exc)
                vectors = None
            if vectors is None:
                note = (
                    "The embedding model did not answer, so your papers were searched "
                    "by keyword only."
                )
                if note not in self.notes:
                    self.notes.append(note)
            else:
                usable = [v for v in vectors if v is not None]
                found = [self._semantic(docs, v) for v in usable]
                if self._on_topic:
                    # A query judged before another of the same call cleared
                    # the full bar is judged again against the lowered one.
                    found = [
                        ranking or self._semantic(docs, v)
                        for ranking, v in zip(found, usable, strict=True)
                    ]
                rankings += found

        fused: dict[int, float] = {}
        for ranking in rankings:
            for rank, index in enumerate(ranking):
                fused[index] = fused.get(index, 0.0) + 1.0 / (_RRF + rank + 1)
        if not fused:
            self._say_how_close(self.probes[probed:])
            return []

        # Best passage per page, then pages in order of their best passage.
        best: dict[Any, tuple[float, int]] = {}
        hits: dict[Any, list[tuple[float, int]]] = {}
        for index, score in fused.items():
            place = docs[index].place
            hits.setdefault(place, []).append((score, index))
            if place not in best or score > best[place][0]:
                best[place] = (score, index)
        ordered = sorted(best.values(), reverse=True)
        top = ordered[0][0]

        results: list[SearchResult] = []
        per_paper: Counter = Counter()
        for score, index in ordered:
            doc = docs[index]
            paper_key = doc.paper.get("key") or doc.paper.get("title")
            cap = PAGES_PER_ATTACHMENT if doc.paper.get("attachment") else PAGES_PER_PAPER
            if per_paper[paper_key] >= cap:
                continue
            per_paper[paper_key] += 1
            # Scaled into the range a relevant web result occupies, so the
            # library is neither discounted nor inflated once it is ranked
            # beside the web.
            relevance = 0.4 + 0.6 * score / top
            matched = [i for _, i in sorted(hits[doc.place], reverse=True)]
            rest = [i for i in self._by_place.get(doc.place, []) if i not in matched]
            result = await self._result(doc, relevance)
            result.excerpts = [docs[i].text for i in matched + rest]
            result.matched = len(matched)
            results.append(result)
            if len(results) >= limit:
                break
        return results

    async def _result(self, doc: _Doc, relevance: float) -> SearchResult:
        paper = doc.paper
        title = paper.get("title") or "Paper"
        if paper.get("attachment"):
            pdf = paper.get("kind") == "pdf"
            result = SearchResult(
                title=f"{title} (PDF p. {doc.page})" if pdf else f"{title} (part {doc.page})",
                url=PAGE_URL.format(file_id=doc.file_id, page=doc.page),
                snippet=doc.text[:300],
                content=await store.page_text(self.conn, doc.file_id, doc.page),
                engine=ENGINE,
                engines=[ENGINE],
                kind=KIND_PDF if pdf else KIND_PAGE,
                relevance=relevance,
            )
            # The thing itself: the person attached it to ask about it, and
            # nothing the web holds is a better source for what it says.
            result.tier = quality.PRIMARY
            result.trust = quality.TIER_TRUST[quality.PRIMARY]
            # A file has no authors; it is named by its name, without the
            # extension, which reads as noise inside a citation.
            result.byline = re.sub(r"\.(pdf|docx|md|markdown|txt)$", "", title, flags=re.I)[:60]
            where = f"page {doc.page}" if pdf else f"part {doc.page}"
            result.reasons = (f"{where} of a file attached to this chat",)
            return result
        if doc.file_id:
            content = await store.page_text(self.conn, doc.file_id, doc.page)
            title = f"{title} (PDF p. {doc.page})"
            url = PAGE_URL.format(file_id=doc.file_id, page=doc.page)
            reason = f"page {doc.page} of the full text you uploaded"
        else:
            content = doc.text
            title = f"{title} (abstract only)"
            url = opens_at(paper)
            reason = "abstract only: no full text uploaded for this paper"
        result = SearchResult(
            title=title,
            url=url,
            snippet=doc.text[:300],
            content=content,
            engine=ENGINE,
            engines=[ENGINE],
            published=str(paper.get("year") or ""),
            authors=list(paper.get("authors") or [])[:6],
            doi=paper.get("doi") or "",
            journal=paper.get("venue") or "",
            kind=KIND_PAPER,
            relevance=relevance,
        )
        result.tier = quality.ACADEMIC
        result.trust = quality.TIER_TRUST[quality.ACADEMIC]
        result.reasons = (reason,)
        result.byline = _byline(paper) if paper.get("authors") else _short(paper)
        return result


def _short(paper: dict[str, Any]) -> str:
    """A paper with no authors on record, named by its title's first words and
    its year, so its citation still says which paper it is."""
    words = str(paper.get("title") or "Paper").split()
    title = " ".join(words[:5]) + ("…" if len(words) > 5 else "")
    return " ".join(part for part in (title, str(paper.get("year") or "").strip()) if part)


def opens_at(paper: dict[str, Any]) -> str:
    """Where a paper's citation opens: its PDF, never its landing page when
    there is a PDF to open.

    The person's own copy first, on its first page in the reader; then the
    open-access PDF a database named (`pdf`); only then the paper's page. A
    click on a source in a literature project is a request to read the paper,
    and a publisher's landing page with an abstract and a paywall is the one
    place that request does not get answered. The overview used to prefer the
    link, so an introduction written from abstracts sent every click to the
    publisher even for papers whose full text was uploaded."""
    if paper.get("file_id"):
        return PAGE_URL.format(file_id=paper["file_id"], page=1)
    return paper.get("pdf") or paper.get("link") or paper.get("url") or ""


def _described(file: dict[str, Any]) -> str:
    """ "Contract.pdf (PDF, 12 pages)", a line of the catalog."""
    count = int(file.get("pages") or 0)
    if file.get("kind") == "pdf":
        what = f"PDF, {count} page{'s' if count != 1 else ''}"
    else:
        what = f"{count} part{'s' if count != 1 else ''} of about a page"
    return f"{(file.get('title') or 'file')[:140]} ({what})"


def _spread(docs: list[_Doc], count: int) -> list[_Doc]:
    """`count` of these pages: all of them if there are no more, otherwise the
    first half of the budget from the start, where a document says what it is,
    and the rest evenly over what follows, ending on the last page, where it
    usually says what it concluded."""
    if len(docs) <= count:
        return docs
    head = docs[: count // 2]
    rest = docs[count // 2 :]
    wanted = count - len(head)
    step = (len(rest) - 1) / max(1, wanted - 1)
    picked = sorted({round(i * step) for i in range(wanted)})
    return head + [rest[i] for i in picked]
