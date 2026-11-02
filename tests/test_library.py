"""Full texts and literature projects: a PDF goes in, is matched, stored, cut
into pages and passages, searched, and cited by page, and a chat in a project
with a reading list answers from those papers alone unless the web is on.

The PDFs are built here, by hand, with a text layer pypdf can read: the suite
must not depend on a file someone checked in, and a three-line PDF is the
smallest thing that still exercises the real extractor.
"""

from __future__ import annotations

import asyncio
import zlib

import pytest
from httpx import ASGITransport, AsyncClient
from test_agent import FakeLLM, collect, fake_report, fake_results, request

from app.agent import orchestrator, research, verify
from app.agent.verify import Claim
from app.main import app
from app.scholar import fulltext, sources
from app.scholar.paper import Paper
from app.search import base, library
from app.search.base import SearchResult
from app.store import library as library_store
from app.store import literature as store
from app.store import projects
from app.store.db import DEFAULT_USER
from app.store.stream import RunStream
from app.tools import fulltext as fulltext_tool

# --------------------------------------------------------------------- a PDF


def make_pdf(pages: list[str]) -> bytes:
    """A valid PDF, one page per string, one line per newline."""

    def escape(line: str) -> str:
        return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    objects: list[bytes] = []
    count = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(count))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(pages):
        lines = "".join(f"({escape(line)}) Tj 0 -14 Td " for line in text.split("\n"))
        stream = f"BT /F1 10 Tf 40 800 Td {lines}ET".encode()
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")

    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


FL = "Federated Learning for Smart Grid Forecasting"

FILLER = (
    "Federated learning lets many clients train one model without sharing raw data. "
    "Each client computes an update on its own records and only the update leaves it. "
)


def paper_pdf(title: str, doi: str = "", body: str = "", pages: int = 3) -> bytes:
    first = f"{title}\nA. Author and B. Author\n" + (f"DOI: {doi}\n" if doi else "")
    first += (FILLER * 3).strip()
    rest = [(body or FILLER * 4).strip() for _ in range(pages - 1)]
    return make_pdf([first, *rest])


# ------------------------------------------------------------------ reading


def test_text_is_read_page_by_page_and_broken_words_are_joined():
    data = make_pdf(["The attention mecha-\nnism is central. " * 10, "Second page text. " * 20])
    extracted = fulltext.extract(data)
    assert len(extracted.pages) == 2
    assert "mechanism" in extracted.pages[0]
    assert "Second page" in extracted.pages[1] and "Second page" not in extracted.pages[0]
    assert extracted.warnings == []


def test_a_pdf_without_text_is_reported_as_a_scan():
    assert fulltext.SCANNED in fulltext.extract(make_pdf(["", "", ""])).warnings


def test_something_that_is_not_a_pdf_is_refused():
    with pytest.raises(fulltext.FulltextError):
        fulltext.extract(b"<html>a login page</html>")


def test_passages_never_cross_a_page():
    pieces = fulltext.chunks(["word " * 400, "other " * 50])
    assert {p.page for p in pieces} == {1, 2}
    assert all(len(p.text) <= fulltext.CHUNK_CHARS for p in pieces)
    assert all("other" not in p.text for p in pieces if p.page == 1)
    # Overlapping, so a sentence cut at a boundary is whole in one of them.
    first, second = [p.text for p in pieces if p.page == 1][:2]
    assert first[-40:].split()[-1] in second


# ----------------------------------------------------------------- matching


def _rows(*papers):
    return [{"id": i, "data": data} for i, data in enumerate(papers, start=1)]


PAPERS = _rows(
    {"title": FL, "doi": "10.1109/tsg.2021.1"},
    {"title": "Attention Is All You Need in Load Prediction", "arxiv": "2106.09685"},
    {"title": "Privacy Preserving Energy Analytics at Scale", "doi": ""},
)


def test_a_pdf_is_matched_by_the_doi_on_its_first_page():
    extracted = fulltext.extract(paper_pdf("Something else entirely", doi="10.1109/TSG.2021.1"))
    found = fulltext.match(extracted, "download.pdf", PAPERS)
    assert (found.paper_id, found.how) == (1, "doi")


def test_the_filename_counts_as_an_identifier():
    extracted = fulltext.extract(paper_pdf("No identifiers printed here at all"))
    found = fulltext.match(extracted, "2106.09685v2.pdf", PAPERS)
    assert (found.paper_id, found.how) == (2, "arxiv")


def test_a_pdf_is_matched_by_its_full_title():
    extracted = fulltext.extract(paper_pdf("Privacy-Preserving Energy Analytics at Scale"))
    assert fulltext.match(extracted, "x.pdf", PAPERS).paper_id == 3


def test_the_reference_list_does_not_identify_a_paper():
    """A DOI cited on page 3 is another paper's. Only the first pages count."""
    data = make_pdf(
        ["An unrelated title\n" + FILLER * 3, FILLER * 3, "References\n10.1109/tsg.2021.1"]
    )
    found = fulltext.match(fulltext.extract(data), "x.pdf", PAPERS)
    assert found.paper_id is None and found.candidates == []


def test_two_candidates_mean_nobody_decides_but_the_person():
    data = paper_pdf(FL, doi="")
    twins = _rows(
        {"title": FL},
        {"title": FL},
    )
    found = fulltext.match(fulltext.extract(data), "x.pdf", twins)
    assert found.paper_id is None and found.candidates == [1, 2]


# ------------------------------------------------------------------- storing


@pytest.fixture
def offline_embed(monkeypatch):
    """Vectors from keywords, so the meaning of a passage survives a language
    change the way a real multilingual model's would."""
    concepts = [
        ("attention", "aufmerksamkeit"),
        ("privacy", "datenschutz"),
        ("grid", "netz"),
        ("aggregation", "aggregierung"),
    ]

    async def embed(texts, *, model, provider="openrouter", usage=None):
        out = []
        for text in texts:
            lower = text.lower()
            # Plus a little text-dependent noise, as a real model has: a text
            # about none of the concepts must not land on one shared point.
            noise = [((zlib.crc32(lower.encode()) >> shift) % 7) / 60 for shift in (0, 5, 10)]
            out.append([float(any(w in lower for w in words)) for words in concepts] + noise)
        return out

    monkeypatch.setattr(fulltext_tool, "embed", embed)
    monkeypatch.setattr(library, "embed", embed)
    return embed


async def _project_list(db, *titles, decision="relevant"):
    project = await projects.create(db, "Thesis", user=DEFAULT_USER)
    collection = await store.create(db, "Reading", project_id=project["id"], user=DEFAULT_USER)
    await store.add_papers(
        db,
        collection["id"],
        [
            {
                "key": f"k{i}",
                "data": {
                    "title": t,
                    "doi": f"10.1000/{i}",
                    "link": f"https://doi.org/10.1000/{i}",
                    "abstract": f"Abstract of {t}.",
                    "year": 2020 + i,
                },
            }
            for i, t in enumerate(titles)
        ],
        round_no=1,
    )
    rows = await store.list_papers(db, collection["id"])
    for row in rows:
        await store.decide(db, collection["id"], row["id"], decision=decision)
    return project, collection, rows


