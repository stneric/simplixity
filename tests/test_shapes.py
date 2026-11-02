"""Answer layout: which shape a run picks, and what the writer is told.

The failure being designed out is a share price arriving as a paragraph the
reader has to dig a number out of, and its opposite, a three-row table
explaining a concept, which is what happens when the formatting instructions
reach every answer instead of the ones that need them.
"""

from __future__ import annotations

from app.agent import depth as depth_module
from app.agent import prompts, shapes


def system_of(messages: list[dict]) -> str:
    return messages[0]["content"]


# --------------------------------------------------------------------- shapes


def test_known_shapes_resolve_case_insensitively():
    assert shapes.get("figure") == shapes.FIGURE
    assert shapes.get("  Series ") == shapes.SERIES
    assert shapes.get("TABLE") == shapes.TABLE


def test_an_improvised_shape_falls_back_to_prose_instead_of_failing():
    for value in ("chart", "widget", "", None, "  "):
        assert shapes.get(value) == shapes.PROSE


def test_every_shape_carries_a_spec_and_prose_carries_none():
    for shape in shapes.SHAPES:
        assert shapes.spec(shape)
    assert shapes.spec(shapes.PROSE) == ""
    assert shapes.spec("nonsense") == ""


def test_every_spec_demands_a_unit_an_as_of_and_a_citation():
    """A figure with no timestamp is a number that was true once, and of
    everything in an answer it is what rots fastest."""
    for shape in shapes.SHAPES:
        spec = shapes.spec(shape).lower()
        assert "unit" in spec
        assert "as of" in spec
        assert "cited" in spec or "citation" in spec


def test_no_spec_invites_the_model_to_compute_a_figure_itself():
    for shape in shapes.SHAPES:
        # Normalised: the specs are hard-wrapped, so a phrase can straddle a
        # newline and an assertion on the raw text would pass or fail on where
        # the line happened to break.
        spec = " ".join(shapes.spec(shape).lower().split())
        assert "never convert" in spec
        assert "compute a figure the sources do not state" in spec


# -------------------------------------------------------------------- prompts


def test_the_classifier_is_told_how_to_choose_a_shape():
    system = system_of(prompts.classify("what is AAPL at", [], depth_module.get("quick")))
    assert "answer_shape" in system
    # Every shape the classifier may name, and not the overview, which it
    # reaches through `facets` and never by name.
    for shape in shapes.SHAPES - {shapes.OVERVIEW}:
        assert f'"{shape}"' in system
    assert '"overview"' not in system
    assert "facets" in system
    for facet in shapes.FACETS:
        assert f'"{facet}"' in system


def test_the_layout_spec_reaches_the_writer_only_when_a_shape_was_chosen():
    quick = depth_module.get("quick")
    plain = system_of(prompts.answer("q", [], [], quick))
    shaped = system_of(prompts.answer("q", [], [], quick, shapes.FIGURE))

    assert "Presenting the figures" not in plain
    # A widget shape writes prose *around* what is already rendered, so it gets
    # the other heading. What matters is that plain prose gets neither.
    assert "Writing alongside what is shown" not in plain
    assert "Writing alongside what is shown" in shaped
    # The citation and evidence contracts are unconditional; only layout is not.
    assert "Cite sources inline" in plain and "Cite sources inline" in shaped


def test_both_writers_are_told_to_settle_the_question_and_not_the_sources_silence():
    """The run this pins: "wann waren wir zuletzt auf dem Mond" was answered
    "Apollo 17, Dezember 1972" and then told the reader Apollo 17 was the last
    crewed Moon *flight* until Artemis 2 in 2026, which does not land. Every
    sentence was supported by its source. Only the reader was misled."""
    quick = depth_module.get("quick")
    for system in (
        system_of(prompts.answer("q", [], [], quick)),
        system_of(prompts.report("q", [], "", [])),
    ):
        assert "the question the reader actually asked" in system
        assert "silence" in system


def test_a_report_takes_a_shape_too():
    shaped = system_of(prompts.report("q", [], "", [], shapes.TABLE))
    assert "Presenting the figures" in shaped
    assert "one row per entity" in shaped


def test_a_widget_shape_tells_the_writer_not_to_restate_the_widget():
    """The reader already sees the scoreboard; a sentence repeating the score is
    the thing this whole layer replaced."""
    for shape in shapes.WIDGET_SHAPES - {shapes.DIAGRAM}:
        spec = " ".join(shapes.spec(shape).split()).lower()
        assert "already shown" in spec
        assert "do not" in spec


# ----------------------------------------------------------------- explainer


def test_the_explainer_asks_for_the_parts_an_encyclopaedia_entry_leaves_out():
    """The reader who asks "wie funktionieren Autoencoder" has usually just read
    the definition. What the definition is missing is never the facts: it is the
    purpose before the parts, one worked case, and an admission of where the
    intuition breaks, and a model writes none of the three unless asked."""
    spec = " ".join(shapes.spec(shapes.EXPLAINER).split()).lower()
    assert "the problem it solves" in spec
    assert "worked small" in spec
    assert "where the intuition breaks" in spec
    # Understanding sets the length here, against the app's own default that a
    # short question deserves a short answer.
    assert "length follows understanding" in spec


def test_the_explainer_permits_an_uncited_analogy_without_loosening_the_contract():
    """The one carve-out in the app: an analogy is not a claim about the world
    and no source can be given for one, so forbidding it, which is what the
    unmodified citation rules do, is what turns the answer back into the
    encyclopaedia entry the reader could not follow. Every actual assertion is
    still cited, and the spec has to say so in the same breath."""
    spec = " ".join(shapes.spec(shapes.EXPLAINER).split())
    assert "carry no citation, and that is deliberate" in spec
    assert "may never smuggle a fact in" in spec
    assert "is cited exactly as it is anywhere else" in spec


