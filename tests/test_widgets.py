"""Widget validation: what reaches the screen, and what is refused.

This layer exists so a weak model can still produce a correct widget: the model
supplies data against a schema and the server does the presenting. Which means
the schema and these checks are the *only* thing standing between a confidently
wrong model and a confidently wrong scoreboard.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent import depth as depth_module
from app.agent import widgets
from app.agent.budget import Budget
from app.agent.context import RunContext, SourceRegistry
from app.embed.rerank import Passage
from app.llm import LLMError
from app.search.base import SearchResult
from app.store.stream import RunStream


class WidgetLLM:
    """Returns one prepared object, or raises."""

    is_local = False

    def __init__(self, value=None, fail: Exception | None = None, model: str = "fake/model"):
        self.value = value
        self.fail = fail
        self.calls = 0
        self.model = model

    async def structured(self, messages, schema, **kwargs):
        self.calls += 1
        if self.fail:
            raise self.fail
        return self.value


def source(index: int, *, image: str = "", url: str = "") -> SearchResult:
    result = SearchResult(title=f"Source {index}", url=url or f"https://s{index}.test/p")
    result.image = image
    result.assess()
    return result


async def context(
    redis, llm, *, sources: dict[int, SearchResult] | None = None, router=None
) -> RunContext:
    depth = depth_module.get("balanced")
    stream = RunStream(redis, "w1", ttl=60)
    registry = SourceRegistry(stream, depth.max_sources)
    await registry.add(list((sources or {1: source(1)}).values()))
    return RunContext(
        stream=stream,
        llm=llm,
        router=router,
        depth=depth,
        budget=Budget(depth),
        sources=registry,
        query="q",
    )


def passages() -> list[Passage]:
    return [Passage("evidence", 1, "Source 1", "https://s1.test/p")]


async def widgets_emitted(stream: RunStream) -> list[dict]:
    """Widget blocks only. Registering sources emits a `sources` block of its
    own, which is not what any of these assertions are about."""
    from app.agent import shapes

    return [block for block in await stream.snapshot() if block["type"] in shapes.CARDS]


# ----------------------------------------------------------------- schema


def test_a_direction_the_model_improvised_is_dropped_not_rendered():
    assert widgets.Figure(direction="up").direction == "up"
    assert widgets.Figure(direction="skyrocketing").direction == ""
    assert widgets.Figure(direction="").direction == ""


def test_only_a_real_iso_code_survives_because_a_wrong_one_shows_a_wrong_flag():
    assert widgets.Side(code="DE").code == "de"
    assert widgets.Side(code="deu").code == ""  # alpha-3 resolves to no flag, not an error
    assert widgets.Side(code="1").code == ""
    assert widgets.Side(code="").code == ""


def test_a_match_status_outside_the_three_is_dropped():
    assert widgets.Match(status="live").status == "live"
    assert widgets.Match(status="halftime").status == ""


def test_whitespace_and_newlines_in_a_value_are_normalised():
    figure = widgets.Figure(label="  Apple\n (AAPL) ", value=" 302.25 ")
    assert figure.label == "Apple (AAPL)"
    assert figure.value == "302.25"


async def test_a_card_is_filled_by_the_router_model_when_there_is_one(redis):
    """Filling six named fields from evidence already in front of the model is
    extraction, not writing, and on a reasoning answer model it was fifteen
    seconds spent on someone's date of birth."""
    answer = WidgetLLM(widgets.Figure(label="Apple", value="302.25", source=1))
    router = WidgetLLM(widgets.Figure(label="Apple", value="302.25", source=1))
    ctx = await context(redis, answer, router=router)

    assert await widgets.emit(ctx, "figure", "what is AAPL at", passages()) is True
    assert (router.calls, answer.calls) == (1, 0)


