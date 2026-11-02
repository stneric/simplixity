"""Source quality: titles, provenance, corroboration and ordering.

These are the properties that decide what the model reads and what the reader
sees next to the answer. A regression here does not crash anything; it quietly
sources an answer to a content farm, so each rule is pinned individually.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.search import lenses as lens_module
from app.search import quality, searxng
from app.search.base import (
    KIND_PAPER,
    KIND_PDF,
    MAX_PER_DOMAIN,
    SearchResult,
    classify_kind,
    clean_title,
    dedupe,
    keep_relevant,
    order,
)
from app.search.searxng import (
    SearchReport,
    _parse,
    _request_groups,
    _results_of,
    _unresponsive,
)


def result(url: str, **overrides) -> SearchResult:
    fields = {"title": "A title", "url": url, "snippet": "snippet", "engines": ["engine a"]}
    made = SearchResult(**{**fields, **overrides})
    made.assess()
    return made


# ------------------------------------------------------------------- titles


def test_engine_pdf_markers_are_not_part_of_the_title():
    assert clean_title("PDF EU AI Act", domain="fpf.org")[0] == "EU AI Act"
    assert clean_title("[PDF] Annual report")[0] == "Annual report"
    assert clean_title("PDF EU AI Act", domain="fpf.org")[1] is True
    assert clean_title("Ordinary title")[1] is False


def test_a_site_brand_that_repeats_the_domain_is_dropped():
    title, _ = clean_title("EU AI Act — the full text | fpf.org", domain="fpf.org")
    assert title == "EU AI Act — the full text"


def test_a_brand_that_only_describes_the_publisher_is_kept():
    """`European Union` is information about the source; `europa.eu` is already
    printed next to it. Only the redundant half goes."""
    title, _ = clean_title(
        "AI Act | Shaping Europe's digital future - European Union",
        domain="digital-strategy.ec.europa.eu",
    )
    assert title == "AI Act | Shaping Europe's digital future - European Union"


def test_a_brand_is_never_stripped_down_to_nothing():
    title, _ = clean_title("Reuters | reuters.com", domain="reuters.com")
    assert title.startswith("Reuters")


def test_an_engines_own_truncation_loses_the_dangling_fragment():
    title, _ = clean_title("EU AI Act Timeline: New 2026 to 20...")
    assert title == "EU AI Act Timeline: New 2026 to 20"
    assert clean_title("Up-to-date developments and ...")[0] == "Up-to-date developments"


def test_html_entities_and_stray_whitespace_are_normalised():
    title, _ = clean_title("  Rights  &amp;\n obligations   ")
    assert title == "Rights & obligations"


def test_a_very_long_title_is_cut_on_a_word_boundary():
    title, _ = clean_title("word " * 60)
    assert len(title) <= 121 and title.endswith("…") and "  " not in title


def test_kind_follows_the_engine_metadata_not_the_url_alone():
    assert (
        classify_kind(url="https://x.test/a", category="science", template="", is_pdf=False, doi="")
        == KIND_PAPER
    )
    assert (
        classify_kind(
            url="https://x.test/a", category="", template="paper.html", is_pdf=False, doi=""
        )
        == KIND_PAPER
    )
    assert (
        classify_kind(url="https://x.test/a.pdf", category="", template="", is_pdf=False, doi="")
        == KIND_PDF
    )


# --------------------------------------------------------------- provenance


def test_official_publishers_outrank_ordinary_pages():
    official = quality.assess("digital-strategy.ec.europa.eu")
    farm = quality.assess("euaiactchecklist.com")
    assert official.tier == quality.PRIMARY
    assert farm.tier == quality.WEB
    assert official.trust > farm.trust


def test_a_subdomain_inherits_its_parent_publisher():
    assert quality.assess("eur-lex.europa.eu").tier == quality.PRIMARY
    assert quality.assess("pmc.ncbi.nlm.nih.gov").tier == quality.PRIMARY  # nih.gov
    assert quality.assess("blog.arxiv.org").tier == quality.ACADEMIC


def test_a_doi_makes_something_a_paper_wherever_it_is_hosted():
    verdict = quality.assess("some-lab.example.org", doi="10.1000/xyz")
    assert verdict.tier == quality.ACADEMIC


def test_vendor_documentation_counts_as_technical():
    for host in ("docs.python.org", "developer.mozilla.org", "somelib.readthedocs.io"):
        assert quality.assess(host).tier == quality.TECHNICAL, host


def test_seo_shaped_titles_are_demoted_but_not_excluded():
    plain = quality.assess("example.test", title="Changes to the AI Act in 2026")
    listicle = quality.assess("example.test", title="Top 10 Things You Need To Know")
    assert listicle.trust < plain.trust
    assert listicle.trust > 0


def test_a_keyword_stuffed_domain_is_demoted():
    assert quality.assess("eu-ai-act-checklist.com").trust < quality.assess("acme.com").trust


def test_aggregators_rank_below_ordinary_pages():
    assert quality.assess("pinterest.com").trust < quality.assess("someblog.test").trust


# -------------------------------------------------------------- corroboration


def test_dedupe_merges_the_engines_that_found_the_same_page():
    """Three engines agreeing is evidence,
    and it is the cheapest evidence available."""
    kept = dedupe(
        [
            result("https://a.test/x", engines=["bing"], score=0.4),
            result("https://www.a.test/x/", engines=["duckduckgo"], score=0.9),
            result("https://a.test/x", engines=["mojeek"], score=0.2),
        ]
    )
    assert len(kept) == 1
    assert sorted(kept[0].engines) == ["bing", "duckduckgo", "mojeek"]
    assert kept[0].score == 0.9
    assert kept[0].hits == 3


def test_corroborated_results_outrank_lone_ones_at_equal_provenance():
    alone = result("https://a.test/x", engines=["bing"], score=0.9)
    agreed = result("https://b.test/y", engines=["bing", "duckduckgo", "mojeek"], score=0.5)
    assert agreed.score_rank() > alone.score_rank()


def test_provenance_still_beats_agreement():
    """Three engines agreeing on a blog does not make it the statute."""
    official = result("https://eur-lex.europa.eu/x", engines=["bing"], score=0.4)
    popular = result("https://blog.test/y", engines=["bing", "duckduckgo", "mojeek"], score=1.0)
    assert official.score_rank() > popular.score_rank()


def test_recency_only_counts_when_the_question_is_time_sensitive():
    recent = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    stale = (datetime.now(UTC) - timedelta(days=2000)).isoformat()
    fresh = result("https://a.test/x", published=recent)
    old = result("https://b.test/y", published=stale)

    assert fresh.score_rank(prefer_recent=True) > old.score_rank(prefer_recent=True)
    assert fresh.score_rank() == old.score_rank()  # undated and old rank alike otherwise


def test_an_undated_page_ranks_between_recent_and_ancient():
    now = datetime.now(UTC)
    assert (
        quality.freshness((now - timedelta(days=3000)).isoformat())
        < quality.freshness("")
        < quality.freshness(now.isoformat())
    )


def test_freshness_survives_an_unparseable_date():
    assert quality.freshness("not a date") == quality.freshness("")


# ------------------------------------------------------------------ ordering


def test_ordering_caps_how_much_of_the_answer_one_domain_can_own():
    same_site = [result(f"https://one.test/{i}", score=1.0) for i in range(5)]
    other = result("https://two.test/a", score=0.1)
    ranked = order([*same_site, other])

    top = ranked[: MAX_PER_DOMAIN + 1]
    assert sum(1 for r in top if r.domain == "one.test") == MAX_PER_DOMAIN
    assert "two.test" in {r.domain for r in top}
    # Capped, not dropped: the rest are still available if nothing better exists.
    assert len(ranked) == 6


def test_ordering_puts_the_primary_source_first():
    ranked = order(
        [
            result("https://contentfarm.test/guide", score=1.0, title="Ultimate Guide 2026"),
            result("https://eur-lex.europa.eu/eli/reg/2024/1689", score=0.3),
        ]
    )
    assert ranked[0].domain == "eur-lex.europa.eu"


# -------------------------------------------------------------------- lenses


def test_lenses_resolve_in_catalogue_order_whatever_order_they_arrive_in():
    assert lens_module.ids_of(lens_module.resolve(["news", "web"])) == ["web", "news"]


def test_an_unknown_or_empty_lens_selection_falls_back_instead_of_searching_nothing():
    assert lens_module.ids_of(lens_module.resolve(["nonsense"])) == list(lens_module.DEFAULT_LENSES)
    assert lens_module.ids_of(lens_module.resolve([])) == list(lens_module.DEFAULT_LENSES)
    assert lens_module.ids_of(lens_module.resolve(None)) == list(lens_module.DEFAULT_LENSES)


def test_auto_is_a_choice_the_run_can_see_and_never_a_corpus_it_searches():
    """It travels as an id because the picker, the settings row and the API all
    carry ids, but nothing may ever search it."""
    assert lens_module.AUTO not in lens_module.LENSES
    assert lens_module.normalise(["auto"]) == ["auto"]
    assert lens_module.wants_auto(["auto", "news"]) is True
    # `resolve` is what a search draws on, so auto disappears there.
    assert lens_module.ids_of(lens_module.resolve(["auto"])) == list(lens_module.DEFAULT_LENSES)
    assert lens_module.normalise(["news", "web"]) == ["web", "news"]


# ------------------------------------------------------- the recency window
# What these pin: a time range is a filter at the engine, not a preference in the
# ranking. "Restricted to the past year" on a paper database is a different
# question: the 2005 paper that answers it is absent from the response, and
# nothing downstream can rank back what was never returned.


def test_without_a_window_every_engine_is_asked_once():
    groups = _request_groups(lens_module.resolve(["web", "scholar"]), "")
    assert len(groups) == 1
    engines, applied = groups[0]
    assert applied == ""
    assert "openalex" in engines and "duckduckgo" in engines


def test_a_window_never_reaches_the_paper_databases():
    groups = _request_groups(lens_module.resolve(["web", "scholar"]), "year")
    windowed = {applied: engines for engines, applied in groups}
    assert set(windowed) == {"year", ""}
    assert "duckduckgo" in windowed["year"]
    assert "pubmed" not in windowed["year"]
    assert set(windowed[""]) >= {"pubmed", "openalex", "arxiv", "crossref"}


def test_an_engine_shared_with_a_windowed_corpus_is_asked_once_and_keeps_the_window():
    """Two requests for one engine per query is how a self-hosted instance talks
    itself into a CAPTCHA, and the reference lens is general engines throughout."""
    groups = _request_groups(lens_module.resolve(["web", "reference"]), "year")
    assert len(groups) == 1
    engines, applied = groups[0]
    assert applied == "year"
    assert engines.count("duckduckgo") == 1


def test_a_timeless_corpus_searched_alone_drops_the_window_entirely():
    groups = _request_groups(lens_module.resolve(["scholar"]), "month")
    assert groups == [(list(lens_module.SCHOLAR.engines), "")]


async def test_the_split_search_asks_both_groups_and_merges_what_they_answer(monkeypatch):
    """The two requests are one search: results pooled, refusals pooled, and the
    footer still reports the window that was actually applied."""
    sent: list[dict[str, str]] = []

    async def fake_get(params: dict[str, str]) -> dict:
        sent.append(params)
        engine = "openalex" if "openalex" in params["engines"] else "duckduckgo"
        return {
            "results": [_raw(url=f"https://{engine}.test/x", engine=engine, engines=[engine])],
            "unresponsive_engines": [["startpage", "CAPTCHA"]] if engine == "duckduckgo" else [],
        }

    monkeypatch.setattr("app.search.searxng._get", fake_get)
    results, report = await searxng.search(
        "creatine", lenses=lens_module.resolve(["web", "scholar"]), time_range="year"
    )

    assert len(sent) == 2
    assert {params.get("time_range", "") for params in sent} == {"year", ""}
    assert {result.domain for result in results} == {"openalex.test", "duckduckgo.test"}
    assert report.unresponsive == {"startpage": "CAPTCHA"}
    assert report.time_range == "year"


def test_the_classifier_is_offered_every_corpus_except_pictures():
    """Images are retrieved by their own search when an answer wants one.
    Letting them into the evidence means the answer gets written from
    stock-photo pages."""
    assert "images" not in lens_module.CHOOSABLE
    assert set(lens_module.CHOOSABLE) == set(lens_module.LENSES) - {"images"}
    listing = lens_module.choices()
    for lens_id in lens_module.CHOOSABLE:
        assert f'"{lens_id}"' in listing


def test_an_engine_that_answered_is_not_also_reported_as_silent():
    """A run makes several requests and SearXNG reports refusals per request, so
    an engine rate limited on the third query still answered the first two. The
    footer said "No answer from: brave" directly under brave's twenty results."""
    report = SearchReport(queries=1)
    report.engines.update({"brave": 20})
    report.unresponsive = {"brave": "Suspended: too many requests", "startpage": "CAPTCHA"}

    assert report.refused == {"startpage": "CAPTCHA"}
    assert [e["name"] for e in report.as_dict()["unresponsive"]] == ["startpage"]


