"""End-to-end agent behaviour with the network replaced by fakes.

The properties under test are the ones that make the difference between a
research app and a spinner: every run reaches a terminal event, sources are
numbered consistently, a dead search still produces an answer, and depth
actually changes the shape of the run.
"""

from __future__ import annotations

import asyncio

import pytest

from app.agent import blocks as agent_blocks
from app.agent import budget as budget_module
from app.agent import deep, linked, orchestrator, research, verify, widgets
from app.agent import depth as depth_module
from app.agent.orchestrator import RunRequest
from app.llm import Delta, LLMError
from app.search import content
from app.search import weather as weather_source
from app.search.base import SearchResult
from app.search.searxng import SearchReport
from app.store.stream import RunStream


class FakeLLM:
    """Answers structured calls by schema and streams a fixed reply."""

    # Remote by default, which is what "no context budgeting" means: the writer
    # only fits its prompt to a window for a local runtime.
    is_local = False
    # Named, because the widget layer logs which model refused a schema, and a
    # double without a name turned every refusal into an AttributeError caught
    # three frames up, where it looked exactly like a widget that was never
    # asked for.
    model = "test/model"

    def __init__(
        self,
        *,
        answer: str = "An answer citing [1].",
        fail: Exception | None = None,
        classification: dict | None = None,
        gap: dict | None = None,
        verdicts: list | None = None,
    ):
        self.answer = answer
        self.fail = fail
        self.classification = classification or {}
        # What the citation check rules. Every claim supported by default, which
        # is the outcome every other test wants, and stated rather than left to
        # a default, because an unanswered claim is now reported as unchecked.
        # Positions past the end of a shorter claim list are simply never read.
        self.verdicts = verdicts or [
            verify.Verdict(claim=position, verdict="supported")
            for position in range(1, verify.JUDGED_LIMIT + 1)
        ]
        # What the second look concludes. Sufficient by default, so every other
        # test keeps the run it was written against.
        self.gap = gap or {}
        self.calls: list[str] = []

    async def structured(self, messages, schema, **kwargs):
        self.calls.append(schema.__name__)
        if self.fail:
            raise self.fail
        if schema is orchestrator.Classification:
            return schema(
                **{
                    "needs_search": True,
                    "standalone_question": "what is xautoclaim",
                    "search_queries": ["xautoclaim", "redis streams claim"],
                    **self.classification,
                }
            )
        if schema is deep.Plan:
            # About the same subject as `fake_results`: the relevance filter is
            # real in these tests, so a plan about something else would search,
            # match nothing and correctly investigate nothing.
            return schema(
                sub_questions=[
                    "what does xautoclaim do with pending entries",
                    "how does xautoclaim differ from xclaim",
                ]
            )
        if schema is deep.GapCheck:
            return schema(**{"sufficient": True, "follow_up_questions": [], **self.gap})
        if schema is verify.Support:
            return schema(verdicts=self.verdicts)
        if schema is verify.Breakdown:
            # The second look found no words for the finding: it stands.
            return schema()
        if schema.__module__ == widgets.__name__:
            # An empty widget: validation refuses it and the prose still runs,
            # which is the contract these tests are checking around it.
            return schema()
        raise AssertionError(f"unexpected schema {schema}")

    async def stream(self, messages, *, max_tokens=None, temperature=0.3):
        self.calls.append("stream")
        if self.fail:
            raise self.fail
        for word in self.answer.split(" "):
            yield Delta(content=word + " ")

    async def complete(self, messages, **kwargs):
        self.calls.append("complete")
        return "A Title"


def fake_results(count: int = 3) -> list[SearchResult]:
    results = [
        SearchResult(
            title=f"Result {i}",
            url=f"https://example{i}.test/page",
            snippet=f"Snippet {i} about xautoclaim and pending entries.",
            score=1.0 - i / 10,
            engines=["fake engine"],
        )
        for i in range(1, count + 1)
    ]
    for result in results:
        result.assess()
    return results


def fake_report(queries: int = 1, **overrides) -> SearchReport:
    report = SearchReport(queries=queries, lenses=["web"], **overrides)
    report.engines.update({"fake engine": 3})
    return report


@pytest.fixture
def wired(monkeypatch):
    """Replace search, page reading and the LLM; leave everything else real."""
    llm = FakeLLM()

    async def search_all(queries, *, limit_per_query=8, lenses=None, time_range=""):
        return fake_results(), fake_report(len(queries))

    async def fetch_many(results, *, limit, max_chars=None):
        for result in results:
            result.content = "Full page text about XAUTOCLAIM. " * 30
        return len(results)

    for module in (research, deep):
        if hasattr(module, "search_all"):
            monkeypatch.setattr(module, "search_all", search_all)
    monkeypatch.setattr(research.content_fetch, "fetch_many", fetch_many)
    monkeypatch.setattr(orchestrator, "LLM", lambda *a, **k: llm)
    return llm


def request(**overrides) -> RunRequest:
    base = {
        "query": "what does xautoclaim do",
        "chat_id": "chat1",
        "model": "test/model",
        "provider": "openrouter",
        "depth": "balanced",
        # Embeddings off: ranking falls back to lexical, which needs no network.
        "embedding_model": None,
    }
    return RunRequest(**{**base, **overrides})


async def collect(stream: RunStream) -> tuple[list, list, dict]:
    """Drain the stream into (events, block types, terminal payload)."""
    events, blocks, done = [], [], {}
    async for event in stream.read(last_id="0", block_ms=1):
        if event is None:
            break
        events.append(event)
        if event.event == "block":
            blocks.append(event.data["type"])
        if event.event == "done":
            done = event.data
    return events, blocks, done


# --------------------------------------------------------------------- quick


async def test_a_researched_run_emits_sources_then_an_answer(redis, wired):
    stream = RunStream(redis, "r1", ttl=60)
    await orchestrator.run(stream, request())

    _events, blocks, done = await collect(stream)

    assert done["status"] == "completed"
    assert blocks.index("research") < blocks.index("sources") < blocks.index("text")
    assert done["stats"]["searches"] >= 1
    assert done["stats"]["pages_fetched"] >= 1


async def test_sources_are_numbered_from_one_and_carry_a_domain(redis, wired):
    stream = RunStream(redis, "r2", ttl=60)
    await orchestrator.run(stream, request())

    sources = next(b for b in await stream.snapshot() if b["type"] == "sources")
    indexes = [s["index"] for s in sources["data"]["sources"]]
    assert indexes == list(range(1, len(indexes) + 1))
    assert all(s["domain"] for s in sources["data"]["sources"])


async def test_quick_depth_skips_page_reading(redis, wired):
    stream = RunStream(redis, "r3", ttl=60)
    await orchestrator.run(stream, request(depth="quick"))

    _events, _blocks, done = await collect(stream)
    assert done["stats"]["pages_fetched"] == 0  # quick is snippets only, by design


# -------------------------------------------------------------------- lookup


def weather_report() -> weather_source.Report:
    return weather_source.Report(
        location="Reutlingen",
        temperature="24",
        unit="°C",
        condition="partly-cloudy",
        summary="leicht bewölkt",
        as_of="13 Aug 2026, 14:00",
        engine="wttr.in",
        days=[weather_source.Day(day="Do", date="2026-08-14", high="26", low="15")],
    )


@pytest.fixture
def no_search(monkeypatch):
    """Any search at all is a failure in the lookup tests."""

    async def explode(*_args, **_kwargs):
        raise AssertionError("the lookup path must not search")

    monkeypatch.setattr(research, "search_all", explode)
    return explode


async def test_a_weather_question_is_fetched_without_searching_or_classifying(
    redis, wired, no_search, monkeypatch
):
    """The whole point of the router. Searched like any question, a forecast
    costs a classify call, two searches, five page reads, an embedding call and
    a widget call, and then arrives from a service in a third of a second."""

    async def lookup(place):
        assert place == "Reutlingen"
        return weather_report()

    monkeypatch.setattr(orchestrator.weather_source, "lookup", lookup)

    stream = RunStream(redis, "w1", ttl=60)
    await orchestrator.run(stream, request(query="wie ist das wetter in Reutlingen"))

    _events, blocks, done = await collect(stream)
    assert done["status"] == "completed"
    assert "weather" in blocks and "text" in blocks
    assert "sources" not in blocks  # nothing was searched, so nothing was cited
    # One call, and it is the one that writes the answer.
    assert done["stats"]["llm_calls"] == 1
    assert done["stats"]["searches"] == 0
    assert wired.calls == ["stream"]