async def test_a_router_too_small_for_the_card_costs_a_call_and_not_the_card(redis):
    """Observed with a 70B router: it echoed the example labels back with empty
    values. The cheap attempt is worth making, and it must not be the last one."""
    router = WidgetLLM(widgets.Figure(label="Apple", value="", source=1))  # nothing in it
    answer = WidgetLLM(widgets.Figure(label="Apple", value="302.25", source=1))
    ctx = await context(redis, answer, router=router)

    assert await widgets.emit(ctx, "figure", "what is AAPL at", passages()) is True
    assert (router.calls, answer.calls) == (1, 1)
    block = next(b for b in await widgets_emitted(ctx.stream) if b["type"] == "figure")
    assert block["data"]["value"] == "302.25"


async def test_a_card_both_models_refuse_is_still_only_a_missing_card(redis):
    empty = widgets.Figure(label="Apple", value="", source=1)
    ctx = await context(redis, WidgetLLM(empty), router=WidgetLLM(empty))

    assert await widgets.emit(ctx, "figure", "q", passages()) is False
    assert not await widgets_emitted(ctx.stream)


# ------------------------------------------------------------- clipping


def test_a_value_that_is_merely_too_long_is_trimmed_rather_than_refused():
    """Refused, one 92-character fact would cost the whole card, and the retry a
    second round-trip to be told the same thing."""
    profile = widgets.Profile.model_validate(
        {
            "name": "Ada Lovelace",
            "subtitle": "English mathematician and writer, " + "very " * 40 + "notable",
            "facts": [
                {"label": "Known for", "value": "her work on " + "the analytical engine " * 10},
                {"label": "Born", "value": "10 December 1815"},
            ],
            "source": 1,
        }
    )
    assert len(profile.subtitle) == 80
    assert len(profile.facts[0].value) == 80
    assert profile.facts[1].value == "10 December 1815"  # untouched


def test_a_list_longer_than_the_layout_allows_is_cut_to_it():
    series = widgets.Series.model_validate(
        {
            "label": "Bitcoin",
            "points": [{"at": str(year), "value": "1"} for year in range(2000, 2050)],
            "source": 1,
        }
    )
    assert len(series.points) == widgets.MAX_POINTS


def test_what_a_trim_cannot_fix_is_still_refused():
    """Clipping is for length, not for substance."""
    with pytest.raises(ValidationError):
        widgets.Profile.model_validate({"name": "Ada", "facts": "not a list", "source": 1})


# ------------------------------------------------------------- validation


async def test_a_figure_citing_a_source_the_run_never_saw_is_refused(redis):
    """A hallucinated [9] fails here rather than appearing next to a number."""
    llm = WidgetLLM(widgets.Figure(label="Apple", value="302.25", source=9))
    ctx = await context(redis, llm)

    assert await widgets.emit(ctx, "figure", "what is AAPL at", passages()) is False
    assert not await widgets_emitted(ctx.stream)


async def test_a_figure_with_a_real_source_is_emitted(redis):
    llm = WidgetLLM(
        widgets.Figure(label="Apple (AAPL)", value="302.25", unit="USD", direction="down", source=1)
    )
    ctx = await context(redis, llm)

    assert await widgets.emit(ctx, "figure", "what is AAPL at", passages()) is True
    block = next(b for b in await widgets_emitted(ctx.stream) if b["type"] == "figure")
    assert block["data"]["value"] == "302.25"
    assert block["data"]["source"] == 1


async def test_a_figure_without_a_value_is_not_a_figure(redis):
    llm = WidgetLLM(widgets.Figure(label="Apple", value="", source=1))
    ctx = await context(redis, llm)
    assert await widgets.emit(ctx, "figure", "q", passages()) is False