async def test_an_upload_is_matched_stored_and_indexed(db, offline_embed):
    _p, c, rows = await _project_list(db, FL)
    data = paper_pdf(FL, doi="10.1000/0")

    result = await fulltext_tool.upload(db, c, "paper.pdf", data, user=DEFAULT_USER)

    assert result["new"] is True
    assert result["file"]["paper_id"] == rows[0]["id"]
    assert result["file"]["match"] == "doi"
    assert result["file"]["pages"] == 3
    assert result["index"]["embedded"] > 0 and result["index"]["waiting"] == 0
    [listed] = await library_store.list_files(db, c["id"])
    assert listed["embedded"] == listed["chunks"] > 0


async def test_the_same_pdf_twice_is_one_file(db, offline_embed):
    _p, c, _rows = await _project_list(db, FL)
    data = paper_pdf(FL)
    await fulltext_tool.upload(db, c, "a.pdf", data, user=DEFAULT_USER)
    again = await fulltext_tool.upload(db, c, "b.pdf", data, user=DEFAULT_USER)
    assert again["new"] is False
    assert len(await library_store.list_files(db, c["id"])) == 1


async def test_a_wrong_manual_assignment_is_flagged_and_a_second_pdf_releases_the_first(
    db, offline_embed
):
    _p, c, rows = await _project_list(db, FL, "Privacy Preserving Energy Analytics")
    first = await fulltext_tool.upload(
        db,
        c,
        "a.pdf",
        paper_pdf(FL),
        user=DEFAULT_USER,
    )
    assert first["file"]["paper_id"] == rows[0]["id"]

    wrong = await fulltext_tool.upload(
        db,
        c,
        "b.pdf",
        paper_pdf("Something else entirely"),
        user=DEFAULT_USER,
        paper_id=rows[0]["id"],
    )
    assert fulltext.TITLE_NOT_FOUND in wrong["file"]["warnings"]
    files = {f["id"]: f for f in await library_store.list_files(db, c["id"])}
    # The first PDF is released, not deleted.
    assert files[first["file"]["id"]]["paper_id"] is None
    assert files[wrong["file"]["id"]]["paper_id"] == rows[0]["id"]


async def test_deleting_the_last_copy_deletes_the_bytes(db, offline_embed):
    _p, c, _rows = await _project_list(db, FL)
    result = await fulltext_tool.upload(
        db,
        c,
        "a.pdf",
        paper_pdf(FL),
        user=DEFAULT_USER,
    )
    assert await library_store.pdf_of(db, result["file"]["id"]) is not None
    assert await library_store.delete_file(db, c["id"], result["file"]["id"])
    async with db.execute("SELECT COUNT(*) AS n FROM lit_blobs") as cursor:
        assert (await cursor.fetchone())["n"] == 0


async def test_without_an_embedding_model_the_upload_says_so(db, monkeypatch):
    from app.store import settings_store

    saved = await settings_store.load(db, DEFAULT_USER)
    await settings_store.save(
        db, saved.model_copy(update={"embedding_enabled": False}), DEFAULT_USER
    )
    _p, c, _rows = await _project_list(db, FL)
    result = await fulltext_tool.upload(
        db,
        c,
        "a.pdf",
        paper_pdf(FL),
        user=DEFAULT_USER,
    )
    assert result["index"]["embedded"] == 0
    assert "keyword" in result["index"]["error"]


# ------------------------------------------------------- a paper of one's own

OWN = "Load Shifting in Rural Microgrids Under Tariff Uncertainty"


@pytest.fixture
def registry(monkeypatch):
    """OpenAlex, offline: `records` is what it would answer, `calls` what it
    was asked, and `fail` makes it fall over the way a network does."""
    state = {"records": [], "calls": [], "fail": False}

    async def by_dois(dois):
        state["calls"].append(("doi", dois))
        if state["fail"]:
            raise sources.SourceError("OpenAlex answered HTTP 503")
        return list(state["records"])

    async def search(source, query, *, limit, years):
        state["calls"].append(("search", query))
        if state["fail"]:
            raise sources.SourceError("OpenAlex answered HTTP 503")
        return list(state["records"])

    monkeypatch.setattr(fulltext_tool.sources, "openalex_by_dois", by_dois)
    monkeypatch.setattr(fulltext_tool.sources, "search", search)
    return state


def test_a_pdf_describes_itself_by_its_first_line_and_the_doi_it_prints():
    described = fulltext.describe(
        fulltext.extract(paper_pdf(OWN, doi="10.5555/own.1")), "scan_0042.pdf"
    )
    assert described.title == OWN and described.how == "page"
    assert described.doi == "10.5555/own.1"


def test_a_title_that_is_a_file_name_is_not_taken():
    extracted = fulltext.Extracted(pages=["x"], meta_title="Microsoft Word - draft3.docx")
    described = fulltext.describe(extracted, "my_thesis_chapter.pdf")
    assert described.title == "my thesis chapter" and described.how == "file"


async def test_an_own_paper_becomes_a_relevant_paper_the_project_chat_searches(
    db, offline_embed, registry
):
    project, c, _rows = await _project_list(db, FL)
    data = make_pdf(
        [
            f"{OWN}\nC. Author\n" + FILLER * 3,
            "Secure aggregation hides each household's load from the operator. " * 8,
        ]
    )
    result = await fulltext_tool.upload(db, c, "own.pdf", data, user=DEFAULT_USER, own=True)

    assert result["described"] == "page"
    paper = await store.get_paper(db, c["id"], result["file"]["paper_id"])
    assert paper["data"]["title"] == OWN
    assert paper["origin"] == "upload" and paper["decision"] == "relevant"
    assert result["file"]["match"] == "upload"
    assert result["index"]["waiting"] == 0

    [top, *_] = await library.Library(db, project["id"]).search(
        ["secure aggregation household load"], limit=5
    )
    assert top.url.endswith("/2") and OWN[:20] in top.title


async def test_an_own_paper_takes_the_registry_record_only_when_its_title_is_on_the_page(
    db, offline_embed, registry
):
    _p, c, _rows = await _project_list(db, FL)
    # A DOI in the abstract names someone else's paper.
    registry["records"] = [Paper(title="An Entirely Different Study of Something", doi="10.5555/x")]
    stranger = await fulltext_tool.upload(
        db, c, "a.pdf", paper_pdf(OWN, doi="10.5555/x"), user=DEFAULT_USER, own=True
    )
    assert stranger["described"] == "page"

    registry["records"] = [
        Paper(
            title="Peer Review Under Pressure in Energy Journals",
            authors=["D. Writer"],
            year=2023,
            doi="10.5555/y",
        )
    ]
    known = await fulltext_tool.upload(
        db,
        c,
        "b.pdf",
        paper_pdf("Peer Review Under Pressure in Energy Journals", doi="10.5555/y"),
        user=DEFAULT_USER,
        own=True,
    )
    assert known["described"] == "registry"
    paper = await store.get_paper(db, c["id"], known["file"]["paper_id"])
    assert paper["data"]["authors"] == ["D. Writer"] and paper["data"]["year"] == 2023
    assert paper["key"] == "doi:10.5555/y"


async def test_an_own_paper_already_in_the_list_is_filed_there_not_added_twice(
    db, offline_embed, registry
):
    _p, c, rows = await _project_list(db, FL, decision="maybe")
    result = await fulltext_tool.upload(
        db, c, "a.pdf", paper_pdf(FL, doi="10.1000/0"), user=DEFAULT_USER, own=True
    )
    assert result["file"]["paper_id"] == rows[0]["id"] and result["described"] == ""
    assert len(await store.list_papers(db, c["id"])) == 1
    # Brought as a source, so it is one now.
    assert (await store.get_paper(db, c["id"], rows[0]["id"]))["decision"] == "relevant"
    assert registry["calls"] == []