async def test_the_fetched_reading_is_what_reaches_the_screen(redis, wired, no_search, monkeypatch):
    """Not a model's reading of it. An extracted number can be wrong in ways a
    fetched one cannot."""

    async def lookup(_place):
        return weather_report()

    monkeypatch.setattr(orchestrator.weather_source, "lookup", lookup)

    stream = RunStream(redis, "w2", ttl=60)
    await orchestrator.run(stream, request(query="wetter Reutlingen"))

    panel = next(b for b in await stream.snapshot() if b["type"] == "weather")
    assert panel["data"]["temperature"] == "24"
    assert panel["data"]["service"] == "wttr.in"
    assert panel["data"]["source"] == 0  # attributed by service, not by citation


async def test_a_home_location_answers_a_question_that_names_no_place(
    redis, wired, no_search, monkeypatch
):
    seen = {}

    async def lookup(place):
        seen["place"] = place
        return weather_report()

    monkeypatch.setattr(orchestrator.weather_source, "lookup", lookup)

    stream = RunStream(redis, "w3", ttl=60)
    await orchestrator.run(stream, request(query="wie ist das wetter heute", home_location="Ulm"))

    _events, _blocks, done = await collect(stream)
    assert seen["place"] == "Ulm"
    assert done["status"] == "completed"


async def test_without_a_place_or_a_home_location_the_run_says_so_and_searches(
    redis, wired, monkeypatch
):
    """Honest failure, not a guess. Geolocation is deliberately excluded by the
    CSP, so there is nowhere else a place could come from."""
    called = {"lookup": 0}

    async def lookup(place):
        called["lookup"] += 1
        return None

    monkeypatch.setattr(orchestrator.weather_source, "lookup", lookup)

    stream = RunStream(redis, "w4", ttl=60)
    await orchestrator.run(stream, request(query="wie ist das wetter heute"))

    _events, blocks, done = await collect(stream)
    assert called["lookup"] == 0  # nothing was looked up for nowhere
    assert "notice" in blocks
    assert done["status"] == "completed" and "text" in blocks


async def test_a_service_that_does_not_answer_falls_back_to_the_full_search(
    redis, wired, monkeypatch
):
    """The cheap preset was justified by the lookup working. Without it the
    question gets the reading budget it would have had anyway."""
    tried = []

    async def lookup(place):
        tried.append(place)
        return None

    monkeypatch.setattr(orchestrator.weather_source, "lookup", lookup)
    wired.classification = {"answer_shape": "weather", "place": "Reutlingen"}

    stream = RunStream(redis, "w5", ttl=60)
    await orchestrator.run(stream, request(query="wie ist das wetter in Reutlingen"))

    _events, blocks, done = await collect(stream)
    assert done["status"] == "completed"
    assert "sources" in blocks and "text" in blocks
    assert done["stats"]["searches"] >= 1
    # The classifier names the same place the pre-router did; asking a service
    # that has just declined it costs a round-trip and cannot succeed.
    assert tried == ["Reutlingen"]
    footer = next(b for b in await stream.snapshot() if b["type"] == "provenance")
    assert footer["data"]["depth"] == "balanced"  # widened back, not left on `simple`
    # Who wrote and who checked, so a model judging its own answer is visible.
    assert set(footer["data"]["models"]) == {"writer", "judge"}


async def test_the_same_place_written_differently_is_not_asked_twice(redis, wired, monkeypatch):
    """The pre-router lifts the name out of the raw question, the classifier
    writes it properly: "reutlingen" and "Reutlingen" are one question for a
    geocoder, and the reader watched the refusal scroll past twice."""
    tried = []

    async def lookup(place):
        tried.append(place)
        return None

    monkeypatch.setattr(orchestrator.weather_source, "lookup", lookup)
    wired.classification = {"answer_shape": "weather", "place": "Reutlingen"}

    stream = RunStream(redis, "w8", ttl=60)
    await orchestrator.run(stream, request(query="wie wird das wetter morgen in reutlingen?"))

    _events, _blocks, done = await collect(stream)
    assert done["status"] == "completed"
    assert tried == ["reutlingen"]
    research_block = next(b for b in await stream.snapshot() if b["type"] == "research")
    refusals = [s for s in research_block["data"]["sub_steps"] if "No weather service" in s["text"]]
    assert len(refusals) == 1


async def test_a_place_the_classifier_finds_is_still_tried_after_a_bare_question(
    redis, wired, monkeypatch
):
    """The pre-router had nothing to look up; the classifier resolved the place
    from the conversation. That is a different question, so it gets asked."""
    tried = []

    async def lookup(place):
        tried.append(place)
        return weather_report()

    monkeypatch.setattr(orchestrator.weather_source, "lookup", lookup)
    wired.classification = {"answer_shape": "weather", "place": "Reutlingen"}

    stream = RunStream(redis, "w7", ttl=60)
    await orchestrator.run(stream, request(query="und wie ist das wetter dort"))

    _events, blocks, done = await collect(stream)
    assert tried == ["Reutlingen"]
    assert "weather" in blocks and done["status"] == "completed"


async def test_small_talk_costs_one_call_and_no_search(redis, wired, no_search):
    stream = RunStream(redis, "w6", ttl=60)
    await orchestrator.run(stream, request(query="danke!"))

    _events, blocks, done = await collect(stream)
    assert done["status"] == "completed"
    assert done["stats"]["llm_calls"] == 1
    assert "sources" not in blocks
    assert wired.calls == ["stream"]  # the classifier never ran


# ------------------------------------------------------------------- routing


async def test_a_simple_question_on_auto_runs_the_cheap_preset(redis, monkeypatch, wired):
    wired.classification = {"complexity": "simple"}

    stream = RunStream(redis, "d1", ttl=60)
    await orchestrator.run(stream, request(depth="auto"))

    _events, _blocks, done = await collect(stream)
    footer = next(b for b in await stream.snapshot() if b["type"] == "provenance")
    assert footer["data"]["depth"] == "simple"
    assert footer["data"]["requested_depth"] == "auto"
    # Two, not five: `fetch_pages=1` is spent twice over, once on the
    # best-ranked result before the passages exist, and once topping up a source
    # that reached the passage set on its snippet alone. "Simple" means one good
    # source settles it, and a source that is never opened settles nothing.
    assert done["stats"]["pages_fetched"] == 2


async def test_a_research_question_on_auto_plans_sub_questions(redis, wired):
    wired.classification = {"complexity": "research"}

    stream = RunStream(redis, "d2", ttl=60)
    await orchestrator.run(stream, request(depth="auto"))

    _events, blocks, _done = await collect(stream)
    assert "plan" in blocks and blocks.count("sub_question") == 2
    # Finished, so it folds away: the sub-questions below it carry the same
    # list with the answers attached.
    plan = next(b for b in await stream.snapshot() if b["type"] == "plan")
    assert plan["data"]["status"] == "done"


async def test_the_footer_says_what_was_asked_for_and_what_ran(redis, wired):
    """A run that quietly did less than it was asked to is the thing that would
    make routing untrustworthy, so a preset the reader chose is what runs, and
    the footer says both halves either way."""
    wired.classification = {"complexity": "simple"}

    stream = RunStream(redis, "d3", ttl=60)
    await orchestrator.run(stream, request(depth="deep"))

    footer = next(b for b in await stream.snapshot() if b["type"] == "provenance")
    assert footer["data"]["requested_depth"] == "deep"
    assert footer["data"]["depth"] == "deep"
    assert "as chosen" in footer["data"]["route_reason"]


async def test_a_recency_window_still_applies_to_an_ordinary_question(redis, wired, monkeypatch):
    windows = []

    async def search_all(queries, *, limit_per_query=8, lenses=None, time_range=""):
        windows.append(time_range)
        return fake_results(), fake_report(len(queries), time_range=time_range)

    monkeypatch.setattr(research, "search_all", search_all)
    wired.classification = {"recency": "week"}

    stream = RunStream(redis, "d4", ttl=60)
    await orchestrator.run(stream, request())

    await collect(stream)
    assert windows == ["week"]


