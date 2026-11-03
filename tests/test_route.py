"""Routing: how much work a question is worth, decided before the work starts.

Two properties carry this feature, and both are about restraint.

The pre-router runs with no model call in front of it, so a wrong shortcut
answers the wrong question with nothing in between to catch it. Half of these
tests are therefore about what it must *not* fire on.

The planner may only take work away. A depth the user chose is a ceiling; the
classifier's guess never widens it, and (the one thing every path must
guarantee) AUTO is a request, not a budget, and nothing that spends may ever
be handed one.
"""

from __future__ import annotations

import pytest

from app.agent import depth as depth_module
from app.agent import route as route_module
from app.agent import shapes
from app.agent.budget import Budget
from app.agent.orchestrator import Classification

# ------------------------------------------------------------------ triage


@pytest.mark.parametrize(
    ("query", "place"),
    [
        ("wie ist das Wetter in Reutlingen", "Reutlingen"),
        ("wie ist das wetter in reutlingen?", "reutlingen"),
        ("Wetter München", "München"),
        ("wetter in new york", "new york"),
        ("what's the weather in San Francisco", "San Francisco"),
        ("Reutlingen Wetter", "Reutlingen"),
        ("regnet es gerade in Hamburg", "Hamburg"),
        ("Temperatur in Bad Urach", "Bad Urach"),
        # The time word belongs to the question, not to the name.
        ("wie wird das Wetter morgen in Berlin", "Berlin"),
        ("weather in Berlin tomorrow", "Berlin"),
    ],
)
def test_a_weather_question_with_a_place_is_fetched_not_searched(query, place):
    route = route_module.triage(query)
    assert route is not None, query
    assert route.mode == route_module.LOOKUP
    assert route.shape == shapes.WEATHER
    assert route.place == place


def test_a_weather_question_without_a_place_still_routes_to_the_lookup():
    """The home location fills it in, and if that is empty too the run says so
    and searches, but it must not be sent to a full research pass first."""
    route = route_module.triage("wie ist das wetter heute")
    assert route is not None
    assert route.mode == route_module.LOOKUP
    assert route.place == ""


@pytest.mark.parametrize(
    "query",
    [
        # The compound noun that made the naive substring match unusable.
        "wie funktioniert ein Wetterballon",
        "wie funktioniert ein Autoencoder",
        "wie entsteht ein Gewitter",
        "warum regnet es im Sommer weniger",
        "how does a weather station work",
        "explain the difference between weather and climate",
        "Geschichte der Wettervorhersage",
        # A weak intent word with no place: a temperature question about
        # something that is not a location.
        "wie hoch ist die Temperatur der Sonne",
        "what temperature does water boil at",
        # Long enough to be carrying a second clause.
        "wie ist das Wetter in Berlin und was sollte ich dort im Winter anziehen wenn es schneit",
        # Nothing to do with weather at all.
        "what does xautoclaim do in redis",
        "",
    ],
)
def test_triage_declines_anything_it_is_not_certain_about(query):
    route = route_module.triage(query)
    assert route is None or route.mode != route_module.LOOKUP, query


@pytest.mark.parametrize("query", ["danke!", "Hallo", "thanks", "ok", "Vielen Dank", "tschüss"])
def test_small_talk_is_answered_without_a_classifier_call(query):
    route = route_module.triage(query)
    assert route is not None
    assert route.mode == route_module.DIRECT


@pytest.mark.parametrize(
    "query",
    ["danke, und was kostet das?", "hallo, wer ist der Bundeskanzler", "ok computer band"],
)
def test_a_greeting_with_a_question_after_it_is_a_question(query):
    route = route_module.triage(query)
    assert route is None or route.mode != route_module.DIRECT


def test_a_place_is_never_guessed_from_grammar():
    """Case-insensitive matching also matches articles. A capture that reduces
    to "der" is the pattern finding the grammar, not the name."""
    assert route_module._clean_place("der") == ""
    assert route_module._clean_place("the week") == ""
    assert route_module._clean_place("das Wetter") == "Wetter"  # trimmed article, real word left
    assert route_module._clean_place("New York City tomorrow") == "New York City"
    assert route_module._clean_place("August in Berlin") == ""  # a month is not a place


# -------------------------------------------------------------------- plan


def classification(**overrides) -> Classification:
    return Classification(**{"needs_search": True, **overrides})


def test_auto_resolves_each_complexity_to_its_own_preset():
    auto = depth_module.get("auto")
    assert route_module.plan(classification(complexity="simple"), auto).depth is depth_module.SIMPLE
    assert route_module.plan(classification(), auto).depth is depth_module.BALANCED
    assert route_module.plan(classification(complexity="research"), auto).depth is depth_module.DEEP