def test_no_lens_searches_a_dictionary():
    """A dictionary matches a word, not a question: with wiktionary enabled,
    "wie wird das Wetter morgen" came back with the entry for "wie" in the
    source list. The engine names live in two places (the lens and the SearXNG
    config), and this is the half that must not drift."""
    for lens in lens_module.LENSES.values():
        for engine in lens.engines:
            assert "wiktionary" not in engine, f"{lens.id} names {engine}"
            assert "dictionary" not in engine, f"{lens.id} names {engine}"


def test_every_lens_names_engines_and_a_category_fallback():
    for lens in lens_module.LENSES.values():
        assert lens.engines, lens.id
        assert lens.categories, lens.id
        assert lens.query_hint, lens.id


# ------------------------------------------------------- the searxng boundary


def _raw(**overrides) -> dict:
    base = {
        "title": "PDF Creatine and muscle mass",
        "url": "https://example.test/paper.pdf",
        "content": "Some  snippet\ntext",
        "engine": "bing",
        "engines": ["bing", "duckduckgo"],
        "score": 1.5,
        "category": "general",
    }
    return {**base, **overrides}


def test_parsing_keeps_every_engine_that_returned_a_result():
    parsed = _parse(_raw())
    assert parsed is not None
    assert parsed.engines == ["bing", "duckduckgo"]
    assert parsed.title == "Creatine and muscle mass"
    assert parsed.kind == KIND_PDF
    assert parsed.snippet == "Some snippet text"


