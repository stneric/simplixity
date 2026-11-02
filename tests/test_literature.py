"""The literature page: parsing five databases, merging one paper's copies,
BibTeX worth citing from, and rounds that never show a paper twice and learn
from what was screened. No network: every database, the model and the
embedding call are replaced."""

from __future__ import annotations

from typing import ClassVar

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.scholar import bibtex, rank, sources
from app.scholar.paper import (
    CONFERENCE,
    JOURNAL,
    PREPRINT,
    Paper,
    clean_text,
    merge,
    normalise_arxiv,
    normalise_doi,
)
from app.search import project as project_search
from app.store import literature as store
from app.store import projects
from app.store.db import DEFAULT_USER
from app.tools import literature

# ------------------------------------------------------------------ parsing


def test_openalex_rebuilds_the_abstract_and_normalises_the_doi():
    paper = sources.parse_openalex(
        {
            "id": "https://openalex.org/W123",
            "doi": "https://doi.org/10.1109/TSG.2020.ABC",
            "display_name": "Federated Learning for <i>Smart</i> Grids",
            "publication_year": 2020,
            "type_crossref": "journal-article",
            "authorships": [{"author": {"display_name": "Ada Lovelace"}}],
            "primary_location": {
                "source": {"display_name": "IEEE Trans. Smart Grid", "type": "journal"},
                "landing_page_url": "https://ieeexplore.ieee.org/document/1",
            },
            "cited_by_count": 42,
            "abstract_inverted_index": {"Grids": [2], "learn": [1], "Smart": [0]},
            "referenced_works": ["https://openalex.org/W9"],
            "biblio": {"volume": "11", "issue": "3", "first_page": "10", "last_page": "20"},
        }
    )
    assert paper.title == "Federated Learning for Smart Grids"
    assert paper.abstract == "Smart learn Grids"
    assert paper.doi == "10.1109/tsg.2020.abc"
    assert paper.kind == JOURNAL
    assert paper.ids["openalex"] == "W123"
    assert paper.references == ["W9"]
    assert paper.pages == "10–20"
    assert paper.link == "https://doi.org/10.1109/tsg.2020.abc"


def test_crossref_strips_jats_and_the_abstract_heading():
    paper = sources.parse_crossref(
        {
            "DOI": "10.1000/X",
            "title": ["A &amp;amp; B"],
            "author": [{"given": "Grace", "family": "Hopper"}, {"name": "ACM Committee"}],
            "issued": {"date-parts": [[2019, 5]]},
            "container-title": ["Proc. Something"],
            "type": "proceedings-article",
            "abstract": "<jats:title>Abstract</jats:title><jats:p>We study things.</jats:p>",
        }
    )
    assert paper.title == "A & B"
    assert paper.abstract == "We study things."
    assert paper.authors == ["Grace Hopper", "ACM Committee"]
    assert paper.kind == CONFERENCE
    assert paper.year == 2019


def test_semantic_scholar_keeps_the_ids_recommendations_need():
    paper = sources.parse_semantic(
        {
            "paperId": "abc123",
            "externalIds": {"ArXiv": "2101.00001", "CorpusId": 1},
            "title": "A preprint",
            "abstract": None,
            "year": 2021,
            "authors": [{"name": "Alan Turing"}],
            "tldr": {"text": "It does a thing."},
        }
    )
    assert paper.ids["s2"] == "abc123"
    assert paper.arxiv == "2101.00001"
    assert paper.kind == PREPRINT
    assert paper.abstract == ""
    assert paper.tldr == "It does a thing."


def test_semantic_scholar_tolerates_a_venue_given_as_a_string():
    # The recommendations endpoint does this; it crashed the first live round.
    paper = sources.parse_semantic(
        {"paperId": "x", "title": "T", "publicationVenue": "NeurIPS", "journal": "J", "tldr": None}
    )
    assert paper.title == "T" and paper.kind == "other"


ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2101.00001v3</id>
    <published>2021-01-01T00:00:00Z</published>
    <title>A
      Preprint</title>
    <summary>  It does a thing.  </summary>
    <author><name>Alan Turing</name></author>
    <link title="pdf" href="http://arxiv.org/pdf/2101.00001v3"/>
  </entry>