async def test_an_own_paper_is_added_when_the_registry_is_down(db, offline_embed, registry):
    _p, c, _rows = await _project_list(db, FL)
    registry["fail"] = True
    result = await fulltext_tool.upload(
        db, c, "own.pdf", paper_pdf(OWN), user=DEFAULT_USER, own=True
    )
    assert result["described"] == "page" and result["file"]["paper_id"] is not None


async def test_a_waiting_pdf_can_be_made_a_paper_of_its_own(client, db, offline_embed, registry):
    _p, c, _rows = await _project_list(db, FL)
    waiting = await fulltext_tool.upload(db, c, "own.pdf", paper_pdf(OWN), user=DEFAULT_USER)
    assert waiting["file"]["paper_id"] is None

    response = await client.post(f"/api/literature/{c['id']}/files/{waiting['file']['id']}/paper")
    assert response.status_code == 200
    body = response.json()
    assert body["described"] == "page" and body["file"]["paper_id"] is not None
    titles = [p["data"]["title"] for p in await store.list_papers(db, c["id"])]
    assert OWN in titles
    missing = await client.post(f"/api/literature/{c['id']}/files/{'0' * 32}/paper")
    assert missing.status_code == 404


# ------------------------------------------------------------------ searching


async def _library_with(
    db, offline_embed, pages_by_title: dict[str, list[str]], decision="relevant"
):
    project, c, rows = await _project_list(db, *pages_by_title, decision=decision)
    for row in rows:
        pages = pages_by_title[row["data"]["title"]]
        await fulltext_tool.upload(
            db, c, "x.pdf", make_pdf(pages), user=DEFAULT_USER, paper_id=row["id"]
        )
    return project, c, rows


async def test_the_library_finds_the_page_and_cites_it(db, offline_embed):
    project, _c, _rows = await _library_with(
        db,
        offline_embed,
        {
            FL: [
                "Federated Learning for Smart Grid Forecasting\n" + FILLER * 3,
                "Secure aggregation hides each client update from the server. " * 8,
                FILLER * 3,
            ]
        },
    )
    lib = library.Library(db, project["id"])
    [top, *_] = await lib.search(["secure aggregation server"], limit=5)
    assert top.url.startswith("/library/") and top.url.endswith("/2")
    assert top.title.endswith("(PDF p. 2)")
    assert "Secure aggregation" in top.content
    assert top.tier == "academic" and top.engine == "library"
    assert base.canonical_url(top.url) == top.url
    # How the prose names it, "[Byline, S. 2]" (web/markdown.js).
    assert top.as_source(1)["byline"] == "Federated Learning for Smart Grid… 2020"


async def test_only_relevant_papers_are_searched(db, offline_embed):
    project, _c, _rows = await _library_with(
        db,
        offline_embed,
        {FL: ["Secure aggregation. " * 30]},
        decision="maybe",
    )
    assert await library.Library(db, project["id"]).search(["secure aggregation"], limit=5) == []


async def test_a_relevant_paper_without_a_pdf_is_offered_as_its_abstract(db):
    project, _c, _rows = await _project_list(db, "Privacy Preserving Energy Analytics")
    [result] = await library.Library(db, project["id"]).search(
        ["privacy energy analytics"], limit=5
    )
    assert result.title.endswith("(abstract only)")
    assert result.url == "https://doi.org/10.1000/0"


async def test_one_paper_cannot_fill_the_whole_search(db, offline_embed):
    project, _c, _rows = await _library_with(
        db,
        offline_embed,
        {FL: ["secure aggregation " * 40] * 8},
    )
    found = await library.Library(db, project["id"]).search(["secure aggregation"], limit=8)
    assert len(found) == library.PAGES_PER_PAPER


async def test_embeddings_find_a_passage_in_another_language(db, offline_embed):
    project, _c, _rows = await _library_with(
        db,
        offline_embed,
        {
            FL: [
                "Federated Learning for Smart Grid Forecasting\n" + FILLER * 3,
                "Self attention weighs every token against every other token. " * 8,
            ]
        },
    )
    lexical = library.Library(db, project["id"])
    assert await lexical.search(["Aufmerksamkeit erklären"], limit=3) == []
    semantic = library.Library(db, project["id"], embedding_model="test/embed")
    # The stored vectors were made by "the" model of `models_for`, not this one,
    # so they are stale here: the run embeds them itself, writes them back, and
    # then finds the English page for the German question.
    [found] = await semantic.search(["Aufmerksamkeit erklären"], limit=3)
    assert found.url.endswith("/2")
    assert any("Indexed" in n for n in semantic.notes)
    again = library.Library(db, project["id"], embedding_model="test/embed")
    await again.search(["Aufmerksamkeit erklären"], limit=3)
    assert not any("Indexed" in n for n in again.notes)


# ------------------------------------------------------------------ the run


def library_request(db, project_id, **overrides):
    return request(
        library=library.Library(db, project_id),
        lenses=[],
        **overrides,
    )


@pytest.fixture
def no_web(monkeypatch):
    calls = []

    async def search_all(queries, **kwargs):
        calls.append(queries)
        results = fake_results()
        for result in results:
            result.snippet = "Secure aggregation on the web."
        return results, fake_report(len(queries))

    monkeypatch.setattr(research, "search_all", search_all)
    return calls


@pytest.fixture
def writer(monkeypatch):
    llm = FakeLLM(
        answer="Secure aggregation hides each update [1].",
        classification={
            "standalone_question": "secure aggregation",
            "search_queries": ["secure aggregation"],
        },
    )
    monkeypatch.setattr(orchestrator, "LLM", lambda *a, **k: llm)
    return llm


async def _secure_aggregation_library(db, offline_embed):
    return await _library_with(
        db,
        offline_embed,
        {
            FL: [
                "Federated Learning for Smart Grid Forecasting\n" + FILLER * 3,
                "Secure aggregation hides each client update from the server. " * 8,
            ]
        },
    )


async def test_a_literature_project_answers_from_its_papers_and_never_asks_the_web(
    redis, db, offline_embed, no_web, writer
):
    project, _c, _rows = await _secure_aggregation_library(db, offline_embed)
    stream = RunStream(redis, "lib1", ttl=60)
    await orchestrator.run(stream, library_request(db, project["id"], web=False))

    _events, blocks, done = await collect(stream)
    assert done["status"] == "completed"
    assert no_web == []
    sources = next(b for b in await stream.snapshot() if b["type"] == "sources")
    assert all(s["url"].startswith("/library/") for s in sources["data"]["sources"])
    # Each page knows which of its passages the answer was written from.
    assert all(s.get("passages") for s in sources["data"]["sources"])
    provenance = next(b for b in await stream.snapshot() if b["type"] == "provenance")
    assert [lens["id"] for lens in provenance["data"]["lenses"]] == ["library"]
    # Said as a search, by name, not as "answered without searching".
    search = provenance["data"]["search"]
    assert search["queries"] >= 1
    assert [e["name"] for e in search["engines"]] == ["Your papers"]
    # The writer was told it writes from the reader's papers.
    assert "verification" in blocks