async def test_who_is_x_is_not_restricted_to_recent_news(redis, wired, monkeypatch):
    """A profile is a question about a person, not about the news. Searching only
    the recent past leaves the card with nothing to state but this month's
    headlines.

    Pinned with a narrow window: "year" does not filter anything for
    any shape, so it would pass here without the timeless rule doing any work."""
    windows = []

    async def search_all(queries, *, limit_per_query=8, lenses=None, time_range=""):
        windows.append(time_range)
        return fake_results(), fake_report(len(queries), time_range=time_range)

    monkeypatch.setattr(research, "search_all", search_all)
    wired.classification = {"answer_shape": "profile", "wants_image": True, "recency": "month"}

    stream = RunStream(redis, "d5", ttl=60)
    await orchestrator.run(stream, request())

    await collect(stream)
    # Two searches (the evidence and the portrait), and neither is windowed.
    assert windows == ["", ""]
    # And the step the reader sees agrees with the search that actually ran.
    research_block = next(b for b in await stream.snapshot() if b["type"] == "research")
    assert not any("restricting" in s["text"] for s in research_block["data"]["sub_steps"])


async def test_a_project_answer_is_numbered_like_any_other_source(redis, wired):
    """The point of a project: what it already answered is retrieved, ranked
    against the web results and *cited*. An assistant that quietly remembers is
    one the reader cannot check."""
    earlier = SearchResult(
        title="How XAUTOCLAIM works",
        url="/#chat=c1",
        snippet="XAUTOCLAIM transfers ownership of pending entries.",
        content="XAUTOCLAIM transfers ownership of pending entries.",
        engines=["project"],
    )
    earlier.tier = "project"

    stream = RunStream(redis, "p1", ttl=60)
    await orchestrator.run(stream, request(project_sources=[earlier]))

    sources = next(b for b in await stream.snapshot() if b["type"] == "sources")
    mine = [s for s in sources["data"]["sources"] if s["url"] == "/#chat=c1"]
    assert len(mine) == 1
    assert mine[0]["tier"] == "project"
    # Once, not once per sub-question: a citation number that means two things
    # is worse than a missing one.
    footer = next(b for b in await stream.snapshot() if b["type"] == "provenance")
    assert footer["data"]["sources"]["tiers"].get("project") == 1


async def test_a_run_without_a_project_is_unchanged(redis, wired):
    stream = RunStream(redis, "p2", ttl=60)
    await orchestrator.run(stream, request())

    sources = next(b for b in await stream.snapshot() if b["type"] == "sources")
    assert all(not s["url"].startswith("/#chat=") for s in sources["data"]["sources"])


async def test_an_answer_cut_off_at_the_ceiling_is_continued(redis, wired, monkeypatch):
    """`finish_reason: length` is the provider saying there was more. Left
    alone it reaches the reader as a sentence that stops mid-word, under a Copy
    button and a footer, which reads as a finished answer."""
    parts = ["The answer runs to the ceiling and stops mid-", "sentence, then finishes properly."]
    streams: list[int] = []

    async def stream(self, messages, *, max_tokens=None, temperature=0.3):
        index = len(streams)
        streams.append(index)
        for word in parts[min(index, len(parts) - 1)].split(" "):
            yield Delta(content=word + " ")
        # Truncated on the first call, complete on the second.
        self.last_finish_reason = "length" if index == 0 else "stop"

    monkeypatch.setattr(FakeLLM, "stream", stream, raising=False)
    wired.last_finish_reason = ""

    stream_obj = RunStream(redis, "t1", ttl=60)
    await orchestrator.run(stream_obj, request())

    text = next(b for b in await stream_obj.snapshot() if b["type"] == "text")
    assert "stops mid- sentence," in text["data"]["content"]
    assert len(streams) == 2  # the answer and one continuation, never a third
    # Finished in the end, so nothing to warn about.
    assert not [b for b in await stream_obj.snapshot() if b["type"] == "notice"]


async def test_an_answer_that_stays_truncated_says_so(redis, wired, monkeypatch):
    """One continuation, not a loop. When it still does not finish, the
    reader is told rather than left with a sentence that stops."""
    calls: list[int] = []

    async def stream(self, messages, *, max_tokens=None, temperature=0.3):
        calls.append(1)
        yield Delta(content="Endless prose that never concludes ")
        self.last_finish_reason = "length"

    monkeypatch.setattr(FakeLLM, "stream", stream, raising=False)
    wired.last_finish_reason = ""

    stream_obj = RunStream(redis, "t2", ttl=60)
    await orchestrator.run(stream_obj, request())

    assert len(calls) == 2
    notices = [b for b in await stream_obj.snapshot() if b["type"] == "notice"]
    assert any("length limit" in b["data"]["text"] for b in notices)


async def test_a_picture_search_says_which_way_it_came_up_empty(redis, wired, monkeypatch):
    """ "No usable picture was found" was true of an instance with no image
    engines left, of a query that matched nothing, and of results whose pictures
    a URL check had thrown away: three repairs behind one sentence, and no way
    to tell from the outside which one was needed."""

    async def search_all(queries, *, limit_per_query=8, lenses=None, time_range=""):
        # The image lens answers, with results that carry no picture at all.
        if lenses and lenses[0].id == "images":
            return fake_results(2), fake_report(1)
        return fake_results(), fake_report(len(queries))

    monkeypatch.setattr(research, "search_all", search_all)
    wired.classification = {"answer_shape": "profile", "wants_image": True}

    stream = RunStream(redis, "d6", ttl=60)
    await orchestrator.run(stream, request())
    await collect(stream)

    research_block = next(b for b in await stream.snapshot() if b["type"] == "research")
    steps = [s["text"] for s in research_block["data"]["sub_steps"]]
    assert any("none carrying a usable picture" in text for text in steps), steps


async def test_a_picture_search_that_no_engine_answered_says_so(redis, wired, monkeypatch):
    async def search_all(queries, *, limit_per_query=8, lenses=None, time_range=""):
        if lenses and lenses[0].id == "images":
            return [], fake_report(1, unresponsive={"bing images": "CAPTCHA"})
        return fake_results(), fake_report(len(queries))

    monkeypatch.setattr(research, "search_all", search_all)
    wired.classification = {"answer_shape": "profile", "wants_image": True}

    stream = RunStream(redis, "d7", ttl=60)
    await orchestrator.run(stream, request())
    await collect(stream)

    research_block = next(b for b in await stream.snapshot() if b["type"] == "research")
    steps = [s["text"] for s in research_block["data"]["sub_steps"]]
    assert any("bing images" in text for text in steps), steps


def picture_results(count: int = 2) -> list[SearchResult]:
    results = [
        SearchResult(
            title=f"Okapi {i}",
            url=f"https://commons.test/okapi-{i}",
            snippet="",
            image=f"https://commons.test/okapi-{i}.jpg",
            engines=["fake images"],
        )
        for i in range(1, count + 1)
    ]
    for result in results:
        result.assess()
    return results


@pytest.fixture
def pictured(monkeypatch):
    """Search that answers the image lens with pictures, and records what each
    lens was asked. The evidence carries a thumbnail of its own, which must
    never be the picture shown."""
    asked: dict[str, list[str]] = {"images": [], "web": []}

    async def search_all(queries, *, limit_per_query=8, lenses=None, time_range=""):
        if lenses and lenses[0].id == "images":
            asked["images"].extend(queries)
            return picture_results(), fake_report(1)
        asked["web"].extend(queries)
        results = fake_results()
        results[0].image = "https://example1.test/banner.jpg"
        return results, fake_report(len(queries))

    monkeypatch.setattr(research, "search_all", search_all)
    return asked


async def test_a_prose_answer_that_wants_a_picture_shows_the_one_it_found(redis, wired, pictured):
    """Observed: the image search ran beside a prose answer, the transcript said
    "Found 2 pictures", and nothing rendered them, because only a card could."""
    wired.classification = {
        "wants_image": True,
        "image_query": "okapi",
        "standalone_question": "what does an okapi look like",
    }

    stream = RunStream(redis, "im1", ttl=60)
    await orchestrator.run(stream, request())

    _events, emitted, done = await collect(stream)
    assert done["status"] == "completed"
    assert emitted.index("image") < emitted.index("text")
    image = next(b for b in await stream.snapshot() if b["type"] == "image")
    # The picture search's first result, not the evidence page's banner.
    assert image["data"]["url"] == "https://commons.test/okapi-1.jpg"
    # And no model was asked to choose it.
    assert not any(call.startswith("Picture") for call in wired.calls)


async def test_the_picture_search_asks_for_the_subject_not_the_web_query(redis, wired, pictured):
    wired.classification = {
        "wants_image": True,
        "image_query": "okapi",
        "search_queries": ["okapi lebensraum verbreitung"],
    }

    stream = RunStream(redis, "im2", ttl=60)
    await orchestrator.run(stream, request())
    await collect(stream)

    assert pictured["images"] == ["okapi"]