def test_parsing_reads_the_paper_metadata_the_science_engines_return():
    parsed = _parse(
        _raw(
            url="https://arxiv.org/abs/1234",
            category="science",
            template="paper.html",
            doi="10.1000/xyz",
            journal="Journal of Tests",
            authors=["A. Author", ""],
            publishedDate="2024-03-01T00:00:00",
        )
    )
    assert parsed is not None
    assert parsed.kind == KIND_PAPER
    assert parsed.tier == quality.ACADEMIC
    assert parsed.published.startswith("2024-03-01")
    assert parsed.authors == ["A. Author"]


def test_non_http_results_are_refused():
    assert _parse(_raw(url="javascript:alert(1)")) is None
    assert _parse(_raw(url="")) is None


def test_a_protocol_relative_picture_is_kept_not_thrown_away():
    """Image engines return `//host/path` routinely: SearXNG's own HTML path
    prepends the scheme. Dropping them is a picture search that finds pictures
    and then reports that it found none."""
    parsed = _parse(_raw(img_src="//upload.wikimedia.org/x.jpg", thumbnail="//host/t.jpg"))
    assert parsed is not None
    assert parsed.image == "https://upload.wikimedia.org/x.jpg"
    assert parsed.thumbnail == "https://host/t.jpg"


def test_pictures_that_cannot_be_shown_are_still_refused():
    """Mixed content is blocked by the browser, and a data: payload of unknown
    size would be written into the transcript."""
    assert _parse(_raw(img_src="http://insecure.test/x.jpg")).image == ""
    assert _parse(_raw(img_src="data:image/png;base64,AAAA")).image == ""