async def test_with_the_web_on_both_are_searched(redis, db, offline_embed, no_web, writer):
    project, _c, _rows = await _secure_aggregation_library(db, offline_embed)
    stream = RunStream(redis, "lib2", ttl=60)
    await orchestrator.run(stream, library_request(db, project["id"], web=True))
    await collect(stream)
    assert no_web, "the web was not searched"
    sources = next(b for b in await stream.snapshot() if b["type"] == "sources")
    urls = [s["url"] for s in sources["data"]["sources"]]
    assert any(u.startswith("/library/") for u in urls) and any(
        u.startswith("https://") for u in urls
    )


async def test_nothing_in_the_papers_means_no_answer_not_the_models_own(
    redis, db, offline_embed, no_web, monkeypatch
):
    project, _c, _rows = await _secure_aggregation_library(db, offline_embed)
    llm = FakeLLM(
        classification={"standalone_question": "volcanoes", "search_queries": ["volcano eruption"]}
    )
    monkeypatch.setattr(orchestrator, "LLM", lambda *a, **k: llm)
    stream = RunStream(redis, "lib3", ttl=60)
    await orchestrator.run(stream, library_request(db, project["id"], web=False))

    _events, _blocks, done = await collect(stream)
    assert done["status"] == "completed"
    assert "stream" not in llm.calls, "the model wrote an answer from its own knowledge"
    notices = [b["data"]["text"] for b in await stream.snapshot() if b["type"] == "notice"]
    assert any("Nothing in your papers" in n for n in notices)


async def test_a_question_the_model_would_answer_itself_is_searched_anyway(
    redis, db, offline_embed, no_web, monkeypatch
):
    project, _c, _rows = await _secure_aggregation_library(db, offline_embed)
    llm = FakeLLM(
        answer="Secure aggregation hides each update [1].",
        classification={"needs_search": False, "search_queries": ["secure aggregation"]},
    )
    monkeypatch.setattr(orchestrator, "LLM", lambda *a, **k: llm)
    stream = RunStream(redis, "lib4", ttl=60)
    await orchestrator.run(stream, library_request(db, project["id"], web=False))
    await collect(stream)
    assert any(b["type"] == "sources" for b in await stream.snapshot())


async def test_a_weather_question_in_a_thesis_project_does_not_reach_a_weather_service(
    redis, db, offline_embed, no_web, writer, monkeypatch
):
    from app.search import weather

    async def lookup(place):
        raise AssertionError("a web service was asked")

    monkeypatch.setattr(weather, "lookup", lookup)
    project, _c, _rows = await _secure_aggregation_library(db, offline_embed)
    stream = RunStream(redis, "lib5", ttl=60)
    await orchestrator.run(
        stream, library_request(db, project["id"], web=False, query="wie wird das wetter in berlin")
    )
    _events, _blocks, done = await collect(stream)
    assert done["status"] == "completed"


# ------------------------------------------------------------------ starting


async def test_a_chat_in_a_project_with_a_reading_list_is_source_bound(redis, db, monkeypatch):
    from app import runs

    seen = {}

    async def fake_run(stream, req):
        seen["request"] = req
        await stream.emit_done("completed")

    monkeypatch.setattr(runs, "orchestrate", fake_run)
    project = await projects.create(db, "Thesis", user=DEFAULT_USER)
    plain = await projects.create(db, "Errands", user=DEFAULT_USER)
    await store.create(db, "Reading", project_id=project["id"], user=DEFAULT_USER)

    await runs.start(redis=redis, db=db, query="q", project_id=project["id"], web=False)
    await asyncio.sleep(0.02)
    bound = seen["request"]
    assert bound.library is not None and bound.web is False and bound.project_sources == []
    assert bound.depth == "auto"

    await runs.start(redis=redis, db=db, query="q", project_id=project["id"], web=True)
    await asyncio.sleep(0.02)
    assert seen["request"].web is True and seen["request"].lenses == ["auto"]

    await runs.start(redis=redis, db=db, query="q", project_id=plain["id"], web=False)
    await asyncio.sleep(0.02)
    assert seen["request"].library is None and seen["request"].web is True


async def test_the_project_list_says_which_projects_are_literature_projects(db, offline_embed):
    _project, c, _rows = await _project_list(db, FL)
    await fulltext_tool.upload(
        db,
        c,
        "a.pdf",
        paper_pdf(FL),
        user=DEFAULT_USER,
    )
    [listed] = await projects.list_projects(db, user=DEFAULT_USER)
    assert (listed["lists"], listed["relevant"], listed["fulltexts"]) == (1, 1, 1)


# ------------------------------------------------------------------ quotations


def _source(content: str) -> SearchResult:
    return SearchResult(title="Paper (PDF p. 2)", url="/library/f/2", content=content)


def test_a_verbatim_quotation_is_confirmed_word_for_word():
    claim = Claim(text="Sie schreiben, „secure aggregation hides each client update“.", sources=[1])
    claim.verdict = verify.SUPPORTED
    verify.check_quotes(
        [claim], {1: _source("... Secure aggregation hides each\nclient update from ...")}
    )
    assert claim.quote == verify.QUOTED and claim.verdict == verify.SUPPORTED


def test_an_altered_quotation_cannot_pass_as_supported():
    claim = Claim(text="They write “secure aggregation conceals every client update”.", sources=[1])
    claim.verdict = verify.SUPPORTED
    verify.check_quotes([claim], {1: _source("Secure aggregation hides each client update.")})
    assert claim.quote == verify.ALTERED and claim.verdict == verify.PARTIAL
    assert "word for word" in claim.note


def test_reasoning_set_apart_is_listed_as_such_not_as_missing_sources():
    answer = (
        'Parser verwerfen die Werte [1: "Log parsers remove the variable part"].\n\n'
        "> Für Ihr Format sollten Sie die Zahlen als eigene Merkmale behalten.\n\n"
        "Ein Vektor ist eine Liste von Zahlen.\n"
    )
    stripped, passages = verify.anchors(answer)
    claims, uncited, own = verify.paragraph_claims(stripped, {1}, passages=passages)
    assert [c.text for c in claims] == ["Parser verwerfen die Werte."]
    assert own == ["Für Ihr Format sollten Sie die Zahlen als eigene Merkmale behalten."]
    assert uncited == ["Ein Vektor ist eine Liste von Zahlen."]


async def test_a_literature_answer_is_written_by_the_literature_model(redis, db, monkeypatch):
    from app import runs
    from app.store import settings_store

    seen = {}

    async def fake_run(stream, req):
        seen["request"] = req
        await stream.emit_done("completed")

    monkeypatch.setattr(runs, "orchestrate", fake_run)
    saved = await settings_store.load(db, DEFAULT_USER)
    chosen = saved.model_copy(
        update={
            "chat_model": "google/gemini-flash-latest",
            "literature_model": "anthropic/claude-opus-latest",
            "literature_provider": "openrouter",
        }
    )
    await settings_store.save(db, chosen, DEFAULT_USER)
    thesis = await projects.create(db, "Thesis", user=DEFAULT_USER)
    errands = await projects.create(db, "Errands", user=DEFAULT_USER)
    await store.create(db, "Reading", project_id=thesis["id"], user=DEFAULT_USER)

    await runs.start(redis=redis, db=db, query="q", project_id=thesis["id"])
    await asyncio.sleep(0.02)
    assert seen["request"].model == "anthropic/claude-opus-latest"
    await runs.start(redis=redis, db=db, query="q", project_id=errands["id"])
    await asyncio.sleep(0.02)
    assert seen["request"].model == "google/gemini-flash-latest"