async def test_a_prose_answer_that_wants_no_picture_runs_no_picture_search(redis, wired, pictured):
    stream = RunStream(redis, "im3", ttl=60)
    await orchestrator.run(stream, request())

    _events, emitted, _done = await collect(stream)
    assert pictured["images"] == []
    assert "image" not in emitted


async def test_wanting_a_picture_overrules_answering_from_memory(redis, wired, pictured):
    """The two fields contradict each other often enough: "wie sieht ein Okapi
    aus" came back as no search needed, and a direct answer has no picture."""
    wired.classification = {
        "needs_search": False,
        "wants_image": True,
        "image_query": "okapi",
        "standalone_question": "wie sieht ein okapi aus",
    }

    stream = RunStream(redis, "im4", ttl=60)
    await orchestrator.run(stream, request(query="wie sieht ein okapi aus"))

    _events, emitted, _done = await collect(stream)
    assert pictured["images"] == ["okapi"]
    assert "image" in emitted


async def test_a_picture_asked_for_by_name_is_searched_when_the_classifier_misses_it(
    redis, wired, pictured
):
    wired.classification = {"standalone_question": "zeig mir ein Bild von einem Okapi"}
    stream = RunStream(redis, "im5", ttl=60)
    await orchestrator.run(stream, request(query="zeig mir ein Bild von einem Okapi"))

    _events, emitted, _done = await collect(stream)
    assert pictured["images"], "the picture search never ran"
    assert "image" in emitted


async def test_a_failed_classifier_still_honours_a_picture_asked_for_by_name(
    redis, wired, pictured
):
    class ClassifierDown(FakeLLM):
        async def structured(self, messages, schema, **kwargs):
            if schema is orchestrator.Classification:
                raise LLMError("router is down")
            return await super().structured(messages, schema, **kwargs)

    wired.__class__ = ClassifierDown
    stream = RunStream(redis, "im6", ttl=60)
    await orchestrator.run(stream, request(query="show me a picture of an okapi"))

    _events, emitted, done = await collect(stream)
    assert done["status"] == "completed"
    assert "image" in emitted


async def test_a_slow_card_is_built_beside_the_answer_and_still_lands_above_it(
    redis, wired, monkeypatch
):
    """The card must not sit in series in front of the writer: on a reasoning
    model that puts fifteen seconds between the reader and the first word.
    Both properties matter: the overlap, and the order the reader sees."""
    timeline = []

    async def slow_emit(ctx, shape, question, passages, **kwargs):
        timeline.append("widget starts")
        await asyncio.sleep(0.05)
        await ctx.stream.emit_block(agent_blocks.block(shape, {"name": "Ada Lovelace"}))
        timeline.append("widget done")
        return True

    original_stream = FakeLLM.stream

    async def watched_stream(self, *args, **kwargs):
        timeline.append("writer starts")
        async for delta in original_stream(self, *args, **kwargs):
            yield delta

    monkeypatch.setattr(orchestrator.widgets, "emit", slow_emit)
    monkeypatch.setattr(FakeLLM, "stream", watched_stream)
    wired.classification = {"answer_shape": "profile"}

    stream = RunStream(redis, "p1", ttl=60)
    await orchestrator.run(stream, request())

    _events, emitted, done = await collect(stream)
    assert done["status"] == "completed"
    assert emitted.index("profile") < emitted.index("text")
    assert timeline.index("writer starts") < timeline.index("widget done")


async def test_a_card_that_needs_the_writers_llm_slot_does_not_deadlock(redis, wired, monkeypatch):
    """The real client holds a concurrency slot for as long as a stream is
    read, and a card is a call of its own. Waiting for the card *inside* the
    stream meant that with every slot held by a writer, no card could start and
    no writer could finish. One slot is that situation in miniature: the card
    can only be built once the writer has let go."""
    gate = asyncio.Semaphore(1)
    original_stream = FakeLLM.stream

    async def gated_stream(self, *args, **kwargs):
        async with gate:
            async for delta in original_stream(self, *args, **kwargs):
                await asyncio.sleep(0.005)
                yield delta

    async def gated_emit(ctx, shape, question, passages, **kwargs):
        # A card call takes longer to get going than the writer's, which is the
        # order the deadlock needs: the writer already streaming, holding the slot.
        await asyncio.sleep(0.01)
        async with gate:
            await ctx.stream.emit_block(agent_blocks.block(shape, {"name": "Ada Lovelace"}))
        return True

    monkeypatch.setattr(orchestrator.widgets, "emit", gated_emit)
    monkeypatch.setattr(FakeLLM, "stream", gated_stream)
    wired.classification = {"answer_shape": "profile"}
    wired.answer = "Ada wrote the first program [1]."

    stream = RunStream(redis, "p1", ttl=60)
    await asyncio.wait_for(orchestrator.run(stream, request()), timeout=5)

    _events, emitted, done = await collect(stream)
    assert done["status"] == "completed"
    assert emitted.index("profile") < emitted.index("text")
    text = next(b for b in await stream.snapshot() if b["type"] == "text")
    # Held back while the card was pending, and none of it lost on the way.
    assert text["data"]["content"].split() == wired.answer.split()


async def test_a_widget_that_raises_costs_itself_and_not_the_answer(redis, wired, monkeypatch):
    async def boom(ctx, shape, question, passages, **kwargs):
        raise RuntimeError("the schema call exploded")

    monkeypatch.setattr(orchestrator.widgets, "emit", boom)
    wired.classification = {"answer_shape": "profile"}

    stream = RunStream(redis, "p2", ttl=60)
    await orchestrator.run(stream, request())

    _events, emitted, done = await collect(stream)
    assert done["status"] == "completed" and "text" in emitted
    assert "profile" not in emitted


async def test_an_explanation_borrows_the_diagram_and_is_quiet_when_it_is_refused(
    redis, wired, monkeypatch
):
    """The explainer is not a diagram answer: it asks for the picture because
    one helps somebody learning the subject, and its prose is streamed without
    knowing whether it arrived. So the request is made against the diagram
    schema, and a refusal says nothing, where a diagram *answer* owes the reader
    the reason the picture its transcript promised is missing."""
    asked = []

    async def watch(ctx, shape, question, passages, *, announce=True):
        asked.append((shape, announce))
        return False

    monkeypatch.setattr(orchestrator.widgets, "emit", watch)
    wired.classification = {"answer_shape": "explainer"}

    stream = RunStream(redis, "x1", ttl=60)
    await orchestrator.run(stream, request())

    _events, emitted, done = await collect(stream)
    assert done["status"] == "completed" and "text" in emitted
    assert asked == [("diagram", False)]
    # Nothing was promised, so nothing is apologised for.
    notices = [b for b in await stream.snapshot() if b["type"] == "notice"]
    assert notices == []


async def test_a_diagram_that_cannot_be_drawn_still_says_so_on_a_diagram_answer(
    redis, wired, monkeypatch
):
    """The other half of the rule above: silence is granted to the borrower, not
    to the shape whose whole answer was going to be the picture."""
    monkeypatch.setattr(
        widgets.Diagram,
        "payload",
        lambda self, evidence, kind: (
            None,
            "The sources describe the parts but not how they connect.",
        ),
    )
    wired.classification = {"answer_shape": "diagram"}

    stream = RunStream(redis, "x2", ttl=60)
    await orchestrator.run(stream, request())
    await collect(stream)

    notices = [b["data"]["text"] for b in await stream.snapshot() if b["type"] == "notice"]
    assert any("not how they connect" in text for text in notices), notices


async def test_a_prose_answer_with_facets_composes_an_overview_quietly(redis, wired, monkeypatch):
    """The space between the fixed shapes and bare prose: the classifier names
    the parts of the answer that are structure, and the panel is composed from
    those alone. Like the explainer's diagram, nothing promised it, so a
    refusal says nothing and the prose never waits on it being there."""
    asked = []

    async def watch(ctx, shape, question, passages, *, announce=True):
        asked.append((shape, announce, ctx.facets))
        return False

    monkeypatch.setattr(orchestrator.widgets, "emit", watch)
    wired.classification = {"facets": ["timeline", "mindmap", "proscons"]}

    stream = RunStream(redis, "o1", ttl=60)
    await orchestrator.run(stream, request())

    _events, emitted, done = await collect(stream)
    assert done["status"] == "completed" and "text" in emitted
    # The improvised block kind is gone before any call is made.
    assert asked == [("overview", False, ("timeline", "proscons"))]
    notices = [b for b in await stream.snapshot() if b["type"] == "notice"]
    assert notices == []