async def test_an_image_widget_uses_the_retrieved_url_not_one_the_model_names(redis):
    """The model chooses between pictures that were retrieved; it never supplies
    an address. This is what stops "show me the Argentine flag" being answered
    with a remembered link to something else."""
    sources = {1: source(1, image="https://commons.test/flag.svg", url="https://commons.test/page")}
    llm = WidgetLLM(widgets.Picture(source=1, caption="Flag of Argentina"))
    ctx = await context(redis, llm, sources=sources)

    assert await widgets.emit(ctx, "image", "what does it look like", passages()) is True
    block = next(b for b in await widgets_emitted(ctx.stream) if b["type"] == "image")
    assert block["data"]["url"] == "https://commons.test/flag.svg"
    assert block["data"]["page_url"] == "https://commons.test/page"


async def test_an_image_widget_citing_a_source_with_no_picture_is_refused(redis):
    llm = WidgetLLM(widgets.Picture(source=1, caption="a flag"))
    ctx = await context(redis, llm)  # source 1 carries no image
    assert await widgets.emit(ctx, "image", "q", passages()) is False


async def test_a_match_needs_both_sides_named(redis):
    llm = WidgetLLM(widgets.Match(home=widgets.Side(name="Deutschland"), source=1))
    ctx = await context(redis, llm)
    assert await widgets.emit(ctx, "match", "q", passages()) is False


async def test_a_match_with_two_sides_is_emitted_with_its_flags(redis):
    llm = WidgetLLM(
        widgets.Match(
            home=widgets.Side(name="Deutschland", code="DE", score="2"),
            away=widgets.Side(name="Argentinien", code="AR", score="1"),
            competition="Freundschaftsspiel",
            status="final",
            source=1,
        )
    )
    ctx = await context(redis, llm)

    assert await widgets.emit(ctx, "match", "q", passages()) is True
    block = next(b for b in await widgets_emitted(ctx.stream) if b["type"] == "match")
    assert block["data"]["home"]["code"] == "de"
    assert block["data"]["away"]["code"] == "ar"


# ------------------------------------------------------------ degradation


async def test_a_model_that_cannot_fill_the_schema_costs_the_widget_and_nothing_else(redis):
    """The prose answer runs either way: a widget is never worth failing a run
    that was about to produce an answer."""
    llm = WidgetLLM(fail=LLMError("no structured output on this model"))
    ctx = await context(redis, llm)

    assert await widgets.emit(ctx, "figure", "q", passages()) is False
    assert not await widgets_emitted(ctx.stream)


async def test_a_shape_with_no_widget_makes_no_call_at_all(redis):
    llm = WidgetLLM(widgets.Figure(label="x", value="1", source=1))
    ctx = await context(redis, llm)

    # `table` is written as markdown by the writer, so there is no schema to
    # fill and no call to make.
    assert await widgets.emit(ctx, "table", "q", passages()) is False
    assert llm.calls == 0


async def test_no_evidence_means_no_widget_call(redis):
    """Without passages there is nothing to fill a schema from, and asking anyway
    is an invitation to invent one."""
    llm = WidgetLLM(widgets.Figure(label="x", value="1", source=1))
    ctx = await context(redis, llm)

    assert await widgets.emit(ctx, "figure", "q", []) is False
    assert llm.calls == 0


# ------------------------------------------------------------------ series


def series(points: int = 4, **overrides) -> widgets.Series:
    return widgets.Series(
        **{
            "label": "Bitcoin",
            "unit": "USD",
            "points": [widgets.Point(at=f"202{i}", value=str(1000 * i)) for i in range(points)],
            "source": 1,
            **overrides,
        }
    )


async def test_a_series_needs_enough_points_to_be_a_trend(redis):
    """Two points are a comparison, and a curve through them is a straight line
    pretending to be a trend."""
    ctx = await context(redis, WidgetLLM(series(points=2)))
    assert await widgets.emit(ctx, "series", "q", passages()) is False