def test_a_preset_the_reader_chose_survives_a_question_called_simple():
    """The chip is the one place a reader can outrank the classifier, so it has
    to mean what it says. Trimming "Deep" down because a cheap router model
    judged the subject ordinary left no way to insist, which is how a question
    that needed reading got a one-search answer."""
    for name in ("quick", "balanced", "deep"):
        route = route_module.plan(classification(complexity="simple"), depth_module.get(name))
        assert route.depth is depth_module.get(name), name
        assert "as chosen" in route.reason

    # Auto is where the guess decides, and there it still resolves down.
    auto = route_module.plan(classification(complexity="simple"), depth_module.get("auto"))
    assert auto.depth is depth_module.SIMPLE


def test_an_explainer_never_resolves_below_balanced_on_auto():
    """The failure this pins: "wie funktionieren Autoencoder" is read as simple,
    and defensibly so, one good source does define them. Simple is one query,
    one page and 900 tokens, so the reader who asked *because* the definition
    did not help them gets the definition, shorter."""
    auto = depth_module.get("auto")
    route = route_module.plan(
        classification(complexity="simple", answer_shape=shapes.EXPLAINER), auto
    )
    assert route.depth is depth_module.BALANCED
    assert "explain" in route.reason

    # Only the floor moves. A question the classifier reads as research still
    # gets the research preset.
    deep = route_module.plan(
        classification(complexity="research", answer_shape=shapes.EXPLAINER), auto
    )
    assert deep.depth is depth_module.DEEP


def test_an_explainer_still_cannot_widen_a_preset_the_reader_chose():
    """The floor above lives inside Auto, which is the router's own guess. A
    chip the reader touched outranks it, in this direction too."""
    route = route_module.plan(
        classification(complexity="simple", answer_shape=shapes.EXPLAINER),
        depth_module.get("quick"),
    )
    assert route.depth is depth_module.QUICK
    assert "as chosen" in route.reason


def test_a_research_question_never_widens_a_preset_the_user_chose():
    """The router may only take work away. "Quick" means quick, whatever the
    classifier thinks the question deserves."""
    route = route_module.plan(classification(complexity="research"), depth_module.get("quick"))
    assert route.depth is depth_module.QUICK
    assert route.mode == route_module.SEARCH  # not a plan-and-report run


def test_an_explicitly_chosen_preset_survives_an_ordinary_question():
    """A setting the user touched outranks the classifier's guess about it: a
    reader who asked for a plan-and-report run must not silently get two
    searches because a free model called the question ordinary."""
    route = route_module.plan(classification(), depth_module.get("deep"))
    assert route.depth is depth_module.DEEP
    assert route.mode == route_module.RESEARCH


def test_an_improvised_complexity_falls_back_to_standard_rather_than_failing():
    for value in ("thorough", "", "SIMPLE!", None):
        route = route_module.plan(classification(complexity=value or ""), depth_module.get("auto"))
        assert route.depth in (depth_module.BALANCED, depth_module.SIMPLE)


def test_auto_never_reaches_a_budget():
    """AUTO carries BALANCED's numbers so a slip degrades rather than breaks:
    but nothing should ever have to rely on that."""
    auto = depth_module.get("auto")
    for complexity in ("simple", "standard", "research", "nonsense"):
        for requested in (auto, *[depth_module.get(n) for n in ("quick", "balanced", "deep")]):
            route = route_module.plan(classification(complexity=complexity), requested)
            assert route.depth.id != "auto"
            assert Budget(route.depth).depth.id != "auto"
    assert depth_module.resolve(auto) is depth_module.BALANCED


def test_every_preset_leaves_room_to_research_before_the_writer_reserve():
    """A wall clock at or below the writer's reserve forbids research outright,
    which is a preset that can never do anything."""
    for depth in (depth_module.SIMPLE, *depth_module.DEPTHS.values()):
        assert Budget(depth).can_research(), depth.id


# -------------------------------------------------------------------- modes


def test_a_lookup_shape_routes_to_the_lookup_whatever_the_depth():
    for name in ("auto", "quick", "deep"):
        route = route_module.plan(
            classification(answer_shape="weather", place="Berlin"), depth_module.get(name)
        )
        assert route.mode == route_module.LOOKUP
        assert route.place == "Berlin"


def test_a_question_needing_no_search_is_answered_directly():
    route = route_module.plan(classification(needs_search=False), depth_module.get("auto"))
    assert route.mode == route_module.DIRECT


def test_a_shape_that_wants_a_picture_asks_for_the_image_search():
    assert route_module.plan(
        classification(answer_shape="image"), depth_module.get("auto")
    ).wants_images
    assert route_module.plan(
        classification(wants_image=True), depth_module.get("auto")
    ).wants_images
    assert not route_module.plan(classification(), depth_module.get("auto")).wants_images


def test_a_profile_searches_for_a_picture_only_when_the_classifier_wants_one():
    """A person or a brand is recognised by sight; ISO 27001 is not, and its
    profile paid for an image search that came back with stock padlocks."""
    auto = depth_module.get("auto")
    assert not route_module.plan(classification(answer_shape="profile"), auto).wants_images
    assert route_module.plan(
        classification(answer_shape="profile", wants_image=True), auto
    ).wants_images