async def test_a_prose_answer_without_facets_costs_no_panel_call(redis, wired, monkeypatch):
    """Most answers are one explanation or one argument, and for those the
    overview must not exist at all, not even as a refused call."""
    asked = []

    async def watch(ctx, shape, question, passages, **kwargs):
        asked.append(shape)
        return False

    monkeypatch.setattr(orchestrator.widgets, "emit", watch)
    wired.classification = {}

    stream = RunStream(redis, "o2", ttl=60)
    await orchestrator.run(stream, request())
    await collect(stream)
    assert asked == []


# --------------------------------------------------------------- second look


async def test_a_half_answered_question_is_searched_again_with_what_was_found(
    redis, wired, monkeypatch
):
    """The blind spot this closes: every query in a run is written before a
    single source has been seen. Asked what DiCaprio's parents did, the run
    searched his name, read what came back and reported that the sources do not
    say, with the answer sitting in a page it had never opened."""
    asked = []

    async def search_all(queries, *, limit_per_query=8, lenses=None, time_range=""):
        asked.append(list(queries))
        return fake_results(), fake_report(len(queries))

    monkeypatch.setattr(research, "search_all", search_all)
    wired.gap = {"sufficient": False, "follow_up_questions": ["George DiCaprio Beruf"]}

    stream = RunStream(redis, "g1", ttl=60)
    await orchestrator.run(stream, request(depth="balanced"))

    _events, _blocks, done = await collect(stream)
    assert done["status"] == "completed"
    # The follow-up is a query the first round could not have written.
    assert asked[-1] == ["George DiCaprio Beruf"]
    research_block = next(b for b in await stream.snapshot() if b["type"] == "research")
    assert any("Missing:" in step["text"] for step in research_block["data"]["sub_steps"])


async def test_evidence_that_covers_the_question_costs_one_call_and_no_search(
    redis, wired, monkeypatch
):
    searches = []

    async def search_all(queries, *, limit_per_query=8, lenses=None, time_range=""):
        searches.append(list(queries))
        return fake_results(), fake_report(len(queries))

    monkeypatch.setattr(research, "search_all", search_all)

    stream = RunStream(redis, "g2", ttl=60)
    await orchestrator.run(stream, request(depth="balanced"))

    await collect(stream)
    assert len(searches) == 1  # the check said the sources cover it
    assert wired.calls.count("GapCheck") == 1
    research_block = next(b for b in await stream.snapshot() if b["type"] == "research")
    assert any("cover the question" in step["text"] for step in research_block["data"]["sub_steps"])


async def test_a_follow_up_that_finds_nothing_new_is_not_a_failed_search(redis, wired, monkeypatch):
    """ "Nothing was found in Web. Widening the sources may help." Under twenty
    sources. The follow-up round routinely registers nothing, because the source
    list is capped and the pages it finds are already in it."""

    async def search_all(queries, *, limit_per_query=8, lenses=None, time_range=""):
        return fake_results(), fake_report(len(queries))

    monkeypatch.setattr(research, "search_all", search_all)
    wired.gap = {"sufficient": False, "follow_up_questions": ["something else entirely"]}

    stream = RunStream(redis, "g5", ttl=60)
    await orchestrator.run(stream, request(depth="balanced"))

    _events, _blocks, done = await collect(stream)
    assert done["status"] == "completed"
    # The second search returns the same three pages, so it registers none.
    notices = [b for b in await stream.snapshot() if b["type"] == "notice"]
    assert not [n for n in notices if "Nothing was found" in n["data"]["text"]], notices


async def test_quick_stays_one_shot_because_that_is_what_it_is_for(redis, wired):
    stream = RunStream(redis, "g3", ttl=60)
    await orchestrator.run(stream, request(depth="quick"))

    await collect(stream)
    assert "GapCheck" not in wired.calls


async def test_the_routed_preset_gets_the_second_look_and_can_pay_for_it(redis, wired):
    """Where the router sends "what did X's parents do": one query, six results,
    and an answer no first query could have named. Its call budget has to have
    room for the check, or `can_research` reserves the last call for the writer
    and it can never run."""
    wired.classification = {"complexity": "simple"}
    wired.gap = {"sufficient": False, "follow_up_questions": ["George DiCaprio Beruf"]}

    stream = RunStream(redis, "g4", ttl=60)
    await orchestrator.run(stream, request(depth="auto"))

    _events, _blocks, done = await collect(stream)
    footer = next(b for b in await stream.snapshot() if b["type"] == "provenance")
    assert footer["data"]["depth"] == "simple"
    assert "GapCheck" in wired.calls
    assert done["stats"]["searches"] >= 2
    assert done["status"] == "completed" and "text" in _blocks


# ------------------------------------------------------------------- corpora


async def test_on_auto_the_classifier_picks_the_corpora(redis, wired):
    wired.classification = {"lenses": ["scholar", "news"]}

    stream = RunStream(redis, "l1", ttl=60)
    await orchestrator.run(stream, request(lenses=["auto"]))

    await collect(stream)
    footer = next(b for b in await stream.snapshot() if b["type"] == "provenance")
    assert [lens["id"] for lens in footer["data"]["lenses"]] == ["scholar", "news"]
    # Who chose them is part of the claim.
    assert footer["data"]["lenses_auto"] is True
    research_block = next(b for b in await stream.snapshot() if b["type"] == "research")
    assert any("Science" in step["text"] for step in research_block["data"]["sub_steps"])


async def test_a_selection_the_reader_made_is_never_overridden(redis, wired):
    """The model is asked only when the reader said it should be."""
    wired.classification = {"lenses": ["scholar", "news"]}

    stream = RunStream(redis, "l2", ttl=60)
    await orchestrator.run(stream, request(lenses=["web"]))

    await collect(stream)
    footer = next(b for b in await stream.snapshot() if b["type"] == "provenance")
    assert [lens["id"] for lens in footer["data"]["lenses"]] == ["web"]
    assert footer["data"]["lenses_auto"] is False


async def test_an_auto_run_whose_model_names_nothing_usable_still_searches(redis, wired):
    wired.classification = {"lenses": ["astrology", "auto"]}

    stream = RunStream(redis, "l3", ttl=60)
    await orchestrator.run(stream, request(lenses=["auto"]))

    _events, _blocks, done = await collect(stream)
    assert done["status"] == "completed"
    footer = next(b for b in await stream.snapshot() if b["type"] == "provenance")
    assert [lens["id"] for lens in footer["data"]["lenses"]] == ["web"]


# ------------------------------------------------------------------- recency
# The bug these pin: `recency` was one setting doing two jobs, so "this question
# is about something current" became a hard one-year filter at the engine, and
# an answer that lived in a 2005 paper was never retrieved to be ranked at all.


async def test_a_year_prefers_recent_sources_without_filtering_any_out(redis, wired, monkeypatch):
    seen: list[str] = []
    # The fixture has already replaced this with its fake; wrap that, not the
    # real one, so the run still gets its canned results.
    inner = research.search_all

    async def spy(queries, *, limit_per_query=8, lenses=None, time_range=""):
        seen.append(time_range)
        return await inner(queries, limit_per_query=limit_per_query, lenses=lenses)

    monkeypatch.setattr(research, "search_all", spy)
    wired.classification = {"recency": "year"}

    stream = RunStream(redis, "r1", ttl=60)
    await orchestrator.run(stream, request())

    await collect(stream)
    assert seen and all(window == "" for window in seen)
    footer = next(b for b in await stream.snapshot() if b["type"] == "provenance")
    assert footer["data"]["recency"] == ""
    assert footer["data"]["prefer_recent"] is True


async def test_a_narrow_window_is_both_a_restriction_and_a_preference(redis, wired, monkeypatch):
    """A breaking-news question is the case the filter exists for: every page of
    the past thirteen months scores the same freshness, so only the engine can
    tell yesterday's report from last spring's. The footer has to carry both
    claims, because they are separately falsifiable."""
    seen: list[str] = []
    # The fixture has already replaced this with its fake; wrap that, not the
    # real one, so the run still gets its canned results.
    inner = research.search_all

    async def spy(queries, *, limit_per_query=8, lenses=None, time_range=""):
        seen.append(time_range)
        return await inner(queries, limit_per_query=limit_per_query, lenses=lenses)

    monkeypatch.setattr(research, "search_all", spy)
    wired.classification = {"recency": "week"}

    stream = RunStream(redis, "r2", ttl=60)
    await orchestrator.run(stream, request())

    await collect(stream)
    assert seen and all(window == "week" for window in seen)
    footer = next(b for b in await stream.snapshot() if b["type"] == "provenance")
    assert footer["data"]["recency"] == "week"
    assert footer["data"]["prefer_recent"] is True