</feed>"""


def test_arxiv_atom_drops_the_version_and_folds_whitespace():
    [paper] = sources.parse_arxiv(ATOM)
    assert paper.arxiv == "2101.00001"
    assert paper.title == "A Preprint"
    assert paper.abstract == "It does a thing."
    assert paper.kind == PREPRINT
    assert paper.pdf.endswith("2101.00001v3")


def test_arxiv_query_requires_every_word():
    assert sources.arxiv_query("federated learning, smart-grid") == (
        "all:federated AND all:learning AND all:smart-grid"
    )


def test_ieee_parses_authors_kind_and_article_number():
    paper = sources.parse_ieee(
        {
            "title": "Edge AI",
            "abstract": "Abstract: <p>Fast.</p>",
            "authors": {"authors": [{"full_name": "Linus Torvalds"}]},
            "publication_year": "2022",
            "publication_title": "IEEE IoT Journal",
            "content_type": "Journals",
            "doi": "10.1109/JIOT.2022.1",
            "article_number": "9999",
            "start_page": "1",
            "end_page": "9",
        }
    )
    assert paper.abstract == "Fast."
    assert paper.kind == JOURNAL
    assert paper.ids["ieee"] == "9999"
    assert paper.publisher == "IEEE"
    assert paper.pages == "1–9"


def test_identifiers_normalise():
    assert normalise_doi("doi: 10.1000/ABC.") == "10.1000/abc"
    assert normalise_doi("no doi here") == ""
    assert normalise_arxiv("https://arxiv.org/pdf/2101.00001v2.pdf") == "2101.00001"
    assert normalise_arxiv("arXiv:hep-th/9901001") == "hep-th/9901001"
    assert clean_text("x" * 50, limit=20).endswith("…")


# ------------------------------------------------------------------- merging


def test_a_preprint_and_its_published_version_are_one_paper():
    preprint = Paper(
        title="Attention Is All You Need: A Very Long Title",
        arxiv="1706.03762",
        abstract="short",
        kind=PREPRINT,
        sources=["arxiv"],
    )
    published = Paper(
        title="Attention is all you need: a very long title",
        doi="10.5555/3295222",
        abstract="the longer abstract",
        venue="NeurIPS",
        kind=CONFERENCE,
        citations=100,
        sources=["crossref"],
    )
    [paper] = merge([preprint, published])
    assert paper.doi == "10.5555/3295222"
    assert paper.arxiv == "1706.03762"
    assert paper.abstract == "the longer abstract"
    assert paper.kind == CONFERENCE
    assert paper.sources == ["arxiv", "crossref"]
    assert paper.key == "doi:10.5555/3295222"


def test_short_generic_titles_are_never_merged():
    papers = merge([Paper(title="Introduction"), Paper(title="Introduction")])
    assert len(papers) == 2


# ------------------------------------------------------------------- bibtex


def _paper(**kw):
    base = {
        "title": "Attention Is All You Need",
        "authors": ["Ashish Vaswani", "Noam Shazeer"],
        "year": 2017,
        "venue": "Advances in NeurIPS",
        "kind": CONFERENCE,
    }
    return Paper(**{**base, **kw})


def test_keys_are_surname_year_word_and_unique():
    papers = [_paper(), _paper(), _paper(authors=["José van der Berg"], title="The Ünicode Test")]
    assert bibtex.assign_keys(papers) == [
        "vaswani2017attention",
        "vaswani2017attentionb",
        "vanderberg2017unicode",
    ]


def test_built_entry_protects_acronyms_and_escapes():
    entry = bibtex.build(_paper(title="IoT & 5G Security for LLMs", doi="10.1/x", pages="1–9"), "k")
    assert entry.startswith("@inproceedings{k,")
    assert "title = {{IoT} \\& {5G} Security for {LLMs}}" in entry
    assert "author = {Vaswani, Ashish and Shazeer, Noam}" in entry
    assert "booktitle = {Advances in NeurIPS}" in entry
    assert "pages = {1--9}" in entry
    assert "doi = {10.1/x}" in entry


def test_a_fetched_entry_gets_our_key_and_one_field_per_line():
    raw = "@article{Vaswani_2017, title={A, B}, author={X and Y}, year={2017}}"
    tidy = bibtex._tidy(raw)
    assert tidy.splitlines() == [
        "@article{Vaswani_2017,",
        "  title={A, B},",
        "  author={X and Y},",
        "  year={2017}",
        "}",
    ]
    assert bibtex.rekey(tidy, "vaswani2017a").startswith("@article{vaswani2017a,")


def test_a_registry_entry_missing_title_and_author_is_completed():
    entry = "@article{X_2025,\n  journal={J},\n  year={2025},\n  pages={74–76}\n}"
    paper = _paper(kind=JOURNAL, doi="10.1/x")
    done = bibtex.complete(entry, paper)
    assert bibtex.field(done, "title") == "Attention Is All You Need"
    assert bibtex.field(done, "author") == "Vaswani, Ashish and Shazeer, Noam"
    assert bibtex.field(done, "pages") == "74--76"
    assert done.count("year") == 1
    # The .bib is also the download list, so every entry says where to go.
    assert bibtex.field(done, "url") == f"https://doi.org/{paper.doi}"


def test_a_registry_author_names_a_paper_the_databases_left_anonymous():
    paper = Paper(title="Federated things", year=None)
    bibtex.fill_from(paper, "@article{a,\n  author={Taïk, Afaf and B, C},\n  year={2020}\n}")
    assert bibtex.base_key(paper) == "taik2020federated"


# --------------------------------------------------------------------- rank


async def test_rocchio_scores(monkeypatch):
    vectors = {"brief": [1.0, 0.0], "pick": [0.0, 1.0], "x": [1.0, 0.0], "y": [0.0, 1.0]}

    async def fake_embed(texts, **_):
        return [vectors[t.split(".")[0]] for t in texts]

    monkeypatch.setattr(rank, "embed", fake_embed)
    candidates = [Paper(title="x"), Paper(title="y")]
    plain = await rank.semantic("brief", candidates, [], [], model="m", provider="p")
    assert plain[0] > plain[1]
    # BETA = 0.75 < ALPHA = 1, so one pick does not flip it alone; two signals do:
    # the pick, and the excluded paper that sits on the brief's own axis.
    steered = await rank.semantic(
        "brief", candidates, [Paper(title="pick")], [Paper(title="x")], model="m", provider="p"
    )
    assert steered[1] - steered[0] > plain[1] - plain[0]


def test_combine_rewards_agreement_and_the_graph():
    a = Paper(title="a", sources=["openalex"], citations=10)
    b = Paper(title="b", sources=["openalex", "crossref", "ieee"], citations=10, links=3)
    rank.combine([a, b], [0.5, 0.5])
    assert b.score > a.score


def test_every_strategy_gets_a_place_on_the_page():
    graph = [Paper(title=f"g{i}", origin="citations", score=0.9 - i / 100) for i in range(20)]
    recs = [Paper(title=f"r{i}", origin="recommended", score=0.5) for i in range(10)]
    chosen = literature.choose(graph + recs, 10)
    assert len(chosen) == 10
    assert sum(p.origin == "recommended" for p in chosen) == literature.QUOTA
    assert chosen == sorted(chosen, key=lambda p: p.score, reverse=True)
    # Fewer candidates than places: all of them.
    assert len(literature.choose(recs[:3], 10)) == 3


# -------------------------------------------------------------------- store


async def test_collections_are_scoped_to_a_profile(db):
    mine = await store.create(db, "Mine", user="eric")
    assert await store.get(db, mine["id"], user="anna") is None
    assert [c["id"] for c in await store.list_collections(db, user="anna")] == []
    assert not await store.delete(db, mine["id"], user="anna")
    assert await store.delete(db, mine["id"], user="eric")


async def test_a_paper_is_stored_once_and_decisions_stick(db):
    c = await store.create(db, "L", user=DEFAULT_USER)
    row = {"key": "doi:10.1/a", "data": {"title": "A"}, "score": 0.5}
    assert await store.add_papers(db, c["id"], [row], round_no=1) == 1
    assert await store.add_papers(db, c["id"], [row], round_no=2) == 0
    [paper] = await store.list_papers(db, c["id"])
    assert await store.decide(db, c["id"], paper["id"], decision="excluded", note="wrong domain")
    [paper] = await store.list_papers(db, c["id"], decisions=("excluded",))
    assert paper["note"] == "wrong domain" and paper["decided_at"]
    with pytest.raises(ValueError):
        await store.decide(db, c["id"], paper["id"], decision="bogus")


async def test_deleting_a_project_unfiles_its_reading_lists(db):
    p = await projects.create(db, "Thesis", user=DEFAULT_USER)
    c = await store.create(db, "L", project_id=p["id"], user=DEFAULT_USER)
    await projects.delete(db, p["id"], user=DEFAULT_USER)
    assert (await store.get(db, c["id"]))["project_id"] is None


# ---------------------------------------------------------------------- api


class FakeLLM:
    """Answers each schema with whatever the test queued for it."""

    replies: ClassVar[dict] = {}
    calls: ClassVar[list] = []
    models: ClassVar[list] = []

    def __init__(self, model, provider):
        self.model = model
        FakeLLM.models.append((provider, model))

    async def structured(self, messages, schema, **_):
        FakeLLM.calls.append((schema.__name__, messages))
        reply = FakeLLM.replies[schema.__name__]
        return reply(messages) if callable(reply) else reply


def _found(title, doi, *, source="openalex", abstract="About smart grids and federated learning"):
    return Paper(title=title, doi=doi, abstract=abstract, year=2022, sources=[source])


@pytest.fixture
def offline(monkeypatch):
    """Every outbound call the feature makes, replaced."""
    FakeLLM.replies = {}
    FakeLLM.calls = []
    FakeLLM.models = []
    monkeypatch.setattr(literature, "LLM", FakeLLM)
    state = {"search": {}, "recommend": [], "graph": [], "citing": [], "searched": []}

    async def search(name, query, *, limit, years):
        state["searched"].append((name, query))
        return [Paper(**{**vars_of(p), "sources": [name]}) for p in state["search"].get(query, [])]

    def vars_of(p):
        return {f: getattr(p, f) for f in p.__dataclass_fields__}

    async def semantic_ids(papers):
        return {p.key: f"s2-{p.key}" for p in papers}

    async def recommend(positive, negative, *, limit):
        state["recommend_args"] = (positive, negative)
        return list(state["recommend"])

    async def by_ids(ids):
        return list(state["graph"])

    async def by_dois(dois):
        return [
            Paper(title="seed", doi=d, ids={"openalex": f"W{i}"}, references=["W99"])
            for i, d in enumerate(dois)
        ]

    async def citing(ids, *, limit, years):
        return list(state["citing"])

    async def no_embed(*_a, **_k):
        return None

    async def no_bib(doi):
        return None

    monkeypatch.setattr(sources, "available", lambda: ["openalex", "crossref"])
    monkeypatch.setattr(sources, "search", search)
    monkeypatch.setattr(sources, "semantic_ids", semantic_ids)
    monkeypatch.setattr(sources, "recommend", recommend)
    monkeypatch.setattr(sources, "openalex_by_ids", by_ids)
    monkeypatch.setattr(sources, "openalex_by_dois", by_dois)
    monkeypatch.setattr(sources, "openalex_citing", citing)
    monkeypatch.setattr(rank, "embed", no_embed)
    monkeypatch.setattr(bibtex, "fetch", no_bib)
    return state


@pytest.fixture
async def client(redis, db):
    app.state.redis = redis
    app.state.db = db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        signed_in = await http.post("/api/profiles", json={"name": DEFAULT_USER})
        assert signed_in.status_code == 200, signed_in.text
        yield http


def _brief(**kw):
    return literature.Brief(
        **{
            "question": "Federated learning for smart grids",
            "queries": ["federated learning smart grid"],
            "language": "de",
            **kw,
        }
    )


async def test_talking_fills_the_brief_and_names_the_list(client, offline):
    FakeLLM.replies["TalkReply"] = literature.TalkReply(
        reply="Wofür brauchst du die Quellen?",
        brief=_brief(include=["peer-reviewed", "peer-reviewed", " "]),
        ready=False,
        title="FL in Smart Grids",
    )
    c = (await client.post("/api/literature", json={})).json()
    body = (await client.post(f"/api/literature/{c['id']}/talk", json={"message": "Hallo"})).json()
    assert body["name"] == "FL in Smart Grids"
    assert body["brief"]["include"] == ["peer-reviewed"]  # tidied
    assert [t["role"] for t in body["talk"]] == ["user", "assistant"]


def test_language_is_read_from_the_message_not_guessed():
    english = "I'm writing my masters thesis and need papers that help me chose a model"
    assert literature.detect_language(english) == "en"
    assert literature.detect_language("Ich brauche Quellen für das Kapitel über die Netze") == "de"
    assert literature.detect_language("ok") == ""
    assert literature.language_name("de") == "German"


async def test_an_english_message_gets_an_english_brief_whatever_the_model_says(client, offline):
    # The live failure: a small model answered English in French and wrote `fr`.
    FakeLLM.replies["TalkReply"] = literature.TalkReply(
        reply="Pourriez-vous préciser…", brief=_brief(language="fr"), ready=False, title="T"
    )
    c = (await client.post("/api/literature", json={})).json()
    message = "I need papers that help me choose a model to analyze logs from Kafka and MQTT"
    body = (await client.post(f"/api/literature/{c['id']}/talk", json={"message": message})).json()
    assert body["brief"]["language"] == "en"
    system = FakeLLM.calls[-1][1][0]["content"]
    assert "in English, short" in system


async def test_the_literature_model_is_used_when_set(client, db, offline):
    from app.store import settings_store

    saved = await settings_store.load(db, DEFAULT_USER)
    saved.chat_provider, saved.chat_model = "ollama", "tiny:4b"
    saved.literature_provider, saved.literature_model = "openrouter", "big/model"
    await settings_store.save(db, saved, DEFAULT_USER)
    FakeLLM.replies["TalkReply"] = literature.TalkReply(
        reply="ok", brief=_brief(), ready=False, title="T"
    )
    c = (await client.post("/api/literature", json={})).json()
    await client.post(f"/api/literature/{c['id']}/talk", json={"message": "hello there"})
    assert FakeLLM.models[-1] == ("openrouter", "big/model")

    saved.literature_model = ""
    await settings_store.save(db, saved, DEFAULT_USER)
    await client.post(f"/api/literature/{c['id']}/talk", json={"message": "hello again"})
    assert FakeLLM.models[-1] == ("ollama", "tiny:4b")


async def test_the_literature_provider_is_validated(client):
    current = (await client.get("/api/settings")).json()
    bad = await client.put("/api/settings", json={**current, "literature_provider": "nope"})
    assert bad.status_code == 422


async def test_a_failed_talk_turn_stores_nothing(client, offline):
    from app.llm.client import LLMError

    def fail(_):
        raise LLMError("model down")

    FakeLLM.replies["TalkReply"] = fail
    c = (await client.post("/api/literature", json={})).json()
    response = await client.post(f"/api/literature/{c['id']}/talk", json={"message": "Hallo"})
    assert response.status_code == 502
    stored = (await client.get(f"/api/literature/{c['id']}")).json()
    assert stored["collection"]["talk"] == []


async def _collection_with_brief(client, db):
    c = (await client.post("/api/literature", json={"name": "L"})).json()
    await store.update(db, c["id"], brief=_brief().model_dump())
    return c


async def test_a_round_merges_databases_and_reports_each(client, db, offline):
    offline["search"]["federated learning smart grid"] = [
        _found("Federated learning for smart grid forecasting", "10.1/a"),
        _found("Privacy in federated smart grid analytics", "10.1/b"),
    ]
    c = await _collection_with_brief(client, db)
    body = (await client.post(f"/api/literature/{c['id']}/rounds")).json()
    report = body["report"]
    assert report["round"] == 1 and report["added"] == 2
    assert {s["id"]: s["found"] for s in report["sources"]} == {"openalex": 2, "crossref": 2}
    assert report["ranking"] == "lexical"
    # The embedding model "did not answer", and the round says so.
    assert any("embedding" in note for note in report["notes"])
    # Two databases, one paper each: merged, with both named.
    assert all(sorted(p["data"]["sources"]) == ["crossref", "openalex"] for p in body["papers"])


async def test_the_next_round_never_shows_a_paper_again_and_learns(client, db, offline):
    first = [
        _found("Federated learning for smart grid forecasting", "10.1/a"),
        _found("Federated smart grid load disaggregation study", "10.1/b"),
    ]
    offline["search"]["federated learning smart grid"] = first
    c = await _collection_with_brief(client, db)
    papers = (await client.post(f"/api/literature/{c['id']}/rounds")).json()["papers"]
    relevant, excluded = papers[0], papers[1]
    await client.patch(
        f"/api/literature/{c['id']}/papers/{relevant['id']}", json={"decision": "relevant"}
    )
    await client.patch(
        f"/api/literature/{c['id']}/papers/{excluded['id']}",
        json={"decision": "excluded", "note": "not about privacy"},
    )

    FakeLLM.replies["_Queries"] = literature._Queries(
        queries=["federated learning smart grid", "privacy preserving smart meter learning"],
        rationale="Narrowed to privacy.",
    )
    offline["search"]["privacy preserving smart meter learning"] = [
        *first,  # already shown: must not come back
        _found("Privacy preserving smart meter learning", "10.1/c"),
    ]
    offline["recommend"] = [_found("Recommended paper on federated smart grids", "10.1/d")]
    offline["graph"] = [_found("Classic federated averaging for smart grid", "10.1/e")]

    body = (await client.post(f"/api/literature/{c['id']}/rounds")).json()
    report = body["report"]
    # A query that already ran is not run again.
    assert report["queries"] == ["privacy preserving smart meter learning"]
    assert report["rationale"] == "Narrowed to privacy."
    new = [p for p in body["papers"] if p["round"] == 2]
    assert {p["data"]["doi"] for p in new} == {"10.1/c", "10.1/d", "10.1/e"}
    origins = {p["data"]["doi"]: p["origin"] for p in new}
    assert origins["10.1/d"] == "recommended" and origins["10.1/e"] == "references"
    # Recommendations were asked with the pick as positive, the reject as negative.
    positive, negative = offline["recommend_args"]
    assert positive == [f"s2-doi:{relevant['data']['doi']}"]
    assert negative == [f"s2-doi:{excluded['data']['doi']}"]
    # The exclusion reason reached the query planner.
    planned = FakeLLM.calls[-1][1][-1]["content"]
    assert "not about privacy" in planned


async def test_a_round_without_a_brief_is_refused(client, offline):
    c = (await client.post("/api/literature", json={})).json()
    response = await client.post(f"/api/literature/{c['id']}/rounds")
    assert response.status_code == 422


async def test_screening_never_summarises_a_paper_without_an_abstract(client, db, offline):
    c = await _collection_with_brief(client, db)
    await store.add_papers(
        db,
        c["id"],
        [
            {"key": "doi:10.1/a", "data": {"title": "Has abstract", "abstract": "We did X."}},
            {"key": "doi:10.1/b", "data": {"title": "Title only"}},
        ],
        round_no=1,
    )
    rows = await store.list_papers(db, c["id"])
    ids = [r["id"] for r in rows]

    def reply(_):
        return literature._Screening(
            items=[
                literature._Screen(
                    id=ids[0], summary="It did X.", fit="Meets A.", verdict="relevant"
                ),
                literature._Screen(
                    id=ids[1], summary="Invented.", fit="Title fits.", verdict="relevant"
                ),
            ]
        )

    FakeLLM.replies["_Screening"] = reply
    body = (await client.post(f"/api/literature/{c['id']}/screen", json={"paper_ids": ids})).json()
    with_abstract, title_only = body["summaries"][str(ids[0])], body["summaries"][str(ids[1])]
    assert with_abstract["summary"] == "It did X." and with_abstract["basis"] == "abstract"
    assert title_only["summary"] == "" and title_only["basis"] == "title"
    assert title_only["verdict"] == "maybe"
    # Stored: asking again costs no call.
    before = len(FakeLLM.calls)
    await client.post(f"/api/literature/{c['id']}/screen", json={"paper_ids": ids})
    assert len(FakeLLM.calls) == before


async def test_export_writes_only_the_chosen_decisions(client, db, offline):
    c = await _collection_with_brief(client, db)
    await store.add_papers(
        db,
        c["id"],
        [
            {
                "key": "doi:10.1/a",
                "data": _found("Kept paper", "10.1/a").as_dict() | {"authors": ["A B"]},
            },
            {
                "key": "doi:10.1/b",
                "data": {"title": "Maybe paper", "authors": ["C D"], "year": 2020},
            },
            {"key": "doi:10.1/c", "data": {"title": "Excluded paper"}},
        ],
        round_no=1,
    )
    rows = await store.list_papers(db, c["id"])
    for row, decision in zip(rows, ["relevant", "maybe", "excluded"], strict=True):
        await store.decide(db, c["id"], row["id"], decision=decision)

    only = await client.get(f"/api/literature/{c['id']}/export.bib")
    assert only.status_code == 200
    assert "Kept paper" in only.text and "Maybe paper" not in only.text
    assert only.headers["x-bib-built"] == "1"
    assert 'filename="L.bib"' in only.headers["content-disposition"]

    both = await client.get(f"/api/literature/{c['id']}/export.bib?include=relevant,maybe")
    assert "Maybe paper" in both.text and "Excluded paper" not in both.text


async def test_another_profile_cannot_see_a_list(client, db):
    other = await store.create(db, "Theirs", user="someone-else")
    assert (await client.get(f"/api/literature/{other['id']}")).status_code == 404
    assert (await client.post(f"/api/literature/{other['id']}/rounds")).status_code == 404


async def test_filing_under_someone_elses_project_is_refused(client, db):
    theirs = await projects.create(db, "Theirs", user="someone-else")
    response = await client.post("/api/literature", json={"project_id": theirs["id"]})
    assert response.status_code == 404


async def test_relevant_papers_become_project_sources(db):
    p = await projects.create(db, "Thesis", user=DEFAULT_USER)
    c = await store.create(db, "L", project_id=p["id"], user=DEFAULT_USER)
    paper = _found("Federated learning for smart grid forecasting", "10.1/a")
    await store.add_papers(db, c["id"], [{"key": paper.key, "data": paper.as_dict()}], round_no=1)
    [row] = await store.list_papers(db, c["id"])

    assert await project_search.search(db, p["id"], "federated learning smart grid") == []
    await store.decide(db, c["id"], row["id"], decision="relevant")
    [result] = await project_search.search(db, p["id"], "federated learning smart grid")
    assert result.url == "https://doi.org/10.1/a"
    assert result.tier == "academic"
    assert result.content.startswith("About smart grids")