def test_the_checker_in_a_literature_project_is_the_writers_model():
    from app.agent.context import RunContext

    class Ctx:
        llm, router, judge, library = "strong", "fast", None, object()

    assert RunContext.checker(Ctx) == "strong"
    Ctx.library = None
    assert RunContext.checker(Ctx) == "fast"


def test_a_judge_model_of_its_own_checks_every_answer():
    """Set apart, it rules in literature projects too: a writer judging its
    own answer shares the blind spots that wrote it."""
    from app.agent.context import RunContext

    class Ctx:
        llm, router, judge, library = "writer", "fast", "other family", object()

    assert RunContext.checker(Ctx) == "other family"
    Ctx.library = None
    assert RunContext.checker(Ctx) == "other family"


def test_et_al_does_not_end_a_sentence():
    [claim] = verify.claims_of("Vaswani et al. define self-attention as a mapping [1].", {1})
    assert claim.text.startswith("Vaswani et al. define")


def test_only_evidence_that_is_really_on_the_page_is_highlighted():
    page = _source("Secure aggregation hides each client update from the server.")
    copied = Claim(text="Updates stay hidden.", sources=[1], verdict=verify.SUPPORTED)
    copied.evidence = "hides each client update from the server"
    tidied = Claim(text="Updates stay private.", sources=[1], verdict=verify.SUPPORTED)
    tidied.evidence = "conceals every update from the coordinator"
    assert verify.attach_evidence([copied, tidied], {1: page})
    assert page.evidence_texts == ["hides each client update from the server"]


def test_a_tidied_excerpt_marks_the_pages_own_sentence():
    page = _source(
        "Federated learning is popular. We scale the dot products by 1/√dk to keep "
        "the softmax out of regions with tiny gradients. Results follow."
    )
    claim = Claim(text="Sie skalieren.", sources=[1], verdict=verify.SUPPORTED)
    claim.evidence = "we scale the dot products by sqrt(dk) to keep the softmax out of regions"
    verify.attach_evidence([claim], {1: page})
    assert page.evidence_texts == [
        "We scale the dot products by 1/√dk to keep the softmax out of regions with tiny gradients."
    ]
    assert page.as_source(1)["evidence"] == page.evidence_texts


def test_a_direct_quotation_is_checked_like_any_passage_citation():
    """`[4 quote: "…"]` only changes how the reader sees it: the words are still
    a passage, taken out of the claim and compared with the page."""
    stripped, passages = verify.anchors(
        'Sie schreiben, sie [1 quote: "scale the dot products"] und mehr [2: "other words here"].'
    )
    assert passages == [(1, "scale the dot products"), (2, "other words here")]
    assert stripped == "Sie schreiben, sie [1~0] und mehr [2~1]."


def test_a_passage_citation_is_taken_out_of_the_claim_and_checked_word_for_word():
    answer = (
        'Die Skalierung verhindert kleine Gradienten [1: "we scale the dot products. By '
        'this"]. Allgemein ist das wichtig [1].'
    )
    stripped, passages = verify.anchors(answer)
    assert passages == [(1, "we scale the dot products. By this")]
    claims, uncited, _own = verify.paragraph_claims(stripped, {1}, passages=passages)
    assert uncited == []
    # The full stop inside the passage did not split the sentence.
    assert [c.text for c in claims] == [
        "Die Skalierung verhindert kleine Gradienten.",
        "Allgemein ist das wichtig.",
    ]
    page = _source("To fix it, we scale the dot products. By this the softmax stays sharp.")
    claims[0].verdict = claims[1].verdict = verify.SUPPORTED
    verify.check_passages(claims, {1: page})
    assert claims[0].passage == verify.EXACT and claims[0].verdict == verify.SUPPORTED
    assert claims[1].passage == ""  # a bare number, a "vgl." reference
    assert page.evidence_texts == ["we scale the dot products. By this"]


def test_several_passages_in_one_bracket_are_each_checked():
    answer = (
        'Logs wachsen schnell und ändern sich oft [6: "First, the volume of logs is increasing '
        'rapidly.", "hundreds of new logging statements every month [ 14])."].'
    )
    stripped, passages = verify.anchors(answer)
    assert stripped == "Logs wachsen schnell und ändern sich oft [6~0][6~1]."
    assert passages[1] == (6, "hundreds of new logging statements every month [ 14]).")
    [claim] = verify.claims_of(stripped, {6}, passages=passages)
    assert claim.sources == [6] and len(claim.passages) == 2


def test_a_passage_ending_in_a_quotation_of_its_own_still_cites():
    # From a real answer: `”"]` read as "end, and a next passage starts", the
    # citation failed and stood in the prose as text.
    answer = (
        'Sie schlagen vor, LLM-Werkzeuge als „Fenster“ zu untersuchen [3: "future research '
        'directions for using LLM-based tools as “windows” rather than “walls”"].'
    )
    stripped, passages = verify.anchors(answer)
    assert stripped == "Sie schlagen vor, LLM-Werkzeuge als „Fenster“ zu untersuchen [3~0]."
    assert passages == [
        (3, "future research directions for using LLM-based tools as “windows” rather than “walls”")
    ]


def test_a_passage_may_quote_a_word_and_cite_a_paper_of_its_own():
    """Seen in an answer: the page puts "anomaly" in quotation marks and cites
    [4] itself. Ending the passage at the first inner mark would fail the
    citation and leave it in the prose as raw text."""
    answer = (
        'Buzz nutzt eine iterative MCMC-Imputation [4: "Since our goal is to detect whether '
        'the last data point is an anomaly, we assume it to be "anomaly", and iteratively use '
        "MCMC imputation (also used by Donut [4]) to obtain a reasonable estimation of the "
        'reconstructed x"].'
    )
    stripped, passages = verify.anchors(answer)
    assert stripped == "Buzz nutzt eine iterative MCMC-Imputation [4~0]."
    assert passages[0][1].startswith("Since our goal")
    assert '"anomaly", and' in passages[0][1] and passages[0][1].endswith("reconstructed x")


def test_a_cited_passage_that_is_not_on_the_page_cannot_pass():
    answer = 'Die Produkte werden verdoppelt [1: "we multiply the dot products by two"].'
    stripped, passages = verify.anchors(answer)
    [claim] = verify.claims_of(stripped, {1}, passages=passages)
    claim.verdict = verify.SUPPORTED
    verify.check_passages([claim], {1: _source("we scale the dot products by 1/sqrt(dk)")})
    assert claim.passage == verify.MISSING and claim.verdict == verify.PARTIAL
    assert "not on [1] word for word" in claim.note


def test_a_snippet_is_not_evidence_against_a_quotation():
    claim = Claim(text="They write “secure aggregation conceals every client update”.", sources=[1])
    snippet_only = SearchResult(
        title="Web page", url="https://x.test", snippet="Secure aggregation."
    )
    verify.check_quotes([claim], {1: snippet_only})
    assert claim.quote == "" and claim.verdict == verify.UNCHECKED


async def test_a_question_the_papers_do_not_cover_finds_nothing(db, offline_embed):
    """A nearest neighbour always exists. Without a floor the Eiffel Tower
    gets six "best" pages and the writer answers it."""
    from app.store import settings_store

    project, _c, _rows = await _secure_aggregation_library(db, offline_embed)
    model = (await settings_store.load(db, DEFAULT_USER)).embedding_model
    lib = library.Library(db, project["id"], embedding_model=model)
    assert await lib.search(["Eiffel Tower height"], limit=6) == []
    assert await lib.search(["Wie funktioniert sichere Aggregierung?"], limit=6)