def test_unresponsive_engines_are_read_back_as_reasons():
    payload = {"unresponsive_engines": [["startpage", "CAPTCHA"], ["brave"], "qwant"]}
    assert _unresponsive(payload) == {
        "startpage": "CAPTCHA",
        "brave": "no response",
        "qwant": "no response",
    }


def test_a_search_report_accumulates_across_the_phases_of_a_run():
    first = SearchReport(queries=1, lenses=["web"])
    first.engines.update({"bing": 5})
    first.unresponsive["startpage"] = "CAPTCHA"

    second = SearchReport(queries=2, lenses=["scholar"])
    second.engines.update({"bing": 3, "pubmed": 4})
    second.unresponsive["startpage"] = "suspended"

    first.merge(second)
    assert first.queries == 3
    assert first.engines["bing"] == 8
    assert first.lenses == ["web", "scholar"]
    # The first reason is the useful one; ten repeats of it are not.
    assert first.unresponsive == {"startpage": "CAPTCHA"}
    assert first.engine_names[0] == "bing"


def test_the_report_serialises_to_what_the_footer_renders():
    report = SearchReport(queries=1, lenses=["web"], time_range="year")
    report.engines.update({"bing": 2})
    report.unresponsive["brave"] = "too many requests"

    payload = report.as_dict()
    assert payload["engines"] == [{"name": "bing", "results": 2}]
    assert payload["unresponsive"] == [{"name": "brave", "reason": "too many requests"}]
    assert payload["time_range"] == "year"