@pytest.mark.parametrize("shape", ["", "explainer", "table"])
def test_prose_and_the_shapes_without_a_card_follow_the_classifier(shape):
    auto = depth_module.get("auto")
    wanted = classification(answer_shape=shape, wants_image=True)
    assert route_module.plan(wanted, auto).wants_images
    assert not route_module.plan(classification(answer_shape=shape), auto).wants_images


def test_the_image_query_reaches_the_route_and_survives_widening():
    auto = depth_module.get("auto")
    route = route_module.plan(classification(wants_image=True, image_query="okapi"), auto)
    assert route.image_query == "okapi"
    assert route_module.widened(route, auto).image_query == "okapi"


@pytest.mark.parametrize(
    "query",
    [
        "zeig mir ein Bild vom Eiffelturm",
        "Zeige mir Fotos der Sixtinischen Kapelle",
        "zeig mal bilder",
        "Bilder von Tesla Cybertruck",
        "show me a picture of a capybara",
        "show me some images of okapis",
        "photo of the new iPhone",
    ],
)
def test_a_picture_asked_for_by_name_is_recognised(query):
    assert route_module.asks_to_see(query)


@pytest.mark.parametrize(
    "query",
    [
        # Picture nouns inside other words.
        "wie funktioniert Bildung in Finnland",
        "mein Bildschirm flackert",
        "Bildgebung beim MRT",
        "image processing in python",
        # A request verb without a picture.
        "zeig mir wie man python installiert",
        "show me how to deploy a container",
        # Idioms the classifier reads in context, never forced here.
        "wie sieht es mit der Inflation aus",
        "what does a good CV look like",
        # The same nouns for an impression.
        "ich will mir ein Bild von der Lage machen",
        "give me the big picture of the AI market",
        "",
    ],
)
def test_a_question_that_does_not_ask_for_a_picture_is_not_forced_to(query):
    assert not route_module.asks_to_see(query)


def test_a_shape_with_its_own_card_does_not_also_go_looking_for_a_picture():
    """Observed on "wie wird das Wetter morgen": the classifier set
    `wants_image`, and a forecast ended up with a second search and "bing
    images" in its footer. The card is what the reader came to see."""
    route = route_module.plan(
        classification(answer_shape="weather", place="Berlin", wants_image=True),
        depth_module.get("auto"),
    )
    assert not route.wants_images
    # A profile still gets one: there the picture is part of the card.
    assert route_module.plan(
        classification(answer_shape="profile", wants_image=True), depth_module.get("auto")
    ).wants_images


def test_widening_a_failed_lookup_restores_the_reading_budget():
    """The cheap preset was justified by the lookup succeeding. Without it the
    question needs the pages it would have read anyway."""
    route = route_module.plan(
        classification(answer_shape="weather", place="Berlin"), depth_module.get("auto")
    )
    widened = route_module.widened(route, depth_module.get("auto"))
    assert widened.mode == route_module.SEARCH
    assert widened.depth is depth_module.BALANCED
    assert widened.shape == shapes.WEATHER  # still laid out as a forecast


# ---------------------------------------------------------------- overview


def test_facets_turn_a_searched_prose_answer_into_an_overview():
    route = route_module.plan(
        classification(facets=["timeline", "facts"]), depth_module.get("balanced")
    )
    assert route.shape == shapes.OVERVIEW
    assert route.facets == ("timeline", "facts")


def test_a_fixed_shape_always_wins_over_facets():
    """A figure card with a facts panel under it is two layouts arguing about
    which one is the answer."""
    route = route_module.plan(
        classification(answer_shape=shapes.FIGURE, facets=["facts"]), depth_module.get("balanced")
    )
    assert route.shape == shapes.FIGURE
    assert route.facets == ()


def test_an_overview_needs_sources_to_cite():
    """Every item in the panel cites a registered source, and a direct answer
    registers none, so the call would be paid for and then refused."""
    route = route_module.plan(
        classification(needs_search=False, facets=["facts"]), depth_module.get("balanced")
    )
    assert route.shape == shapes.PROSE
    assert route.facets == ()


def test_the_quick_presets_never_pay_for_a_panel():
    """The router takes work away: a second model call above a short answer it
    would mostly repeat is work the reader who chose Quick did not ask for."""
    for requested, complexity in (("quick", "standard"), ("auto", "simple")):
        route = route_module.plan(
            classification(complexity=complexity, facets=["facts"]),
            depth_module.get(requested),
        )
        assert route.shape == shapes.PROSE, requested
        assert route.facets == (), requested


def test_the_overview_is_never_accepted_by_name():
    """Without facets there is nothing to compose."""
    route = route_module.plan(
        classification(answer_shape=shapes.OVERVIEW), depth_module.get("balanced")
    )
    assert route.shape == shapes.PROSE


def test_a_widened_lookup_keeps_its_facets():
    route = route_module.Route(
        mode=route_module.LOOKUP,
        depth=depth_module.SIMPLE,
        shape=shapes.OVERVIEW,
        facets=("facts",),
    )
    assert route_module.widened(route, depth_module.get("balanced")).facets == ("facts",)