def test_one_shared_word_is_not_a_keyword_hit():
    lib = library.Library(None, "p")  # type: ignore[arg-type]
    docs = [library._Doc("the height of the feature map is halved", {})]
    for doc in docs:
        doc.tf = library.Counter(library.terms(doc.text))
        lib._df.update(doc.tf.keys())
    assert lib._bm25(docs, "Eiffel Tower height") == []
    assert lib._bm25(docs, "feature map height") == [0]


def test_every_sentence_is_a_claim_and_the_untied_ones_are_listed():
    answer = (
        "Der Eiffelturm ist 300 Meter hoch.\n\n"
        "Vaswani et al. definieren es so:\n\n"
        "Aggregation is secure by design. It hides each update [1].\n\n"
        "## Details\n"
    )
    claims, uncited, _own = verify.paragraph_claims(answer, {1})
    assert uncited == ["Der Eiffelturm ist 300 Meter hoch."]
    # The sentence before a cited one in its paragraph is checked against the
    # same source, not waved through.
    assert [(c.text, c.sources, c.inherited) for c in claims] == [
        ("Aggregation is secure by design.", [1], True),
        ("It hides each update.", [1], False),
    ]


async def test_an_answer_from_the_models_own_knowledge_is_flagged(
    redis, db, offline_embed, no_web, monkeypatch
):
    """The backstop behind the search floor: if pages do reach the writer and
    it answers from memory anyway, every uncited sentence is put in front of
    the reader."""
    project, _c, _rows = await _secure_aggregation_library(db, offline_embed)
    llm = FakeLLM(
        answer="Der Eiffelturm ist 300 Meter hoch.",
        classification={"search_queries": ["secure aggregation"]},
    )
    monkeypatch.setattr(orchestrator, "LLM", lambda *a, **k: llm)
    stream = RunStream(redis, "lib6", ttl=60)
    await orchestrator.run(stream, library_request(db, project["id"], web=False))
    await collect(stream)
    [check] = [b for b in await stream.snapshot() if b["type"] == "verification"]
    assert check["data"]["uncited"] == ["Der Eiffelturm ist 300 Meter hoch."]


# ---------------------------------------------------------------------- API


@pytest.fixture
async def client(redis, db, offline_embed):
    app.state.redis = redis
    app.state.db = db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        signed_in = await http.post("/api/profiles", json={"name": DEFAULT_USER})
        assert signed_in.status_code == 200, signed_in.text
        yield http


async def test_upload_list_assign_open_and_delete_over_http(client, db):
    _p, c, rows = await _project_list(db, FL)
    data = paper_pdf("Some other title")
    uploaded = await client.post(
        f"/api/literature/{c['id']}/files",
        content=data,
        headers={"X-Filename": "Some%20paper.pdf", "Content-Type": "application/pdf"},
    )
    assert uploaded.status_code == 200, uploaded.text
    file = uploaded.json()["file"]
    assert file["paper_id"] is None and file["name"] == "Some paper.pdf"

    listed = (await client.get(f"/api/literature/{c['id']}")).json()
    assert [f["id"] for f in listed["files"]] == [file["id"]]

    assigned = await client.patch(
        f"/api/literature/{c['id']}/files/{file['id']}", json={"paper_id": rows[0]["id"]}
    )
    assert assigned.json()["file"]["paper_id"] == rows[0]["id"]
    assert fulltext.TITLE_NOT_FOUND in assigned.json()["file"]["warnings"]

    pdf = await client.get(f"/api/literature/files/{file['id']}/pdf")
    assert pdf.status_code == 200 and pdf.content == data
    assert pdf.headers["content-type"] == "application/pdf"

    # A citation opens the reader, which asks for the file and its paper.
    cited = await client.get(f"/library/{file['id']}/2")
    assert cited.status_code == 200 and "reader.js" in cited.text
    info = (await client.get(f"/api/literature/files/{file['id']}")).json()
    assert info["paper"]["data"]["title"] == FL and info["file"]["pages"] == 3

    assert (await client.delete(f"/api/literature/{c['id']}/files/{file['id']}")).status_code == 200
    assert (await client.get(f"/api/literature/files/{file['id']}/pdf")).status_code == 404


async def test_a_file_is_not_readable_from_another_profile(client, db, offline_embed):
    theirs = await store.create(db, "Theirs", user="someone-else")
    result = await fulltext_tool.upload(
        db, theirs, "a.pdf", paper_pdf("Some other title"), user="someone-else"
    )
    file_id = result["file"]["id"]
    assert (await client.get(f"/api/literature/files/{file_id}/pdf")).status_code == 404
    assert (await client.get(f"/api/literature/files/{file_id}")).status_code == 404
    assert (
        await client.post(f"/api/literature/{theirs['id']}/files", content=b"%PDF")
    ).status_code == 404


async def test_something_that_is_not_a_pdf_is_refused_over_http(client, db):
    c = await store.create(db, "Mine", user=DEFAULT_USER)
    response = await client.post(f"/api/literature/{c['id']}/files", content=b"<html>login</html>")
    assert response.status_code == 422
    assert "not a PDF" in response.json()["detail"]


# ------------------------------------------------------- what the search skips

REFERENCES = (
    "[12] A. Vaswani, N. Shazeer, N. Parmar, J. Uszkoreit and L. Jones, Secure aggregation "
    "for federated learning, in Advances in Neural Information Processing Systems, 2017. "
    "[13] K. He, X. Zhang, S. Ren and J. Sun, Secure aggregation at scale, CVPR, 2016. "
    "[14] J. Devlin, M. Chang, K. Lee and K. Toutanova, Server side aggregation, NAACL, 2019. "
    "[15] T. Brown, B. Mann and N. Ryder, Language models are few-shot learners, 2020. "
    "[16] H. B. McMahan, E. Moore and D. Ramage, Communication-efficient learning, 2017. "
)


def test_a_reference_list_is_told_from_prose_that_cites():
    assert library.looks_like_references(REFERENCES)
    related = (
        "Federated learning was introduced by McMahan et al. (2017), who showed that "
        "averaging client updates converges on non-IID data. Bonawitz et al. (2017) added "
        "secure aggregation so the server sees only the sum. Kairouz et al. (2021) surveyed "
        "open problems, Li et al. (2020) studied heterogeneity, and Fekri et al. (2022) "
        "compared it with centralised baselines on smart meter data from 2019 to 2021."
    )
    assert not library.looks_like_references(related)
    assert not library.looks_like_references(FILLER * 4)


async def test_the_bibliography_is_not_a_page_the_answer_can_cite(db, offline_embed):
    """A reference list is other papers' titles, so it matches a topic's
    keywords better than the paper's own pages do. Found, it handed the writer
    a page on which the claim it cited was somebody else's title."""
    project, _c, _rows = await _library_with(
        db,
        offline_embed,
        {FL: ["Secure aggregation hides each client update from the server. " * 4, REFERENCES]},
    )
    found = await library.Library(db, project["id"]).search(["secure aggregation"], limit=5)
    assert [r.url.rsplit("/", 1)[-1] for r in found] == ["1"]