# ------------------------------------------------------------------ relevance
# The bug these pin: a "Reference" search for the ship of Theseus returned ten
# book-catalogue records ("The pig that wants to be eaten", "Biblical commentary
# the Old Testament"), every one of them correctly labelled a reference work, and
# every one about something else. Provenance without relevance promotes junk,
# because a catalogue *is* a reference work, of the wrong thing.


def test_results_about_something_else_are_dropped_whatever_their_tier():
    question = "Erklär mir, was das Schiff von Theseus ist."
    results = [
        result("https://openlibrary.org/w/1", title="The pig that wants to be eaten"),
        result("https://en.wikibooks.org/w/2", title="Scientific racism in Germany"),
        result("https://en.wikisource.org/w/3", title="Biblical commentary the Old Testament"),
        result(
            "https://de.wikipedia.org/wiki/x",
            title="Schiff des Theseus",
            snippet="Ein Gedankenexperiment.",
        ),
    ]
    kept, dropped = keep_relevant(results, question)

    assert [r.title for r in kept] == ["Schiff des Theseus"]
    assert dropped == 3


def test_the_question_language_does_not_decide_relevance():
    """The question is German, the source is English, and the subject is the same
    word in both, which is the case the stop lists have to survive."""
    kept, _ = keep_relevant(
        [result("https://x.test/a", title="Ship of Theseus", snippet="A thought experiment")],
        "Was ist das Schiff von Theseus?",
    )
    assert kept


def test_a_search_that_matched_nothing_registers_nothing():
    """Keeping everything, on the reasoning that weak sources labelled as such
    beat a blank page, fails once the engines start refusing. A search
    that answers "EU AI Act" with two pizza chains has not found weak evidence,
    it has found none, and numbering it produces an answer whose every citation
    points somewhere else. The empty path answers from the model and says so."""
    results = [result("https://x.test/a", title="Something unrelated", snippet="")]
    kept, dropped = keep_relevant(results, "quantum chromodynamics lattice spacing")
    assert kept == []
    assert dropped == 1


def test_a_query_with_no_readable_subject_still_keeps_its_results():
    """The guard on the guard: a query this tokenizer cannot read must not be
    turned into an empty search."""
    results = [result("https://x.test/a", title="天气预报", snippet="")]
    kept, _dropped = keep_relevant(results, "的")
    assert kept == results


def test_a_question_with_no_content_words_keeps_everything():
    kept, dropped = keep_relevant([result("https://x.test/a")], "was ist das?")
    assert kept and dropped == 0


# ---------------------------------------------------- the wikipedia infobox
# SearXNG's wikipedia engine never populates `results`. It answers with an
# infobox, and only on an exact title match. Reading `results` alone is why a
# reference search fell through to whatever book catalogue keyword-matched the
# query instead.


def test_an_infobox_is_read_as_a_source():
    results = _results_of(
        {
            "results": [],
            "infoboxes": [
                {
                    "infobox": "Schiff des Theseus",
                    "content": "Das Schiff des Theseus ist ein Gedankenexperiment.",
                    "urls": [{"title": "Wikipedia", "url": "https://de.wikipedia.org/wiki/Schiff"}],
                    "engine": "wikipedia",
                    "engines": ["wikipedia"],
                }
            ],
        }
    )
    assert len(results) == 1
    assert results[0].title == "Schiff des Theseus"
    assert results[0].tier == quality.REFERENCE
    assert results[0].engines == ["wikipedia"]


def test_an_infobox_without_a_usable_url_or_body_is_not_a_source():
    assert _results_of({"infoboxes": [{"infobox": "X", "content": "text", "urls": []}]}) == []
    assert (
        _results_of(
            {"infoboxes": [{"infobox": "X", "content": "", "urls": [{"url": "https://a.test"}]}]}
        )
        == []
    )