# --------------------------------------------------------- the citation check


async def test_the_answer_is_checked_against_the_sources_it_cites(redis, wired):
    stream = RunStream(redis, "v1", ttl=60)
    await orchestrator.run(stream, request())

    _events, blocks, done = await collect(stream)
    assert done["status"] == "completed"
    # After the prose, because it is a statement about the prose.
    assert blocks.index("text") < blocks.index("verification")
    check = next(b for b in await stream.snapshot() if b["type"] == "verification")
    assert check["data"]["total"] == 1
    assert check["data"]["checked"] == 1
    assert check["data"]["supported"] == 1
    assert check["data"]["unchecked"] == 0
    assert check["data"]["claims"][0]["sources"] == [1]


async def test_a_claim_no_verdict_came_back_for_is_reported_as_unchecked(redis, wired):
    """The check's own gaps are the block's business too. Counting them as
    supported is how a truncated verdict list becomes a clean bill of health."""
    wired.answer = "An answer citing [1]. A second sentence, also citing [1]."
    wired.verdicts = [verify.Verdict(claim=1, verdict="supported")]

    stream = RunStream(redis, "v5", ttl=60)
    await orchestrator.run(stream, request())

    await collect(stream)
    check = next(b for b in await stream.snapshot() if b["type"] == "verification")
    assert check["data"]["total"] == 2
    assert check["data"]["checked"] == 1
    assert check["data"]["unchecked"] == 1
    assert check["data"]["claims"][1]["verdict"] == verify.UNCHECKED


async def test_a_check_with_no_budget_left_says_so_instead_of_vanishing(redis, wired, monkeypatch):
    """An absent block reads exactly like a check that ran and found nothing
    wrong: the flattering reading, which is why it must not be available."""
    monkeypatch.setattr(budget_module.Budget, "can_verify", lambda self: False)

    stream = RunStream(redis, "v6", ttl=60)
    await orchestrator.run(stream, request())

    _events, _blocks, done = await collect(stream)
    assert done["status"] == "completed"
    check = next(b for b in await stream.snapshot() if b["type"] == "verification")
    assert check["data"]["status"] == agent_blocks.STATUS_SKIPPED
    assert check["data"]["checked"] == 0


async def test_a_claim_its_source_does_not_carry_is_reported_as_such(redis, wired):
    wired.verdicts = [verify.Verdict(claim=1, verdict="unsupported", note="the page says nothing")]

    stream = RunStream(redis, "v2", ttl=60)
    await orchestrator.run(stream, request())

    await collect(stream)
    check = next(b for b in await stream.snapshot() if b["type"] == "verification")
    assert check["data"]["unsupported"] == 1
    assert check["data"]["claims"][0]["note"] == "the page says nothing"


async def test_a_failed_check_costs_the_check_and_nothing_else(redis, wired, monkeypatch):
    """The answer is already streamed and on screen by the time this runs, so
    there is no failure here worth showing the reader."""

    async def explode(*_args, **_kwargs):
        raise RuntimeError("the check fell over")

    monkeypatch.setattr(verify, "check", explode)

    stream = RunStream(redis, "v3", ttl=60)
    await orchestrator.run(stream, request())

    _events, blocks, done = await collect(stream)
    assert done["status"] == "completed"
    assert "text" in blocks
    assert "verification" not in blocks


async def test_an_answer_that_cites_nothing_is_not_given_an_empty_verdict(redis, wired):
    wired.answer = "An answer with no citations at all."

    stream = RunStream(redis, "v4", ttl=60)
    await orchestrator.run(stream, request())

    _events, blocks, done = await collect(stream)
    assert done["status"] == "completed"
    assert "verification" not in blocks


# ---------------------------------------------------------------- degradation


async def test_a_dead_search_still_produces_an_answer_and_says_so(redis, wired, monkeypatch):
    async def nothing(queries, *, limit_per_query=8, lenses=None, time_range=""):
        return [], fake_report(len(queries), failures=["searxng: not reachable"])

    monkeypatch.setattr(research, "search_all", nothing)

    stream = RunStream(redis, "r4", ttl=60)
    await orchestrator.run(stream, request())

    _events, blocks, done = await collect(stream)
    assert done["status"] == "completed"
    assert "notice" in blocks  # the user is told the answer is unsourced
    assert "text" in blocks


async def test_provider_failure_ends_in_an_error_event_not_silence(redis, wired):
    wired.fail = LLMError("the provider rejected the API key")

    stream = RunStream(redis, "r5", ttl=60)
    await orchestrator.run(stream, request())

    _events, _blocks, done = await collect(stream)
    assert done["status"] == "error"
    assert "API key" in done["message"]


async def test_cancellation_is_reported_as_cancelled_not_as_a_failure(redis, wired):
    stream = RunStream(redis, "r6", ttl=60)
    await stream.set_state(cancel="1")

    await orchestrator.run(stream, request())

    _events, _blocks, done = await collect(stream)
    assert done["status"] == "cancelled"


# ---------------------------------------------------------------------- deep


async def test_deep_depth_plans_and_researches_sub_questions(redis, wired):
    stream = RunStream(redis, "r7", ttl=60)
    await orchestrator.run(stream, request(depth="deep"))

    _events, blocks, done = await collect(stream)

    assert done["status"] == "completed"
    assert blocks.count("sub_question") == 2
    assert blocks.index("plan") < blocks.index("sub_question")
    assert "Plan" in wired.calls and "GapCheck" in wired.calls


def test_a_sub_question_is_searched_with_the_query_the_planner_wrote():
    """The bug this pins, from a stored run: "Which ranking Russian officials […]
    have been assassinated […] since 2025" was reduced to its first ten words and
    searched as `ranking Russian officials military officers defense industry
    personnel collaborators have`. The engines answered with pages about military
    ranks, the sub-question found nothing, and the report concluded there had
    been no assassinations."""
    sub = deep.SubQuestion(
        question=(
            "Which ranking Russian officials, military officers, defense-industry personnel, "
            "or collaborators have been assassinated in Russian territory since 2025?"
        ),
        query="Russian officials assassinated 2025 Ukraine",
    )
    assert sub.search_query == "Russian officials assassinated 2025 Ukraine"


def test_a_planner_that_writes_no_query_still_gets_its_sub_question_searched():
    """Bare strings included, a model that ignores the schema must degrade to
    the keyword reduction, not to an unsearched sub-question."""
    assert deep.SubQuestion(question="what does xautoclaim do").search_query
    assert deep.SubQuestion.model_validate("what does xautoclaim do").question == (
        "what does xautoclaim do"
    )
    # A "query" that arrived as a whole sentence is reduced rather than sent.
    sentence = deep.SubQuestion(question="q", query=" ".join(f"word{i}" for i in range(30)))
    assert len(sentence.search_query.split()) <= deep.MAX_QUERY_WORDS


async def test_a_sub_question_that_found_nothing_reaches_the_writer_as_a_gap(
    redis, wired, monkeypatch
):
    """Dropped by `_format_findings`, it would reach the writer as nothing at
    all: the report prompt would hold a question about assassinations, evidence about
    refineries, and no sign that anyone had looked, and the model filled the
    hole with "the sources record none of these", which is the one sentence the
    run had no basis for."""

    async def nothing(queries, *, limit_per_query=8, lenses=None, time_range=""):
        return [], fake_report(len(queries))

    monkeypatch.setattr(research, "search_all", nothing)

    stream = RunStream(redis, "r9", ttl=60)
    await orchestrator.run(stream, request(depth="deep"))

    _events, _blocks, done = await collect(stream)
    assert done["status"] == "completed"
    # Every sub-question came back empty, so the run says so where the reader is.
    notices = [b for b in await stream.snapshot() if b["type"] == "notice"]
    assert any("found no usable sources" in n["data"]["text"] for n in notices), notices


def test_the_empty_sub_questions_are_named_and_not_merely_counted():
    """The bug this closes: "1 of 9 sub-questions found no usable sources" sent
    the reader hunting through nine collapsed blocks for the one that came back
    empty, so the warning was unsettling and unactionable at once. Which facet
    is missing is the whole content of the notice."""
    text = deep._unanswered_notice(["Which testbeds are used in evaluations?"], 9)

    assert "1 of 9" in text
    assert "Which testbeds are used in evaluations?" in text