async def test_a_found_page_carries_its_passages_matched_first(db, offline_embed):
    project, _c, _rows = await _library_with(
        db,
        offline_embed,
        {FL: [FILLER * 6 + " Secure aggregation hides each client update from the server."]},
    )
    [page] = await library.Library(db, project["id"]).search(["secure aggregation"], limit=3)
    assert page.matched >= 1 and len(page.excerpts) > page.matched
    assert "Secure aggregation" in page.excerpts[0]


async def test_a_scan_does_not_hide_its_papers_abstract(db, offline_embed):
    """A PDF without a text layer has no passages. Counting it as the paper's
    full text held the abstract back for pages that do not exist, and the
    paper disappeared from the library without a word."""
    project, c, rows = await _project_list(db, "Privacy Preserving Energy Analytics")
    await fulltext_tool.upload(
        db, c, "scan.pdf", make_pdf(["", ""]), user=DEFAULT_USER, paper_id=rows[0]["id"]
    )
    [result] = await library.Library(db, project["id"]).search(
        ["privacy energy analytics"], limit=5
    )
    assert result.title.endswith("(abstract only)")


def test_the_judge_sees_the_passage_a_claim_cites():
    """The ranked set holds a few passages of a page, not necessarily the one
    the writer quoted from; judged without it, a correct passage citation came
    back unsupported."""
    page = SearchResult(title="P (PDF p. 2)", url="/library/f/2", content="x")
    page.excerpts = ["first chunk about something else", "we scale the dot products by a root"]
    claim = Claim(text="Die Skalierung hilft.", sources=[1])
    claim.passages = [(1, "we scale the dot products")]
    shown = verify._evidence_for([claim], [], {1: page})
    assert [p.text for p in shown] == ["we scale the dot products by a root"]


# ------------------------------------------------------ prefixes and staleness