async def test_a_series_with_unparsable_values_is_still_emitted(redis):
    """The client parses leniently for the curve only. A value it cannot plot
    costs the line, never the table: dropping the row would lose the source's
    own words, which is the thing the string type exists to keep."""
    data = series()
    data.points[1].value = "about 3k"
    ctx = await context(redis, WidgetLLM(data))

    assert await widgets.emit(ctx, "series", "q", passages()) is True
    block = next(b for b in await widgets_emitted(ctx.stream) if b["type"] == "series")
    assert [p["value"] for p in block["data"]["points"]] == ["0", "about 3k", "2000", "3000"]


async def test_a_series_citing_an_unknown_source_is_refused(redis):
    ctx = await context(redis, WidgetLLM(series(source=9)))
    assert await widgets.emit(ctx, "series", "q", passages()) is False


# -------------------------------------------------------------- comparison


def comparison(rows: int = 3, **overrides) -> widgets.Comparison:
    return widgets.Comparison(
        **{
            "title": "DuckDB vs ClickHouse",
            "left": widgets.Column(name="DuckDB"),
            "right": widgets.Column(name="ClickHouse"),
            "rows": [
                widgets.CompareRow(label=f"Attribute {i}", left="a", right="b") for i in range(rows)
            ],
            "source": 1,
            **overrides,
        }
    )


async def test_a_comparison_needs_both_sides_named(redis):
    data = comparison()
    data.right = widgets.Column(name="")
    ctx = await context(redis, WidgetLLM(data))
    assert await widgets.emit(ctx, "comparison", "q", passages()) is False


async def test_a_comparison_with_one_row_is_a_sentence_not_a_table(redis):
    ctx = await context(redis, WidgetLLM(comparison(rows=1)))
    assert await widgets.emit(ctx, "comparison", "q", passages()) is False


async def test_a_row_empty_on_both_sides_is_dropped_and_the_rest_survives(redis):
    data = comparison()
    data.rows.append(widgets.CompareRow(label="Licence", left="", right=""))
    ctx = await context(redis, WidgetLLM(data))

    assert await widgets.emit(ctx, "comparison", "q", passages()) is True
    block = next(b for b in await widgets_emitted(ctx.stream) if b["type"] == "comparison")
    assert len(block["data"]["rows"]) == 3


# ------------------------------------------------------------------- steps


async def test_a_procedure_needs_at_least_two_steps(redis):
    data = widgets.Steps(title="Install", steps=[widgets.Step(text="Run it")], source=1)
    ctx = await context(redis, WidgetLLM(data))
    assert await widgets.emit(ctx, "steps", "q", passages()) is False


async def test_a_step_without_text_is_dropped_not_numbered(redis):
    """A blank step still gets a number on screen, which is a step the reader
    goes looking for."""
    data = widgets.Steps(
        title="Install",
        steps=[
            widgets.Step(text="Download the archive"),
            widgets.Step(text="", detail="orphaned detail"),
            widgets.Step(text="Run the installer", detail="`./install.sh`"),
        ],
        source=1,
    )
    ctx = await context(redis, WidgetLLM(data))

    assert await widgets.emit(ctx, "steps", "q", passages()) is True
    block = next(b for b in await widgets_emitted(ctx.stream) if b["type"] == "steps")
    assert [s["text"] for s in block["data"]["steps"]] == [
        "Download the archive",
        "Run the installer",
    ]


# ----------------------------------------------------------------- profile


def profile(**overrides) -> widgets.Profile:
    return widgets.Profile(
        **{
            "name": "Ada Lovelace",
            "subtitle": "Mathematician, 1815-1852",
            "facts": [
                widgets.Row(label="Born", value="1815"),
                widgets.Row(label="Known for", value="the first algorithm"),
            ],
            "source": 1,
            **overrides,
        }
    )


async def test_a_profile_without_a_picture_is_still_a_profile(redis):
    """Unlike the image widget, where no picture means no answer. A card lays
    out perfectly well without a portrait."""
    ctx = await context(redis, WidgetLLM(profile(image_source=0)))

    assert await widgets.emit(ctx, "profile", "who is Ada Lovelace", passages()) is True
    block = next(b for b in await widgets_emitted(ctx.stream) if b["type"] == "profile")
    assert block["data"]["image"] is None
    assert block["data"]["name"] == "Ada Lovelace"


