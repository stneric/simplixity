"""Search deduplication, chunking and ranking.

Ranking is the difference between an answer built on the relevant paragraph and
one built on a cookie banner, and it has to keep working when the embedding
provider does not.
"""

from __future__ import annotations

from app.embed import rerank
from app.llm import Usage
from app.search.base import SearchResult, canonical_url, dedupe
from app.search.content import _clean, _unreadable


def result(url: str, *, title: str = "t", score: float = 0.0, content: str = "") -> SearchResult:
    return SearchResult(title=title, url=url, snippet="snip", score=score, content=content)


# ------------------------------------------------------------------ dedupe


def test_canonical_url_ignores_scheme_www_and_trailing_slash():
    same = {
        canonical_url("https://www.example.com/a/"),
        canonical_url("http://example.com/a"),
        canonical_url("https://example.com/a/"),
    }
    assert len(same) == 1


def test_canonical_url_rejects_non_http_schemes():
    assert canonical_url("javascript:alert(1)") == ""
    assert canonical_url("ftp://example.com/x") == ""
    assert canonical_url("not a url") == ""


def test_wikipedia_language_editions_of_one_article_are_one_source():
    """`de.` and `fi.` for the same title are one encyclopaedia, not two engines
    agreeing, and the second is an article the reader cannot read."""
    same = {
        canonical_url("https://de.wikipedia.org/wiki/SearXNG"),
        canonical_url("https://fi.wikipedia.org/wiki/SearXNG"),
        canonical_url("https://en.wikipedia.org/wiki/SearXNG"),
    }
    assert len(same) == 1


def test_different_wikipedia_articles_and_other_wikimedia_hosts_stay_apart():
    assert canonical_url("https://de.wikipedia.org/wiki/Katze") != canonical_url(
        "https://de.wikipedia.org/wiki/Hund"
    )
    # Commons is a media archive, not a language edition of anything.
    assert canonical_url("https://commons.wikimedia.org/wiki/X") != canonical_url(
        "https://de.wikipedia.org/wiki/X"
    )


def test_the_edition_the_wikipedia_engine_returned_wins_over_a_higher_score():
    """A general engine's score says nothing about which language was asked for."""
    fi = result("https://fi.wikipedia.org/wiki/SearXNG", score=0.9)
    fi.engines = ["google cse"]
    de = result("https://de.wikipedia.org/wiki/SearXNG", score=0.2)
    de.engines = ["wikipedia"]

    kept = dedupe([fi, de])
    assert len(kept) == 1
    assert kept[0].url == "https://de.wikipedia.org/wiki/SearXNG"
    # The evidence that two engines found it survives the collapse.
    assert set(kept[0].engines) == {"wikipedia", "google cse"}


def test_dedupe_keeps_the_better_scoring_copy():
    kept = dedupe(
        [result("https://a.test/x", score=0.2), result("https://www.a.test/x/", score=0.9)]
    )
    assert len(kept) == 1
    assert kept[0].score == 0.9


def test_dedupe_never_throws_away_already_fetched_content():
    """Content costs a network round trip; the deduper must not discard it just
    because the duplicate scored higher."""
    kept = dedupe(
        [
            result("https://a.test/x", score=0.9, content="the full page"),
            result("https://a.test/x", score=0.1),
        ]
    )
    assert kept[0].content == "the full page"


# ----------------------------------------------------------------- chunking


def test_short_text_is_one_chunk():
    assert rerank.chunk_text("short") == ["short"]


def test_chunks_break_on_paragraphs_and_stay_under_the_limit():
    text = "\n\n".join(f"Paragraph number {i} " + "word " * 40 for i in range(12))
    chunks = rerank.chunk_text(text, size=600, overlap=50)
    assert len(chunks) > 1
    assert all(len(c) <= 600 + 50 for c in chunks)
    assert "".join(chunks).count("Paragraph") >= 12


def test_a_single_oversized_paragraph_is_split_rather_than_dropped():
    chunks = rerank.chunk_text("x" * 5000, size=1000, overlap=100)
    assert len(chunks) >= 5
    assert all(len(c) <= 1000 for c in chunks)


# ------------------------------------------------------------------ ranking


def _passages(*pairs: tuple[str, int]) -> list[rerank.Passage]:
    return [
        rerank.Passage(text, index, f"Source {index}", f"https://s{index}.test")
        for text, index in pairs
    ]


def test_lexical_ranking_prefers_the_passage_that_answers_the_question():
    passages = _passages(
        ("A recipe for sourdough bread with a long fermentation.", 1),
        ("The XAUTOCLAIM command transfers ownership of pending stream entries.", 2),
        ("Cookie policy and privacy notice for this website.", 3),
    )
    rerank._lexical_scores("what does XAUTOCLAIM do with pending entries", passages)
    assert max(passages, key=lambda p: p.score).source_index == 2


