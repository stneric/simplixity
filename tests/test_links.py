"""Links typed into a question: found in ordinary prose, public ones only.

The run that reads them is tested in `test_agent.py`, beside the fakes it
needs.
"""

from __future__ import annotations

import pytest

from app.agent import depth as depth_module
from app.agent import prompts
from app.search import content, links

ARTICLE = "https://news.example.org/kreatin-studie"

# ------------------------------------------------------------------ extraction


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("fasse https://example.org/artikel zusammen", ["https://example.org/artikel"]),
        ("see https://example.org/a.", ["https://example.org/a"]),
        ("(siehe https://example.org/a)", ["https://example.org/a"]),
        ("[Artikel](https://example.org/a)", ["https://example.org/a"]),
        ("„https://example.org/a“", ["https://example.org/a"]),
        (
            "https://en.wikipedia.org/wiki/Mercury_(planet)",
            ["https://en.wikipedia.org/wiki/Mercury_(planet)"],
        ),
        ("www.example.org/x, bitte", ["https://www.example.org/x"]),
        ("https://example.org/a?b=1&c=2#d", ["https://example.org/a?b=1&c=2#d"]),
        ("zweimal https://a.example/x und https://a.example/x", ["https://a.example/x"]),
        ("kein Link hier, nur example.org", []),
        (
            "was ist github.com/stneric/simplixity",
            ["https://github.com/stneric/simplixity"],
        ),
        ("lies spiegel.de/politik/artikel.html.", ["https://spiegel.de/politik/artikel.html"]),
        ("schreib an a@example.org/x", []),
        ("Node.js und README.md und z.B. dies", []),
    ],
)
def test_links_are_found_in_prose(text, expected):
    assert links.extract(text) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:6379/",
        "http://redis:6379/",
        "http://127.0.0.1/admin",
        "http://192.168.1.10/",
        "http://10.0.0.1/",
        "http://[::1]/",
        "http://printer.local/",
        "ftp://example.org/file",
    ],
)
def test_addresses_off_the_public_web_are_not_links(url):
    """The reader service may be self-hosted next to Redis and SearXNG."""
    assert links.extract(f"lies {url}") == []


def test_a_question_with_many_links_reads_only_the_first_few():
    text = " ".join(f"https://example{i}.org/" for i in range(10))
    assert len(links.extract(text)) == links.MAX_LINKS


def test_remove_leaves_the_words_around_a_link():
    assert links.remove("was sagt https://example.org/a über Kreatin") == "was sagt über Kreatin"


def test_the_reader_header_names_the_page():
    raw = (
        "Title: Kreatin im Test\n\nURL Source: https://example.org/a\n\n"
        "Published Time: 2026-10-01T08:00:00Z\n\nMarkdown Content:\nText"
    )
    assert content._header(raw, "Title") == "Kreatin im Test"
    assert content._header(raw, "Published Time") == "2026-10-01T08:00:00Z"
    assert content._header("Markdown Content:\nTitle: no", "Title") == ""


def test_the_classifier_sees_how_the_page_begins():
    messages = prompts.classify(
        f"stimmt das? {ARTICLE}",
        [],
        depth_module.BALANCED,
        linked=[(1, "Linked article", "It begins like this.")],
    )
    system = messages[0]["content"]
    assert "### Linked article\nIt begins like this." in system
    assert "never put a URL into a search query" in " ".join(system.split())