async def test_a_profile_portrait_comes_from_a_retrieved_result_not_from_the_model(redis):
    sources = {1: source(1, image="https://commons.test/ada.jpg", url="https://commons.test/ada")}
    ctx = await context(redis, WidgetLLM(profile(image_source=1)), sources=sources)

    assert await widgets.emit(ctx, "profile", "q", passages()) is True
    block = next(b for b in await widgets_emitted(ctx.stream) if b["type"] == "profile")
    assert block["data"]["image"]["url"] == "https://commons.test/ada.jpg"


async def test_a_profile_naming_a_source_with_no_picture_loses_the_portrait_only(redis):
    ctx = await context(redis, WidgetLLM(profile(image_source=1)))  # source 1 has no image

    assert await widgets.emit(ctx, "profile", "q", passages()) is True
    block = next(b for b in await widgets_emitted(ctx.stream) if b["type"] == "profile")
    assert block["data"]["image"] is None


async def test_a_profile_that_declined_a_portrait_still_shows_a_retrieved_picture(redis):
    """The run has already searched for pictures and paid for them by the time
    the card is filled. Asked "was ist ubuntu" it retrieved six, including a
    screenshot of the desktop, and showed none of them because the card came
    back with `image_source: 0`.

    Shown as a picture of its own rather than promoted into the portrait well:
    the model said none of these shows the subject, and overriding that on an
    identity card is how the wrong face ends up under the right name."""
    sources = {
        1: source(1),
        2: source(2, image="https://ds.test/ubuntushellthemes.png", url="https://ds.test/p"),
    }
    ctx = await context(redis, WidgetLLM(profile(image_source=0)), sources=sources)

    assert await widgets.emit(ctx, "profile", "was ist ubuntu", passages()) is True
    emitted = await widgets_emitted(ctx.stream)
    assert [block["type"] for block in emitted] == ["profile", "image"]
    assert emitted[0]["data"]["image"] is None
    assert emitted[1]["data"]["url"] == "https://ds.test/ubuntushellthemes.png"
    assert emitted[1]["data"]["source"] == 2


async def test_a_profile_without_a_portrait_and_without_pictures_stays_one_card(redis):
    ctx = await context(redis, WidgetLLM(profile(image_source=0)))

    assert await widgets.emit(ctx, "profile", "q", passages()) is True
    assert [block["type"] for block in await widgets_emitted(ctx.stream)] == ["profile"]


async def test_a_profile_with_a_name_and_nothing_else_is_refused(redis):
    ctx = await context(redis, WidgetLLM(widgets.Profile(name="Ada Lovelace", source=1)))
    assert await widgets.emit(ctx, "profile", "q", passages()) is False


# ------------------------------------------------- when the card is lost anyway


async def test_a_schema_failure_is_retried_before_the_card_is_given_up(redis):
    """One unusable JSON response cost the Apollo 17 card outright. With no
    router configured the retry is the same model, and on `openrouter/auto`
    not even the same model underneath."""
    llm = WidgetLLM(fail=LLMError("did not return usable JSON"))
    ctx = await context(redis, llm)

    assert await widgets.emit(ctx, "profile", "q", passages()) is False
    assert llm.calls == 2