async def test_passages_are_embedded_as_documents_under_their_papers_title(db, monkeypatch):
    seen = []

    async def recording(texts, *, model, provider="openrouter", usage=None):
        seen.extend(texts)
        return [[1.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(fulltext_tool, "embed", recording)
    monkeypatch.setattr(library, "embed", recording)
    from app.store import settings_store

    saved = await settings_store.load(db, DEFAULT_USER)
    await settings_store.save(
        db,
        saved.model_copy(update={"embedding_model": "nvidia/nemotron-3-embed-1b:free"}),
        DEFAULT_USER,
    )
    project, c, rows = await _project_list(db, FL)
    await fulltext_tool.upload(
        db, c, "a.pdf", paper_pdf(FL), user=DEFAULT_USER, paper_id=rows[0]["id"]
    )
    assert seen and all(text.startswith(f"passage: {FL}\n") for text in seen)
    [listed] = await fulltext_tool.files(db, c["id"], user=DEFAULT_USER)
    assert listed["embedded"] == listed["chunks"]

    seen.clear()
    lib = library.Library(db, project["id"], embedding_model="nvidia/nemotron-3-embed-1b:free")
    await lib.search(["secure aggregation"], limit=3)
    assert "query: secure aggregation" in seen


async def test_vectors_from_before_the_prefixes_count_as_not_indexed(db, offline_embed):
    _p, c, rows = await _project_list(db, FL)
    await fulltext_tool.upload(
        db, c, "a.pdf", paper_pdf(FL), user=DEFAULT_USER, paper_id=rows[0]["id"]
    )
    # What every vector stored before INDEX_VERSION looked like: the bare model.
    await db.execute("UPDATE lit_chunks SET model = 'old'")
    await db.commit()
    [listed] = await fulltext_tool.files(db, c["id"], user=DEFAULT_USER)
    assert listed["embedded"] == 0


async def test_finding_nothing_says_how_close_the_nearest_passage_came(db, offline_embed):
    project, _c, _rows = await _library_with(
        db, offline_embed, {FL: ["Self attention weighs every token. " * 10]}
    )
    lib = library.Library(db, project["id"], embedding_model="test/embed")
    assert await lib.search(["Eiffel tower height in metres"], limit=3) == []
    assert lib.probes and not lib.probes[-1]["passed"]
    assert any("the bar is" in note for note in lib.notes)

    # A second miss in the same run says nothing more, and what was said is
    # handed to the transcript once.
    assert await lib.search(["a pancake recipe with blueberries"], limit=3) == []
    assert sum("the bar is" in note for note in lib.notes) == 1
    assert len(lib.fresh_notes()) == len(lib.notes)
    assert lib.fresh_notes() == []


def test_once_a_run_is_on_topic_its_narrower_queries_need_less(db):
    """One paper asked for its key takeaways: every passage is about the same
    thing, the similarities bunch together, and a fair sub-question stood 0.10
    above the median against a bar of 0.12. Once another query of the run has
    cleared the full bar, the question of whether the run is about these
    documents has been answered, and the narrower query counts. An off-topic
    one still does not."""
    lib = library.Library(db, "p")
    docs = [
        library._Doc(f"passage {i}", {}, vector=[float(i == j) for j in range(5)]) for i in range(5)
    ]
    narrow = [0.6, 0.5, 0.5, 0.5, 0.5]  # gap 0.10
    broad = [0.9, 0.5, 0.5, 0.5, 0.5]  # gap 0.40
    off_topic = [0.56, 0.5, 0.5, 0.5, 0.5]  # gap 0.06

    assert lib._semantic(docs, narrow) == []
    assert lib._semantic(docs, broad)
    assert lib._semantic(docs, narrow) == [0]
    assert lib._semantic(docs, off_topic) == []


# ------------------------------------------------------------------- columns


def _ops_pdf(ops: str) -> bytes:
    stream = ops.encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


LEFT = [f"Left column sentence {i} about secure aggregation here." for i in range(10)]
RIGHT = [f"Right column sentence {i} about smart grid loads." for i in range(10)]


def _two_columns(*, interleaved: bool) -> bytes:
    def line(x, y, text):
        return f"1 0 0 1 {x} {y} Tm ({text}) Tj "

    rows = list(enumerate(zip(LEFT, RIGHT, strict=True)))
    if interleaved:
        body = "".join(
            line(40, 780 - i * 12, left) + line(310, 780 - i * 12, right)
            for i, (left, right) in rows
        )
    else:
        body = "".join(line(40, 780 - i * 12, left) for i, (left, _) in rows)
        body += "".join(line(310, 780 - i * 12, r) for i, (_, r) in rows)
    return _ops_pdf(f"BT /F1 9 Tf {body}ET")


def test_columns_drawn_across_the_page_are_read_one_after_the_other():
    extracted = fulltext.extract(_two_columns(interleaved=True))
    text = extracted.pages[0]
    assert fulltext.COLUMNS in extracted.warnings
    assert text.index(LEFT[-1]) < text.index(RIGHT[0])
    assert f"{LEFT[0]} {RIGHT[0]}" not in " ".join(text.split())


def test_columns_already_read_in_order_are_left_alone():
    extracted = fulltext.extract(_two_columns(interleaved=False))
    assert fulltext.COLUMNS not in extracted.warnings


def test_a_table_is_not_taken_for_two_columns():
    rows = "".join(
        f"1 0 0 1 40 {780 - i * 12} Tm (Model {i}) Tj 1 0 0 1 310 {780 - i * 12} Tm (0.{i}1) Tj "
        for i in range(12)
    )
    prose = "1 0 0 1 40 600 Tm (" + "A sentence of ordinary prose. " * 8 + ") Tj "
    extracted = fulltext.extract(_ops_pdf(f"BT /F1 9 Tf {rows}{prose}ET"))
    assert fulltext.COLUMNS not in extracted.warnings


async def test_vectors_made_the_older_way_still_search_and_are_renewed(db, offline_embed):
    """A vector from this model without the prefixes and the title is worse,
    not useless. Dropping it would leave a large library searched by keyword
    until every passage was embedded again, so it is used, and re-made a slice
    at a time."""
    from app.config import settings
    from app.embed.embedder import vector_tag

    model = settings.embedding_model
    project, _c, _rows = await _library_with(
        db,
        offline_embed,
        {
            FL: [
                "Federated Learning for Smart Grid Forecasting\n" + FILLER * 3,
                "Self attention weighs every token against every other token. " * 8,
            ]
        },
    )
    await db.execute("UPDATE lit_chunks SET model = ?", (model,))
    await db.commit()
    lib = library.Library(db, project["id"], embedding_model=model)
    [found] = await lib.search(["Aufmerksamkeit erklären"], limit=3)
    assert found.url.endswith("/2")
    assert not any("keyword only" in n for n in lib.notes)
    async with db.execute("SELECT DISTINCT model FROM lit_chunks") as cursor:
        assert [row["model"] for row in await cursor.fetchall()] == [vector_tag(model)]


async def test_what_a_run_has_no_room_for_is_embedded_in_the_background(
    db, offline_embed, monkeypatch
):
    """After a model change a large library took one question per 512
    passages to search fully again. The run embeds its slice and hands the
    rest to a task that finishes it, one slice at a time."""
    from app.config import settings
    from app.embed.embedder import vector_tag

    project, _c, _rows = await _library_with(
        db, offline_embed, {FL: ["Self attention weighs every token. " * 30] * 3}
    )
    await db.execute("UPDATE lit_chunks SET vector = NULL, model = NULL")
    await db.commit()
    monkeypatch.setattr(settings, "library_reembed_limit", 1)
    lib = library.Library(db, project["id"], embedding_model="test/embed")
    await lib.search(["attention"], limit=3)
    assert any("in the background" in n for n in lib.notes)
    await library._BACKGROUND[project["id"]]
    assert not library.reindexing(project["id"])
    async with db.execute("SELECT DISTINCT model FROM lit_chunks") as cursor:
        assert [row["model"] for row in await cursor.fetchall()] == [vector_tag("test/embed")]


# ---------------------------------------------------------- the whole library


async def test_the_library_lists_its_papers_and_hands_over_their_abstracts(db, offline_embed):
    project, c, rows = await _project_list(db, FL, "Privacy Preserving Energy Analytics")
    await fulltext_tool.upload(
        db, c, "a.pdf", paper_pdf(FL), user=DEFAULT_USER, paper_id=rows[0]["id"]
    )
    lib = library.Library(db, project["id"])
    catalog = await lib.catalog()
    assert FL in catalog and "Privacy Preserving Energy Analytics" in catalog
    overview = await lib.overview()
    assert {r.title for r in overview} == {
        f"{FL} (abstract)",
        "Privacy Preserving Energy Analytics (abstract)",
    }
    assert all(r.content.startswith("Abstract of") for r in overview)
    # A click on a paper opens its PDF, never the publisher's page, when there
    # is a PDF: the uploaded copy first, else none to open but the link.
    urls = {r.title: r.url for r in overview}
    assert urls[f"{FL} (abstract)"].startswith("/library/")
    assert urls["Privacy Preserving Energy Analytics (abstract)"].startswith("https://doi.org/")


def test_a_paper_opens_at_its_pdf_before_its_landing_page():
    paper = {"link": "https://doi.org/10.1/x", "pdf": "https://arxiv.org/pdf/1.pdf"}
    assert library.opens_at(paper) == "https://arxiv.org/pdf/1.pdf"
    assert library.opens_at({**paper, "file_id": "f"}) == "/library/f/1"
    assert library.opens_at({"link": "https://doi.org/10.1/x"}) == "https://doi.org/10.1/x"


async def test_a_request_about_the_whole_library_is_written_from_every_abstract(
    redis, db, offline_embed, no_web, monkeypatch
):
    """ "Write the introduction" matches no passage, and the semantic floor
    rightly finds nothing for it. With the abstracts of everything collected
    the writer has what such a request needs, and the queries were written
    knowing what the library holds."""
    project, _c, _rows = await _secure_aggregation_library(db, offline_embed)
    llm = FakeLLM(
        answer="Die Arbeiten behandeln föderiertes Lernen [1].",
        classification={
            "standalone_question": "Schreib eine Einleitung",
            "search_queries": ["volcano eruption"],
            "whole_library": True,
        },
    )
    prompts_seen = []
    original = orchestrator.prompts.classify

    def classify(*args, **kwargs):
        prompts_seen.append(kwargs.get("catalog", ""))
        return original(*args, **kwargs)

    monkeypatch.setattr(orchestrator.prompts, "classify", classify)
    monkeypatch.setattr(orchestrator, "LLM", lambda *a, **k: llm)
    stream = RunStream(redis, "lib-whole", ttl=60)
    await orchestrator.run(stream, library_request(db, project["id"], web=False))

    _events, _blocks, done = await collect(stream)
    assert done["status"] == "completed"
    assert FL in prompts_seen[0]
    assert "stream" in llm.calls, "nothing was written"
    sources = next(b for b in await stream.snapshot() if b["type"] == "sources")
    assert any(s["title"].endswith("(abstract)") for s in sources["data"]["sources"])


async def test_earlier_chats_are_searched_only_when_asked(redis, db, monkeypatch):
    from app import runs
    from app.store import chats

    seen = {}

    async def fake_run(stream, req):
        seen["request"] = req
        await stream.emit_done("completed")

    monkeypatch.setattr(runs, "orchestrate", fake_run)
    project = await projects.create(db, "Errands", user=DEFAULT_USER)
    await chats.ensure_chat(db, "old", "secure aggregation", project["id"], user=DEFAULT_USER)
    await chats.start_message(
        db, chat_id="old", run_id="r-old", query="secure aggregation", depth="quick", model="m"
    )
    await chats.finish_message(
        db, "r-old", answer="Secure aggregation hides updates [1].", blocks=[], status="completed"
    )

    await runs.start(redis=redis, db=db, query="secure aggregation", project_id=project["id"])
    await asyncio.sleep(0.02)
    assert seen["request"].project_sources == []

    await runs.start(
        redis=redis,
        db=db,
        query="secure aggregation",
        project_id=project["id"],
        earlier_chats=True,
    )
    await asyncio.sleep(0.02)
    [earlier] = seen["request"].project_sources
    assert "[1]" not in earlier.content


def test_a_formula_s_split_characters_are_mended_or_dropped():
    """pypdf returned an italic x (U+1D465) as two surrogate halves, and a lone half failed the
    upload with "surrogates not allowed" as soon as it was written anywhere."""
    pair = "\ud835\udc65"  # MATHEMATICAL ITALIC SMALL X, as UTF-16 halves
    cleaned = fulltext.clean(f"Let {pair} be \ud835 the input")
    assert cleaned == "Let \U0001d465 be the input"
    cleaned.encode("utf-8")  # what SQLite and JSON do with it
    assert fulltext.unsurrogate("plain text") == "plain text"