def test_the_reference_lens_does_not_rest_on_book_catalogues():
    """Openlibrary, Wikisource and Wikibooks keyword-match anything, so asking
    them for a concept returns books that merely contain the words."""
    engines = set(lens_module.REFERENCE.engines)
    assert "wikipedia" in engines
    assert not engines & {"openlibrary", "wikisource", "wikibooks", "wikiquote"}
    # Wikipedia only ever answers through its infobox, so something else has to
    # carry the lens when there is no exact-title article.
    assert engines & {"duckduckgo", "google cse", "bing", "mojeek"}


def test_phrasing_terms_no_result_uses_are_not_required():
    """ "Is creatine worth taking?": `worth` and `taking` appear in no paper, so
    requiring them fails every paper on the topic and the fallback then keeps the
    irrelevant one too. A term the corpus never uses is phrasing, not subject."""
    results = [
        result("https://a.test/1", title="Creatine supplementation augments strength gains"),
        result("https://b.test/2", title="Few-shot fat-free thigh muscle segmentation"),
        result("https://c.test/3", title="Effects of creatine supplementation and training"),
    ]
    kept, dropped = keep_relevant(results, "Is creatine worth taking?")

    assert [r.url for r in kept] == ["https://a.test/1", "https://c.test/3"]
    assert dropped == 1


def test_a_two_letter_acronym_is_a_subject_term():
    """ "EU" and "AI" are the whole subject of the question; dropping them as too
    short leaves only "changed" and "act", which match anything."""
    kept, dropped = keep_relevant(
        [
            result("https://a.test/1", title="EU AI Act amendments defer obligations"),
            result("https://b.test/2", title="Weather Europe today (2026/08/12)"),
            result("https://c.test/3", title="Anthropic starts marking Claude's outputs"),
        ],
        "What changed in the EU AI Act in 2026?",
    )
    assert [r.url for r in kept] == ["https://a.test/1"]
    assert dropped == 2


def test_a_short_term_does_not_match_a_word_that_merely_contains_it():
    """`eu` inside `europe` is a coincidence; `schiff` inside `schiffes` is not.
    A German compound is why the substring tolerance exists at all, and a
    two-letter acronym is why it has to stop somewhere."""
    eu, europe = quality.relevance_scores("EU rules", ["EU rules apply", "Europe today"])
    assert eu > europe == 0.0

    compound, unrelated = quality.relevance_scores(
        "Schiff Theseus", ["Des Schiffes Theseus Paradoxon", "Ein Buch über Schweine"]
    )
    assert compound == 1.0 and unrelated == 0.0


def test_a_term_the_whole_corpus_shares_separates_nothing():
    """Every result in a news search mentions 2026; that is the calendar, not the
    subject, so a match on it alone must not carry a source.

    Judged against the best result rather than against a constant. The three
    stragglers here share the year and nothing else, and half of the best result
    is not enough, while in a corpus where every result matches almost as well
    as the best one, that same rule keeps them all."""
    results = [
        result("https://a.test/1", title="EU AI Act obligations 2026"),
        result("https://b.test/2", title="Marketing analytics solution of 2026"),
        result("https://c.test/3", title="Semiconductor push 2026"),
        result("https://d.test/4", title="Weather report 2026"),
    ]
    kept, dropped = keep_relevant(results, "EU AI Act 2026")

    assert [r.url for r in kept] == ["https://a.test/1"]
    assert dropped == 3


def test_results_that_all_match_almost_as_well_are_all_kept():
    """The bug this pins, and the reason the bar is relative: five of these six
    say "Mamdani" and one says "Zohran Mamdani". Weighted so that the rarer word
    counted for more, the five scored 0.26 against a floor of 0.34, and a
    question about a sitting mayor was answered from one source, with nothing to
    check it against."""
    titles = [
        "Zohran Mamdani - Wikipedia, the 112th mayor of New York City",
        "Mamdani sworn in as mayor of New York City",
        "Who is Mamdani? The democratic socialist who won City Hall",
        "Mamdani unveils his first budget",
        "Mayor Mamdani announces a free bus pilot",
        "Mamdani: what his win means for the Democratic party",
    ]
    results = [result(f"https://s{i}.test/x", title=title) for i, title in enumerate(titles)]
    kept, dropped = keep_relevant(results, "Wer ist Zohran Mamdani?")

    assert len(kept) == 6 and dropped == 0
    # And the one that matches in full still leads the ranking.
    assert order(kept)[0].url == "https://s0.test/x"