async def test_a_lost_picture_card_still_shows_the_picture_that_was_retrieved(redis):
    """A `wants_image` shape has already paid for an image search. Showing
    nothing because a *different* call could not fill a schema throws away
    evidence the run is holding."""
    sources = {
        1: source(1),
        2: source(2, image="https://commons.test/apollo.jpg", url="https://commons.test/apollo"),
    }
    ctx = await context(redis, WidgetLLM(fail=LLMError("no json")), sources=sources)

    assert await widgets.emit(ctx, "profile", "apollo 17", passages()) is True
    block = next(b for b in await widgets_emitted(ctx.stream) if b["type"] == "image")
    assert block["data"]["url"] == "https://commons.test/apollo.jpg"
    assert block["data"]["source"] == 2
    # Nothing on the card was written by a model: the picture is the one the
    # search returned, and no caption was invented for it.
    assert block["data"]["caption"] == ""


async def test_a_shape_that_wants_no_picture_gets_no_consolation_prize(redis):
    sources = {1: source(1, image="https://commons.test/x.jpg")}
    ctx = await context(redis, WidgetLLM(fail=LLMError("no json")), sources=sources)

    assert await widgets.emit(ctx, "table", "q", passages()) is False
    assert not await widgets_emitted(ctx.stream)


# ----------------------------------------------------------------- diagram


def graph(nodes: list[str], edges: list[tuple[str, str]]) -> widgets.Diagram:
    return widgets.Diagram(
        title="Request flow",
        nodes=[widgets.Node(id=n, label=n.title()) for n in nodes],
        edges=[widgets.Edge(**{"from": a, "to": b}) for a, b in edges],
        source=1,
    )


async def test_an_unconnected_box_is_pruned_rather_than_costing_the_diagram(redis):
    """Rejecting the whole picture would happen invisibly, since an empty card
    is hidden by CSS. A correct three-box diagram is worth more than a fourth
    box the model could not place."""
    boxes = ["client", "cache", "origin", "orphan"]
    data = graph(boxes, [("client", "cache"), ("cache", "origin")])
    ctx = await context(redis, WidgetLLM(data))

    assert await widgets.emit(ctx, "diagram", "q", passages()) is True
    block = next(b for b in await widgets_emitted(ctx.stream) if b["type"] == "diagram")
    assert [n["id"] for n in block["data"]["nodes"]] == ["client", "cache", "origin"]


async def test_a_diagram_with_no_structure_left_says_so_instead_of_vanishing(redis):
    data = graph(["a", "b", "c"], [])
    ctx = await context(redis, WidgetLLM(data))

    assert await widgets.emit(ctx, "diagram", "q", passages()) is False
    notices = [b for b in await ctx.stream.snapshot() if b["type"] == "notice"]
    assert notices and "connect" in notices[0]["data"]["text"]


async def test_a_dangling_edge_costs_the_arrow_not_the_diagram(redis):
    data = graph(["a", "b"], [("a", "b"), ("a", "ghost")])
    ctx = await context(redis, WidgetLLM(data))

    assert await widgets.emit(ctx, "diagram", "q", passages()) is True
    block = next(b for b in await widgets_emitted(ctx.stream) if b["type"] == "diagram")
    assert len(block["data"]["edges"]) == 1


# ---------------------------------------------------------------- overview


def overview(*sections) -> widgets.Overview:
    return widgets.Overview.model_validate({"sections": list(sections)})


async def overview_ctx(redis, value, facets=("timeline", "facts", "proscons")):
    ctx = await context(redis, WidgetLLM(value), sources={1: source(1), 2: source(2)})
    ctx.facets = facets
    return ctx


TIMELINE = {
    "kind": "timeline",
    "title": "Zeitleiste",
    "items": [
        {"label": "1989", "text": "Mauerfall", "source": 1},
        {"label": "1990", "text": "Wiedervereinigung", "source": 2},
        {"label": "1991", "text": "Hauptstadtbeschluss", "source": 1},
    ],
}