def test_a_plan_that_mostly_failed_names_a_few_and_counts_the_rest():
    """A run where everything came back empty must not paste its whole plan
    into a banner."""
    text = deep._unanswered_notice([f"Question {n}?" for n in range(5)], 9)

    assert "5 of 9" in text
    assert "Question 0?" in text
    assert "and 2 more" in text
    assert "Question 4?" not in text


def test_a_question_that_failed_twice_is_one_unanswered_question():
    """Observed live: the gap round handed back an empty sub-question word for
    word, the identical search came back empty again, and the notice named it
    twice against a total that counted it twice."""
    outcomes = [
        deep.Outcome("Wie gut integrieren sich ORMs?", "", "orm integration"),
        deep.Outcome("Welche Hürden gibt es?", "findings", "huerden"),
        deep.Outcome("Wie gut integrieren sich ORMs", "", "orm integration"),
    ]
    unanswered, asked = deep._unanswered(outcomes)
    assert unanswered == ["Wie gut integrieren sich ORMs?"]
    assert asked == 2


def test_a_retry_that_found_something_answers_the_question():
    outcomes = [deep.Outcome("Q?", "", "first words"), deep.Outcome("Q?", "found it", "better")]
    assert deep._unanswered(outcomes) == ([], 1)


def test_the_gap_round_never_repeats_a_search_already_made():
    """A question already answered is never worth asking again; one that failed
    is, but only with different words."""
    outcomes = [
        deep.Outcome("Answered?", "findings", "answered"),
        deep.Outcome("Empty?", "", "empty words"),
    ]
    follow_ups = [
        deep.SubQuestion(question="Answered", query="other words"),
        deep.SubQuestion(question="Empty?", query="empty words"),
        deep.SubQuestion(question="Empty?", query="better words"),
        deep.SubQuestion(question="New?", query="new"),
    ]
    kept = deep._untried(follow_ups, outcomes)
    assert [(sub.question, sub.query) for sub in kept] == [
        ("Empty?", "better words"),
        ("New?", "new"),
    ]


async def test_the_plan_and_gap_calls_carry_a_token_ceiling(redis, wired, monkeypatch):
    """Without one OpenRouter reserves the model's whole output window against
    the balance and can refuse the call outright, the rule every other
    structured call in the app already follows."""
    ceilings: dict[str, object] = {}
    original = FakeLLM.structured

    async def watch(self, messages, schema, **kwargs):
        ceilings.setdefault(schema.__name__, kwargs.get("max_tokens"))
        return await original(self, messages, schema, **kwargs)

    monkeypatch.setattr(FakeLLM, "structured", watch)
    stream = RunStream(redis, "r10", ttl=60)
    await orchestrator.run(stream, request(depth="deep"))
    await collect(stream)

    assert ceilings.get("Plan") == deep.PLAN_TOKENS
    assert all(ceilings.get(name) for name in ceilings), ceilings


async def test_the_report_prompt_names_what_was_searched_and_not_found(redis, wired, monkeypatch):
    prompts_seen: list[list[dict]] = []
    original = research.write_answer
    real_search = research.search_all
    calls = {"n": 0}

    async def capture(ctx, messages, **kwargs):
        prompts_seen.append(messages)
        return await original(ctx, messages, **kwargs)

    async def half(queries, *, limit_per_query=8, lenses=None, time_range=""):
        """The first sub-question finds sources, the rest find nothing, which is
        the shape of the run that produced the wrong answer."""
        calls["n"] += 1
        if calls["n"] == 1:
            return await real_search(queries, limit_per_query=limit_per_query, lenses=lenses)
        return [], fake_report(len(queries))

    monkeypatch.setattr(research, "write_answer", capture)
    monkeypatch.setattr(research, "search_all", half)

    stream = RunStream(redis, "r10", ttl=60)
    await orchestrator.run(stream, request(depth="deep"))

    await collect(stream)
    report = "\n".join(str(m.get("content", "")) for m in prompts_seen[-1])
    assert "Searched, nothing found" in report


async def test_a_failing_sub_question_does_not_end_the_deep_run(redis, wired, monkeypatch):
    calls = {"n": 0}
    original = research.search_all

    async def flaky(queries, *, limit_per_query=8, lenses=None, time_range=""):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("engine exploded")
        return await original(queries, limit_per_query=limit_per_query, lenses=lenses)

    monkeypatch.setattr(research, "search_all", flaky)

    stream = RunStream(redis, "r8", ttl=60)
    await orchestrator.run(stream, request(depth="deep"))

    _events, _blocks, done = await collect(stream)
    assert done["status"] == "completed"


# -------------------------------------------------------------------- budget


def test_depth_presets_are_ordered_by_cost():
    quick, balanced, deep_ = (depth_module.get(name) for name in ("quick", "balanced", "deep"))
    assert quick.wall_clock_s < balanced.wall_clock_s < deep_.wall_clock_s
    assert quick.max_sources < balanced.max_sources < deep_.max_sources
    assert quick.fetch_pages == 0 and balanced.fetch_pages > 0
    # The routed preset reads exactly one page: fewer than Balanced, more than
    # the nothing that made it answer "the sources do not say" about a fact
    # sitting in the first result.
    assert 0 < depth_module.SIMPLE.fetch_pages < balanced.fetch_pages
    assert deep_.sub_questions > 0 and balanced.sub_questions == 0


def test_unknown_depth_falls_back_instead_of_failing():
    assert depth_module.get("turbo").id == depth_module.DEFAULT_DEPTH
    assert depth_module.get(None).id == depth_module.DEFAULT_DEPTH


async def test_the_footer_keeps_the_ranking_meter_out_of_the_model_token_count(redis):
    """ "456k tokens, $0.081" was a real footer, and the arithmetic does not work:
    almost all of those tokens were free embedding tokens from ranking the source
    list once per sub-question. Two meters in one number make a correct bill look
    like a wrong one."""
    from app.agent.context import RunContext, SourceRegistry
    from app.llm.client import Usage

    depth = depth_module.get("deep")
    stream = RunStream(redis, "p1", ttl=60)
    ctx = RunContext(
        stream=stream,
        llm=FakeLLM(),
        depth=depth,
        budget=budget_module.Budget(depth),
        sources=SourceRegistry(stream, depth.max_sources),
        query="q",
    )
    ctx.llm.usage = Usage(prompt_tokens=7000, completion_tokens=700, cost_usd=0.08, priced=True)
    ctx.embedding_usage = Usage(prompt_tokens=448000, cost_usd=0.0, priced=True)

    stats = ctx.provenance()["stats"]
    assert stats["total_tokens"] == 7700
    assert stats["embedding"]["total_tokens"] == 448000
    # The price is still every meter added up, because a bill is a bill.
    assert stats["cost_usd"] == 0.08


def test_budget_reserves_room_for_the_writer():
    from app.agent.budget import Budget

    budget = Budget(depth_module.get("quick"))
    budget.llm_calls = budget.depth.max_llm_calls - 1
    assert not budget.can_research()  # the last call belongs to the answer
    assert budget.note_exhaustion("research")


def test_an_answer_written_with_the_reserve_may_still_be_continued():
    """The writer spends its reserve on the answer; asking `can_research`
    afterwards refused the continuation of exactly the long answers that hit
    the ceiling. Only the clock decides."""
    from app.agent.budget import Budget

    budget = Budget(depth_module.get("balanced"))
    budget.llm_calls = budget.depth.max_llm_calls
    budget.started_at -= budget.wall_clock_s - 10  # ten seconds left, under the reserve
    assert not budget.can_research()
    assert budget.can_continue()
    budget.started_at -= 20
    assert not budget.can_continue()


# ------------------------------------------------------- fitting the context


def window_ctx(local: bool, answer_tokens: int = 4000):
    from types import SimpleNamespace

    return SimpleNamespace(
        llm=SimpleNamespace(is_local=local),
        depth=SimpleNamespace(answer_tokens=answer_tokens),
    )


def long_passages(count: int = 28):
    from app.embed.rerank import Passage

    return [
        Passage("Ein Abschnitt über Fehlerlokalisierung. " * 30, i, f"Quelle {i}", f"https://s{i}")
        for i in range(1, count + 1)
    ]


def build_probe(kept, compact):
    from app.agent import prompts

    return prompts.report("Frage", kept, "", [], compact=compact)


def test_a_remote_model_is_never_trimmed():
    """A remote window is far larger than anything this app assembles, so
    guessing at it would drop good evidence for nothing."""
    passages = long_passages()

    kept, dropped, answer_tokens, compact = orchestrator._fit_to_window(
        window_ctx(local=False), passages, build_probe
    )

    assert kept == passages
    assert (dropped, answer_tokens, compact) == (0, 4000, False)