def test_ranking_caps_passages_per_source_so_the_evidence_stays_broad():
    # One long page produces many high-scoring chunks; it must not crowd out
    # every corroborating source.
    passages = _passages(*[("hit", 1) for _ in range(10)], *[("hit", 2) for _ in range(3)])
    for index, passage in enumerate(passages):
        passage.score = 100 - index  # source 1 wins on raw score everywhere

    taken = rerank._take_diverse(passages, top_k=6)
    from_source_one = sum(1 for p in taken if p.source_index == 1)
    assert from_source_one <= rerank.MAX_PER_SOURCE
    assert any(p.source_index == 2 for p in taken)


def test_diversity_cap_relaxes_rather_than_returning_too_few():
    """If breadth cannot fill the quota, the runners-up get in, an answer with
    fewer passages than budgeted is worse than a slightly lopsided one."""
    passages = _passages(*[("hit", 1) for _ in range(8)])
    assert len(rerank._take_diverse(passages, top_k=5)) == 5


def test_cosine_handles_mismatched_and_empty_vectors():
    assert rerank._cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert rerank._cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert rerank._cosine([1.0], [1.0, 2.0]) == 0.0
    assert rerank._cosine([0.0, 0.0], [0.0, 0.0]) == 0.0


async def test_ranking_falls_back_to_lexical_when_embeddings_are_unavailable(monkeypatch):
    async def no_embeddings(*_args, **_kwargs):
        return None

    monkeypatch.setattr(rerank, "embed", no_embeddings)
    registered = [
        (1, result("https://a.test/1", content="irrelevant filler about gardening. " * 40)),
        (
            2,
            result(
                "https://b.test/2",
                content="the answer mentions quantum tunnelling explicitly. " * 40,
            ),
        ),
    ]
    passages = await rerank.rank_passages(
        "quantum tunnelling", registered, top_k=2, model="some/embedder"
    )
    assert passages
    assert passages[0].source_index == 2


async def test_the_reranker_bills_what_the_embedding_model_charged(monkeypatch):
    """Embeddings go out over plain httpx and have no `LLM` to hang off, so
    they were the one model call the footer's price never covered. Free today
    only because the configured model happens to be one of the free ones."""
    sink = Usage()

    async def priced_embeddings(texts, *, model, provider="openrouter", usage=None):
        if usage is not None:
            usage.add({"prompt_tokens": 4096, "total_tokens": 4096, "cost": 0.00012})
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(rerank, "embed", priced_embeddings)
    registered = [(i, result(f"https://a.test/{i}", content="text " * 80)) for i in range(1, 5)]

    await rerank.rank_passages("anything", registered, top_k=2, model="some/embedder", usage=sink)

    assert sink.prompt_tokens == 4096
    assert sink.as_dict()["cost_usd"] == 0.00012


# ------------------------------------------------------------------ content


def test_clean_strips_the_reader_header_and_collapses_blank_lines():
    raw = (
        "Title: Something\nURL Source: https://x.test\n\n"
        "Markdown Content:\nBody line\n\n\n\nNext line"
    )
    assert _clean(raw, 1000) == "Body line\n\nNext line"


WIKI_CHROME = """Markdown Content:
[Zum Inhalt springen](https://de.wikipedia.org/wiki/X#bodyContent)

- [x] Hauptmenü

*   [Hauptseite](https://de.wikipedia.org/wiki/Wikipedia:Hauptseite "Hauptseite")
*   [Themenportale](https://de.wikipedia.org/wiki/Portal:Wikipedia_nach_Themen)
*   [Zufälliger Artikel](https://de.wikipedia.org/wiki/Spezial:Zuf%C3%A4llige_Seite)
*   [Spenden](https://donate.wikimedia.org/w/index.php?campaign=x "Unterstütze uns")

DiCaprio, Sohn des [Comicbuch](https://de.wikipedia.org/wiki/Comic "Comic")-Autors und -verlegers [George DiCaprio](https://de.wikipedia.org/wiki/George_DiCaprio "George DiCaprio"), wuchs bei seiner Mutter auf.

*   [Bearbeiten](https://de.wikipedia.org/w/index.php?action=edit)
*   [Versionsgeschichte](https://de.wikipedia.org/w/index.php?action=history)
"""  # noqa: E501, a real page, and its paragraph is one line


def test_clean_drops_the_navigation_and_keeps_the_article():
    """The bug this closes: the reader returns Wikipedia *with* its sidebar, the
    German DiCaprio article opens with 24 000 characters of menu (exactly the
    cap), so a run read the navigation, none of the article, and reported that
    the sources do not say what his father did."""
    cleaned = _clean(WIKI_CHROME, 24000)

    assert "George DiCaprio" in cleaned
    assert "Comicbuch" in cleaned  # a sentence keeps its own links
    assert "Hauptseite" not in cleaned
    assert "Zufälliger Artikel" not in cleaned
    assert "Versionsgeschichte" not in cleaned


def test_a_page_that_really_is_a_list_of_links_keeps_them():
    """The filter may not empty a page: a link directory is legitimate content,
    and half a page is better evidence than none."""
    raw = "Markdown Content:\n" + "\n".join(
        f"*   [Result {i}](https://example{i}.test/page)" for i in range(20)
    )
    assert _clean(raw, 5000).count("Result") == 20