def test_ordering_never_trades_evidence_quality_for_a_picture():
    """Citation numbers are handed out in ranking order, so anything that lifts a
    source for *presentation* rewrites which evidence the answer is written from.
    Preferring results that carry an image when the answer will show one makes
    an encyclopaedia article lose [1] to a blog that happens to have a photo.
    The picture is chosen separately."""
    encyclopaedia = result("https://de.wikipedia.org/wiki/x", score=0.5)
    blog_with_photo = result("https://someblog.test/y", score=0.9)
    blog_with_photo.image = "https://someblog.test/photo.jpg"

    ranked = order([blog_with_photo, encyclopaedia])
    assert ranked[0].domain == "de.wikipedia.org"


# ------------------------------------------------------------- the registry


async def test_no_single_site_may_occupy_the_answer(redis):
    """Observed: "wie installiere ich pytorch mit cuda" came back with four
    Docker Hub tags out of ten sources, and a flag question with three pages
    from one stock library. A question answered by four pages of one site was
    researched once, not four times."""
    from app.agent.context import SourceRegistry
    from app.store.stream import RunStream

    registry = SourceRegistry(RunStream(redis, "cap1", ttl=60), 10)
    pages = [result(f"https://hub.docker.com/r/pytorch/{i}") for i in range(5)]
    pages += [result("https://pytorch.org/get-started"), result("https://stackoverflow.com/q/1")]
    for page in pages:
        page.assess()

    registered = await registry.add(pages)
    kept = [r.domain for _index, r in registered]

    assert kept.count("hub.docker.com") == 2
    assert "pytorch.org" in kept and "stackoverflow.com" in kept


async def test_the_cap_scales_with_the_preset(redis):
    """A deep run has room for a subject that genuinely lives on one site."""
    from app.agent.context import SourceRegistry
    from app.store.stream import RunStream

    registry = SourceRegistry(RunStream(redis, "cap2", ttl=60), 28)
    pages = [result(f"https://one.test/{i}") for i in range(10)]
    for page in pages:
        page.assess()

    registered = await registry.add(pages)
    assert len(registered) == 7  # 28 // 4


# ------------------------------------------------------------- query shaping


def test_a_planned_sub_question_is_searched_as_keywords():
    """The deep path handed its sub-questions to the engines verbatim, and a
    German sentence beginning "Welche neuen Bestimmungen…" is answered by
    matching the word "welche": one run came back with eight sources about
    German pronouns (Duden, DWDS, Wiktionary), and an answer explaining that
    they do not mention the AI Act."""
    reduced = quality.as_query(
        "Welche neuen oder geänderten Bestimmungen des EU AI Act sind seit seinem "
        "Inkrafttreten am 1. August 2024 hinzugekommen, einschließlich Leitlinien?"
    )
    assert "welche" not in reduced.lower()
    assert "EU" in reduced and "Act" in reduced
    assert len(reduced.split()) <= 10


def test_query_shaping_never_returns_nothing():
    """A script this tokenizer cannot read must still be searched."""
    assert quality.as_query("的天气预报") == "的天气预报"
    assert quality.as_query("wie und was") == "wie und was"


def _compose_engines() -> set[str]:
    """The `keep_only` list of the SearXNG config in docker-compose.yml.

    Read by indentation rather than with a YAML parser, which would be a
    dependency for one test: the list ends at the first line indented less
    than its items that is not a comment.
    """
    lines = (Path(__file__).parent.parent / "docker-compose.yml").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "keep_only:")
    indent = None
    engines: set[str] = set()
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        depth = len(line) - len(line.lstrip())
        if indent is None:
            indent = depth
        if depth < indent or not stripped.startswith("- "):
            break
        engines.add(stripped[2:].strip())
    return engines


def test_every_engine_a_lens_names_is_kept_in_the_searxng_config():
    """SearXNG ignores an engine name it does not know, and a `keep_only` that
    lacks an engine a lens asks for leaves that lens searching nothing there.
    The two lists live in different files, so this keeps them together."""
    kept = _compose_engines()
    assert len(kept) > 20, "keep_only list not found in docker-compose.yml"
    lenses = [v for v in vars(lens_module).values() if isinstance(v, lens_module.Lens)]
    missing = {e for lens in lenses for e in lens.engines} - kept
    assert not missing, f"named by a lens but not kept in docker-compose.yml: {sorted(missing)}"