def test_a_local_model_gets_evidence_that_fits_its_window(monkeypatch):
    """The bug this closes: 28 passages went to a runtime serving 4096 tokens,
    which truncated the prompt silently and HTTP 200, taking the numbered source
    list with it. The model was then asked to cite a list it had never seen and
    wrote "[Quelle impliziert durch den Kontext]" instead."""
    monkeypatch.setattr(orchestrator.settings, "local_context_tokens", 4096)
    passages = long_passages()

    kept, dropped, answer_tokens, compact = orchestrator._fit_to_window(
        window_ctx(local=True), passages, build_probe
    )

    assert 0 < len(kept) < len(passages), "some evidence, and not all of it"
    assert dropped == len(passages) - len(kept)
    assert compact is True, "a 4k window cannot carry the full ruleset and the sources"
    assert answer_tokens < 4000, "the answer shares the window with its own prompt"

    sent = sum(len(m["content"]) for m in build_probe(kept, compact))
    assert sent // 4 + answer_tokens < 4096, "prompt plus answer must fit the window"


def test_a_large_local_window_is_treated_like_a_remote_one(monkeypatch):
    """Someone who set OLLAMA_CONTEXT_LENGTH=131072 has bought the full
    ruleset back, and must not keep being served the small-model prompt."""
    monkeypatch.setattr(orchestrator.settings, "local_context_tokens", 131_072)

    kept, dropped, _tokens, compact = orchestrator._fit_to_window(
        window_ctx(local=True), long_passages(), build_probe
    )

    assert dropped == 0
    assert len(kept) == 28
    assert compact is False


def test_fitting_keeps_the_best_ranked_passages():
    """Passages arrive ranked, so the cut is taken from the tail."""
    from app.agent import prompts

    passages = long_passages(10)
    kept, dropped = prompts.fit_passages(passages, max_chars=4000)

    assert [p.source_index for p in kept] == list(range(1, len(kept) + 1))
    assert dropped == 10 - len(kept)


def test_earlier_answers_reach_the_model_without_their_citation_numbers():
    """Each run numbers its sources from one, so the history's `[4]` is a
    different page now, and a model shown the old pair repeats it."""
    from app.agent import prompts

    history = [
        {"role": "user", "content": "Wie skaliert man?"},
        {
            "role": "assistant",
            "content": 'Man skaliert [4: "we scale the dot products"] und [2][3]. '
            'Sie [1 quote: "multiply by a root"].',
        },
    ]
    [question, answer] = prompts.trim_history(history)
    assert question["content"] == "Wie skaliert man?"
    assert answer["content"] == "Man skaliert und. Sie."


# ----------------------------------------------------------------- linked pages
#
# A link in the question is the reader naming the page the answer is about. It
# is read whatever the preset, holds citation number 1, reaches the writer ahead
# of every search result, and a question about the page alone is not searched.

ARTICLE = "https://news.example.org/kreatin-studie"
PAGE_TEXT = "The linked article reports that xautoclaim reclaims pending entries. " * 40


@pytest.fixture
def page(monkeypatch):
    """The linked page, readable. `page.urls` records what was asked for."""

    async def read_page(url, *, max_chars=None):
        read_page.urls.append(url)
        return content.Page(text=PAGE_TEXT, title="Linked article", published="2026-10-01")

    read_page.urls = []
    monkeypatch.setattr(linked.content, "read_page", read_page)
    return read_page


@pytest.fixture
def prompts_seen(wired):
    """Every message list the writer was given."""
    seen: list[list[dict]] = []
    original = wired.stream

    def stream(messages, **kwargs):
        seen.append(messages)
        return original(messages, **kwargs)

    wired.stream = stream
    return seen


async def test_the_linked_page_is_source_one_and_reaches_the_writer_first(
    redis, wired, page, prompts_seen
):
    stream = RunStream(redis, "l1", ttl=60)
    await orchestrator.run(stream, request(query=f"was sagt dieser Artikel? {ARTICLE}"))

    _events, _blocks, done = await collect(stream)
    assert done["status"] == "completed"
    assert page.urls == [ARTICLE]

    sources = next(b for b in await stream.snapshot() if b["type"] == "sources")
    first = sources["data"]["sources"][0]
    assert (first["index"], first["url"], first["title"]) == (1, ARTICLE, "Linked article")
    # Searched as well (the fake classifier asks for it), and outside the
    # evidence budget, so the search kept every slot it had.
    assert len(sources["data"]["sources"]) > 1

    user = prompts_seen[0][-1]["content"]
    assert user.index("### [1] Linked article") < user.index("### [2]")
    system = " ".join(prompts_seen[0][0]["content"].split())
    assert "The reader linked a page in the question: source [1]" in system


async def test_a_question_about_the_page_alone_is_not_searched(
    redis, wired, page, prompts_seen, monkeypatch
):
    async def explode(*_args, **_kwargs):
        raise AssertionError("a summary of the linked page must not search")

    monkeypatch.setattr(research, "search_all", explode)
    wired.classification = {"needs_search": False, "search_queries": []}

    stream = RunStream(redis, "l2", ttl=60)
    await orchestrator.run(stream, request(query=f"fasse zusammen: {ARTICLE}"))

    _events, blocks, done = await collect(stream)
    assert done["status"] == "completed"
    assert done["stats"]["searches"] == 0
    assert "sources" in blocks  # answered from the page, and cited to it
    assert "### [1] Linked article" in prompts_seen[0][-1]["content"]


async def test_the_page_is_read_on_quick_too(redis, wired, page):
    """Quick reads no search results. The page the reader pointed at is not one."""
    stream = RunStream(redis, "l3", ttl=60)
    await orchestrator.run(stream, request(query=f"kurz: {ARTICLE}", depth="quick"))

    _events, _blocks, done = await collect(stream)
    assert page.urls == [ARTICLE]
    assert done["stats"]["pages_fetched"] == 1


async def test_an_unreadable_link_is_said_and_searched_instead(redis, wired, monkeypatch):
    async def read_page(url, *, max_chars=None):
        return None

    monkeypatch.setattr(linked.content, "read_page", read_page)
    wired.classification = {"needs_search": False, "search_queries": []}

    stream = RunStream(redis, "l4", ttl=60)
    await orchestrator.run(stream, request(query=f"fasse zusammen: {ARTICLE}"))

    _events, _blocks, done = await collect(stream)
    snapshot = await stream.snapshot()
    notices = [b["data"]["text"] for b in snapshot if b["type"] == "notice"]
    assert any(ARTICLE in text for text in notices)
    assert done["stats"]["searches"] >= 1


async def test_no_query_carries_the_url(redis, wired, page, monkeypatch):
    searched: list[str] = []
    original = research.search_all

    async def search_all(queries, **kwargs):
        searched.extend(queries)
        return await original(queries, **kwargs)

    monkeypatch.setattr(research, "search_all", search_all)
    wired.classification = {"search_queries": [f"kreatin {ARTICLE}", ARTICLE]}

    stream = RunStream(redis, "l5", ttl=60)
    await orchestrator.run(stream, request(query=f"stimmt das? {ARTICLE}"))

    assert searched == ["kreatin"]


async def test_a_link_is_not_small_talk(redis, wired, page):
    stream = RunStream(redis, "l6", ttl=60)
    await orchestrator.run(stream, request(query=f"danke {ARTICLE}"))
    assert page.urls == [ARTICLE]


async def test_a_linked_page_gets_no_stray_picture(redis, wired, page, monkeypatch):
    """Asked "was ist <link>", the classifier picks a profile, and a profile
    falls back to the first thumbnail any search result carries."""

    async def search_images(*_args, **_kwargs):
        raise AssertionError("a question about a linked page must not search for pictures")

    original = research.search_all

    async def search_all(queries, **kwargs):
        results, report = await original(queries, **kwargs)
        for result in results:
            result.image = "https://cdn.example.test/stock.jpg"
        return results, report

    monkeypatch.setattr(research, "search_images", search_images)
    monkeypatch.setattr(research, "search_all", search_all)
    wired.classification = {"answer_shape": "profile"}

    stream = RunStream(redis, "l7", ttl=60)
    await orchestrator.run(stream, request(query=f"was ist {ARTICLE}"))

    _events, blocks, done = await collect(stream)
    assert done["status"] == "completed"
    assert "image" not in blocks