def test_clean_truncates_on_a_paragraph_boundary():
    text = "Markdown Content:\n" + "\n\n".join("paragraph " * 20 for _ in range(20))
    cleaned = _clean(text, 900)
    assert len(cleaned) <= 900
    assert not cleaned.endswith("parag")  # never mid-word


WALLS = [
    "Just a moment...\n\nEnable JavaScript and cookies to continue.",
    "Verifying you are human. This may take a few seconds.",
    "Checking if the site connection is secure\n\nneeds to review your connection.",
    "Sorry, we can not verify that you are not a robot when JavaScript is turned off.",
    "Access denied\n\nYour request has been blocked.",
    "Sicherheitsabfrage\n\nBitte aktivieren Sie JavaScript, um fortzufahren.",
]


def test_a_challenge_screen_is_not_a_page():
    """The bug this closes: publishers answer an automated read with HTTP 200
    and a bot-verification screen, so nothing upstream saw a failure. It became
    the source's content, was ranked like prose and reached the model, which
    reported that the source "contains only a bot-verification interstitial",
    four times in one deep run, which is why that report was thin."""
    for wall in WALLS:
        assert _unreadable(wall), wall


def test_a_paper_about_captchas_is_still_a_paper():
    """The wall phrases are matched on a short body only. A filter that threw
    away every page saying "captcha" would silently blind the app to a research
    field."""
    paper = "This paper evaluates CAPTCHA solving with deep networks. " * 40
    assert not _unreadable(paper)


def test_a_page_with_no_body_is_not_a_page():
    assert _unreadable("Page not found")
    assert _unreadable("")


def test_lexical_ranking_reads_german_words_whole():
    """The fallback tokenizer was ASCII-only: "Größe" was "gr" and
    "Wärmepumpen" was "rmepumpen", so a German question was ranked by the
    fragments of its words that happened to be ASCII."""
    assert rerank._tokens("Welche Größe haben Wärmepumpen?") == {"größe", "haben", "wärmepumpen"}
    passages = _passages(
        ("Die Größe einer Wärmepumpe richtet sich nach der Heizlast.", 1),
        ("Grüne Rabatte gibt es im Baumarkt.", 2),
    )
    rerank._lexical_scores("Welche Größe haben Wärmepumpen?", passages)
    assert max(passages, key=lambda p: p.score).source_index == 1


async def test_a_page_of_the_papers_is_ranked_by_its_own_passages(monkeypatch):
    """A library page arrives cut at sentences, matched passages first. The
    reranker takes those rather than cutting the page again, and where it can
    tell nothing apart, a German question against an English page, the
    passages the library search matched win the tie."""

    async def no_embeddings(*_args, **_kwargs):
        return None

    monkeypatch.setattr(rerank, "embed", no_embeddings)
    page = result("/library/f/2", content="whole page")
    page.excerpts = ["the matched passage " * 10, "intro text " * 20, "outro " * 30]
    page.matched = 1
    other = result("/library/g/1", content="other page")
    other.excerpts = ["unmatched filler " * 20] * 3
    other.matched = 0
    passages = await rerank.rank_passages(
        "Wie funktioniert das?", [(1, page), (2, other)], top_k=2, model="some/embedder"
    )
    assert passages[0].text.startswith("the matched passage")
    assert all(p.text != "whole page" for p in passages)


# --------------------------------------------------------- what gets embedded


def test_each_model_gets_the_markers_it_was_trained_with():
    """Nemotron-3-Embed only ever saw "query: " and "passage: " in training,
    and its card measures cross-lingual similarity collapsing without them."""
    from app.embed import embedder

    assert embedder.prefixes("nvidia/nemotron-3-embed-1b:free") == ("query: ", "passage: ")
    assert embedder.prefixes("intfloat/multilingual-e5-large") == ("query: ", "passage: ")
    assert embedder.prefixes("BAAI/bge-m3") == ("", "")
    assert embedder.prefixes("openai/text-embedding-3-small") == ("", "")
    nemotron = "nvidia/nemotron-3-embed-1b:free"
    assert embedder.prepare(["a"], nemotron, embedder.QUERY) == ["query: a"]
    assert embedder.prepare(["a"], nemotron, embedder.DOCUMENT) == ["passage: a"]
    assert embedder.prepare(["a"], nemotron, embedder.SYMMETRIC) == ["query: a"]
    assert embedder.passage_text("body", title="Paper") == "Paper\nbody"
    assert embedder.vector_tag("m") != "m"


async def test_the_reranker_marks_the_question_and_titles_the_passages(monkeypatch):
    seen = []

    async def recording(texts, *, model, provider="openrouter", usage=None):
        seen.extend(texts)
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(rerank, "embed", recording)
    registered = [
        (i, result(f"https://a.test/{i}", title=f"Page {i}", content="text " * 300))
        for i in range(1, 4)
    ]
    await rerank.rank_passages("frage", registered, top_k=2, model="nvidia/nemotron-3-embed-1b")
    assert seen[0] == "query: frage"
    assert all(text.startswith("passage: Page ") for text in seen[1:])