async def test_an_overview_is_composed_from_the_blocks_the_classifier_named(redis):
    value = overview(
        TIMELINE,
        {
            "kind": "facts",
            "title": "Eckdaten",
            "items": [
                {"label": "Fläche", "text": "891 km²", "source": 1},
                {"label": "Einwohner", "text": "3,8 Mio.", "source": 2},
            ],
        },
    )
    ctx = await overview_ctx(redis, value)
    assert await widgets.emit(ctx, "overview", "q", passages()) is True

    (block,) = await widgets_emitted(ctx.stream)
    sections = block["data"]["sections"]
    assert [s["kind"] for s in sections] == ["timeline", "facts"]
    assert sections[0]["items"][1] == {
        "label": "1990",
        "text": "Wiedervereinigung",
        "note": "",
        "source": 2,
    }


async def test_one_uncited_item_costs_the_item_and_not_the_section(redis):
    """A citation per item is the price of a panel drawn from several pages, and
    the check is per item for the same reason."""
    timeline = {
        **TIMELINE,
        "items": [*TIMELINE["items"], {"label": "1994", "text": "x", "source": 9}],
    }
    ctx = await overview_ctx(redis, overview(timeline))
    assert await widgets.emit(ctx, "overview", "q", passages()) is True
    (block,) = await widgets_emitted(ctx.stream)
    assert [i["label"] for i in block["data"]["sections"][0]["items"]] == ["1989", "1990", "1991"]


async def test_a_section_below_its_minimum_is_dropped_and_the_rest_survives(redis):
    """Two dates are not a timeline. Losing it must not cost the facts beside it."""
    short = {**TIMELINE, "items": TIMELINE["items"][:2]}
    facts = {
        "kind": "facts",
        "items": [
            {"label": "a", "text": "1", "source": 1},
            {"label": "b", "text": "2", "source": 1},
        ],
    }
    ctx = await overview_ctx(redis, overview(short, facts))
    assert await widgets.emit(ctx, "overview", "q", passages()) is True
    (block,) = await widgets_emitted(ctx.stream)
    assert [s["kind"] for s in block["data"]["sections"]] == ["facts"]


async def test_a_list_of_unnamed_clauses_is_the_argument_chopped_up_and_is_dropped(redis):
    """A list item needs a name of its own. Without one the entries are the
    prose's points with the connections cut, and three named items at least:
    two are a sentence."""
    clauses = {
        "kind": "list",
        "items": [
            {"text": "Die Nachfrage ist gestiegen", "source": 1},
            {"text": "Das Angebot ist knapp", "source": 1},
            {"label": "Zinsen", "text": "niedrig", "source": 2},
        ],
    }
    named = {
        "kind": "list",
        "items": [
            {"label": "Sodbrennen", "text": "sehr häufig", "source": 1},
            {"label": "Übelkeit", "text": "häufig", "source": 2},
            {"label": "Schwindel", "text": "gelegentlich", "source": 1},
        ],
    }
    ctx = await overview_ctx(redis, overview(clauses), facets=("list",))
    assert await widgets.emit(ctx, "overview", "q", passages()) is False

    ctx = await overview_ctx(redis, overview(named), facets=("list",))
    assert await widgets.emit(ctx, "overview", "q", passages()) is True
    (block,) = await widgets_emitted(ctx.stream)
    assert [i["label"] for i in block["data"]["sections"][0]["items"]] == [
        "Sodbrennen",
        "Übelkeit",
        "Schwindel",
    ]


async def test_a_block_the_classifier_did_not_name_is_decoration_and_is_dropped(redis):
    callout = {"kind": "callout", "items": [{"text": "Beware", "source": 1}]}
    ctx = await overview_ctx(redis, overview(callout, TIMELINE), facets=("timeline",))
    assert await widgets.emit(ctx, "overview", "q", passages()) is True
    (block,) = await widgets_emitted(ctx.stream)
    assert [s["kind"] for s in block["data"]["sections"]] == ["timeline"]