def test_the_explainer_never_writes_around_a_diagram_that_may_not_arrive():
    """It borrows the widget, so the picture is refused whenever the sources
    describe no connected parts, and the prose is streamed without knowing
    which happened."""
    assert shapes.companion(shapes.EXPLAINER) == shapes.DIAGRAM
    assert shapes.companion(shapes.DIAGRAM) == ""  # that shape *is* the diagram
    spec = " ".join(shapes.spec(shapes.EXPLAINER).split())
    assert "not guaranteed" in spec
    assert '"as shown above"' in spec


def test_an_explanation_is_not_handed_the_rules_for_laying_out_a_table():
    """Instruction dilution is real: the alignment syntax is for a shape that
    writes a table, and an explanation has no column to rag."""
    spec = shapes.spec(shapes.EXPLAINER)
    assert "---:" not in spec
    assert spec.startswith("## Explaining this so it is understood")
    # Its figures are still anchored like everyone else's.
    assert "as of" in spec.lower()


def test_a_figure_question_pulls_in_the_corpus_that_can_answer_it():
    assert "data" in shapes.lenses_for(shapes.FIGURE)
    assert "data" in shapes.lenses_for(shapes.SERIES)


def test_images_are_never_added_to_the_search_the_answer_is_written_from():
    """Image results are pages built around a photograph (stock libraries,
    wallpaper sites), and as text evidence they are worth nothing. An earlier
    version added the images lens to the main search and the prose for "what
    does the Argentine flag look like" came back cited to Pixabay. Pictures get
    their own search, after the evidence has its citation numbers."""
    for shape in shapes.SHAPES:
        assert "images" not in shapes.lenses_for(shape), shape


def test_a_picture_is_offered_by_its_own_name_and_not_by_its_article():
    """The failure this pins: the candidate list described the *page*, so a model
    asked which source carries a picture of Ubuntu read "Linux Mint vs Ubuntu:
    Which One is Right for You", answered 0, and the run showed none of the six
    pictures it had retrieved. `ubuntushellthemes.png` was one of them and the
    model was never shown that word."""
    from app.search.base import SearchResult

    result = SearchResult(title="Linux Mint vs Ubuntu: Which One is Right for You", url="https://x")
    result.image = "https://i.ds.at/rs:fill:1600:0/plain/2020/04/21/ubuntu-shell-themes.png?v=2"
    result.assess()

    listing = prompts._image_listing([(8, result)])
    assert listing.startswith("[8] ubuntu shell themes")
    # The article is still named, as where the picture was found.
    assert "Linux Mint vs Ubuntu" in listing


def test_the_writer_is_asked_for_the_alignment_syntax_the_renderer_honours():
    """`---:` is not decoration: the renderer reads it, and a column of numbers
    ragged-left is the thing this whole feature is about."""
    assert "---:" in system_of(prompts.answer("q", [], [], depth_module.get("quick"), shapes.TABLE))


# ----------------------------------------------------------------- overview


def test_facets_are_cleaned_rather_than_trusted():
    """A model that improvised a block kind costs that block, not the panel,
    and a panel past three blocks is a second answer above the first."""
    assert shapes.facets(["Timeline", "timeline", "mindmap", "facts"]) == ("timeline", "facts")
    assert shapes.facets(["facts", "stats", "list", "callout"]) == ("facts", "stats", "list")
    assert shapes.facets(None) == ()


def test_the_overview_borrows_its_panel_and_never_depends_on_it():
    """The panel is refused whenever the sources do not fill it, and the prose
    is streamed without knowing which happened."""
    assert shapes.companion(shapes.OVERVIEW) == shapes.OVERVIEW
    assert shapes.OVERVIEW not in shapes.WIDGET_SHAPES
    assert shapes.OVERVIEW in shapes.CARDS
    spec = " ".join(shapes.spec(shapes.OVERVIEW).split())
    assert spec.startswith("## Writing beside an overview")
    assert "drawn only when the sources fill it" in spec
    assert '"as shown above"' in spec
    # Anchoring holds here like everywhere else.
    assert "as of" in spec.lower()


def test_the_writer_is_told_what_the_panel_holds():
    """Told only that "a panel may be shown", the writer listed the Brexit dates
    again under the timeline. Named, it has something concrete to leave out."""
    spec = " ".join(shapes.spec(shapes.OVERVIEW, ("timeline", "proscons")).split())
    assert "the dated events in order and the arguments for and against" in spec
    assert "Do not list them again" in spec
    # Every facet has a phrase, or the sentence would read "a panel with , and".
    assert all(facet.shown for facet in shapes.FACET_LIST)


def test_the_panel_reminder_sits_next_to_the_question_and_only_there():
    """In the system prompt alone the instruction lost to the default answer
    rules: measured live, the Brexit answer listed six dates under a timeline
    showing them. Next to the question the same run wrote the causes instead."""
    quick = depth_module.get("balanced")
    shaped = prompts.answer("q", [], [], quick, shapes.OVERVIEW, facets=("timeline",))
    assert "already sees a panel with the dated events in order" in shaped[-1]["content"]
    reported = prompts.report("q", [], "", [], shapes.OVERVIEW, facets=("list",))
    assert "the individual items" in reported[-1]["content"]
    # No reminder for any other shape, nor for the compact ruleset, which has
    # dropped the spec it would be reminding of.
    assert "already sees" not in prompts.answer("q", [], [], quick, shapes.FIGURE)[-1]["content"]
    compact = prompts.answer("q", [], [], quick, shapes.OVERVIEW, facets=("list",), compact=True)
    assert "already sees" not in compact[-1]["content"]