async def test_a_one_sided_weighing_is_refused_and_the_side_words_are_normalised(redis):
    one_sided = {
        "kind": "proscons",
        "items": [
            {"label": "Vorteil", "text": "billig", "source": 1},
            {"label": "pro", "text": "leise", "source": 1},
        ],
    }
    ctx = await overview_ctx(redis, overview(one_sided))
    # Nothing survives, and nothing promised the reader the panel, so it is
    # refused without a notice.
    assert await widgets.emit(ctx, "overview", "q", passages()) is False
    assert await widgets_emitted(ctx.stream) == []

    both = {
        "kind": "proscons",
        "items": [
            {"label": "Vorteil", "text": "billig", "source": 1},
            {"label": "Nachteil", "text": "laut", "source": 2},
        ],
    }
    ctx = await overview_ctx(redis, overview(both))
    assert await widgets.emit(ctx, "overview", "q", passages()) is True
    (block,) = await widgets_emitted(ctx.stream)
    assert [i["label"] for i in block["data"]["sections"][0]["items"]] == ["pro", "con"]


async def test_a_grid_row_short_a_value_is_padded_not_refused(redis):
    grid = {
        "kind": "grid",
        "columns": ["Preis", "Reichweite", ""],
        "items": [
            {"label": "A", "cells": ["30k"], "source": 1},
            {"label": "B", "cells": ["40k", "500 km", "extra"], "source": 1},
        ],
    }
    ctx = await overview_ctx(redis, overview(grid), facets=("grid",))
    assert await widgets.emit(ctx, "overview", "q", passages()) is True
    (block,) = await widgets_emitted(ctx.stream)
    section = block["data"]["sections"][0]
    assert section["columns"] == ["Preis", "Reichweite"]
    assert [i["cells"] for i in section["items"]] == [["30k", ""], ["40k", "500 km"]]


def test_every_facet_has_a_completeness_rule():
    """A block kind added to the registry without one would raise on the first
    panel that used it, inside a card call whose failures are silent."""
    from app.agent import shapes

    assert set(widgets._COMPLETE) == set(shapes.FACETS)


def test_a_model_writing_numbers_and_nulls_still_gets_its_card():
    """Prompt mode, where the small models this layer is for end up, routinely
    writes `18` for a temperature and `null` for an empty field. Pydantic
    refuses both for a string, which would cost the whole card."""
    weather = widgets.Weather.model_validate_json(
        '{"location": "Berlin", "temperature": 18, "unit": null,'
        ' "days": [{"day": "Mo", "high": 21.5, "low": null}], "source": null}'
    )
    assert weather.temperature == "18"
    assert weather.unit == ""
    assert (weather.days[0].high, weather.days[0].low) == ("21.5", "")
    assert weather.source == 0


def test_an_edge_arriving_under_its_alias_is_still_fitted():
    edge = widgets.Edge.model_validate({"from": "Encoder A", "to": "b", "label": "  feeds\n x "})
    assert (edge.source, edge.target, edge.label) == ("encodera", "b", "feeds x")


def test_an_improvised_block_kind_is_unknown_rather_than_an_error():
    assert widgets.Section(kind="Mindmap").kind == ""
    assert widgets.Section(kind=" Timeline ").kind == "timeline"


def test_the_overview_call_describes_only_the_blocks_it_may_use():
    from app.agent import prompts, shapes

    assert set(prompts.OVERVIEW_FACETS) == set(shapes.FACETS)
    text = prompts.overview_widget(("timeline",))
    assert '"timeline"' in text
    assert '"callout"' not in text


# ---------------------------------------------------------------- registry


def test_every_widget_shape_has_a_schema_and_every_schema_a_shape():
    """`shapes.py` is the single source of truth, so a layout offered to the
    classifier that has no schema here would fail at the moment a reader asked
    for it. This is the check that stops that reaching runtime."""
    from app.agent import shapes

    assert set(shapes.CARDS) == set(widgets._WIDGETS)
    for shape, (schema, instructions) in widgets._WIDGETS.items():
        assert shapes.get(shape) == shape, shape
        assert schema.__name__ and instructions.strip(), shape
