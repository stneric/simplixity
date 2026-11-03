"""How an answer should be laid out, when prose is the wrong container.

A price, a forecast and a league table are not prose, and writing them as prose
is how "what is AAPL at" comes back as a paragraph the reader has to parse a
number out of. But bolting a formatting section onto every answer makes the model
tabulate things that are not tables. Instruction dilution is real, and a
three-row table explaining a concept is worse than the sentence it replaced.

So the shape is *decided*, once, by the classifier that already runs, and only
the matching spec reaches the writer. Same pattern as the recency window: one
extra enum on a call that was happening anyway.

**One registry, one place to change.** The two lists in this module are
generated from `SHAPES`, so adding a layout means adding a
`Shape` and a renderer, and `tests/test_widgets.py` fails if the schema half is
missing.

The rule every shape shares is the one that matters. A figure without its unit,
its as-of moment and its source is not research output. It is a number that was
true once. Everything else in this app exists to make an answer checkable, and of
all the things in an answer, a figure rots fastest.
"""

from __future__ import annotations

from dataclasses import dataclass

PROSE = ""
FIGURE = "figure"
SERIES = "series"
TABLE = "table"
IMAGE = "image"
MATCH = "match"
WEATHER = "weather"
DIAGRAM = "diagram"
PROFILE = "profile"
STEPS = "steps"
COMPARISON = "comparison"
EXPLAINER = "explainer"
OVERVIEW = "overview"


@dataclass(frozen=True, slots=True)
class Shape:
    """One answer layout, and everything the run needs to know about it."""

    id: str
    # One bullet for the classifier. Deliberately concrete: "quantitative" is a
    # word models apply to any answer containing a digit.
    guidance: str
    # What the writer is told, once this shape has been chosen.
    spec: str
    # Corpora this shape needs on top of whatever the user selected.
    lenses: tuple[str, ...] = ()
    # Whether the answer wants a retrieved picture. Drives the separate image
    # search; see `research.find_images` for why it is separate.
    wants_image: bool = False
    # Whether that picture is the classifier's call rather than the shape's. A
    # profile of a person or a brand wants one; a profile of ISO 27001 paid for
    # an image search that came back with stock photos of padlocks.
    image_if_asked: bool = False
    # Whether a recency window would starve this answer rather than sharpen it.
    # "Wer ist X" is a question about a person, and the classifier reads it as
    # current-events often enough that restricting to the past year leaves the
    # card with nothing to state but this month's headlines.
    timeless: bool = False
    # Rendered from a validated schema (`widgets.py`) rather than as markdown.
    widget: bool = True
    # A widget this shape may borrow without being that widget's answer. The
    # explainer borrows the diagram, the overview its own panel. A shape that
    # sets this gets the card *if* the sources fill it, and silence if they do
    # not, so the prose can never be written around a box that was refused. See
    # `orchestrator._execute`.
    companion: str = ""
    # The heading the writer's spec arrives under. Empty takes the default for
    # the kind of shape this is, prose around a widget, or a table to lay out.
    heading: str = ""
    # Whether the markdown alignment rules belong in this spec. They are for
    # shapes that write a table; an explanation has none to rag.
    aligns: bool = True
    # Answerable from a structured service without searching at all. The run
    # tries this *first*: a lookup that works costs no search, no page read, no
    # rerank and no widget call. See `orchestrator._try_lookup`.
    lookup: bool = False


# The as-of rule, shared by every spec. Written once because a figure with no
# timestamp is the failure this whole feature is for.
_ANCHORING = """\
Every figure carries its unit and the moment it describes ("as of 12 August
2026", "close on 11 August", "forecast issued 06:00 UTC"), taken from the source,
never from today's date, and cited. If a source gives a number without a date,
say that the date is unknown rather than implying it is current. Never convert,
round beyond the source's own precision, or compute a figure the sources do not
state."""

# Markdown alignment is the one bit of syntax worth asking for explicitly: the
# renderer honours it, and it is what stops a column of numbers from ragging.
_ALIGNMENT = """\
In every table, right-align numeric columns with the `---:` form in the divider
row, and left-align text columns."""


# The explainer's spec, written out here rather than inline because it is the
# longest thing a writer is ever handed and it is the whole of the feature.
#
# The order is the load-bearing part. A model asked to "explain X clearly"
# writes a definition, then a list of components, then a sentence about
# applications, which is the shape of an encyclopaedia entry and is exactly what
# the reader has already failed to understand, that entry is usually what they
# just read. What is missing from it is never the facts. It is the purpose
# before the parts, one worked case, and an admission of where the intuition
# breaks, and none of the three appear unless they are asked for by name.
#
# The citation carve-out below is the one place in this app where a sentence may
# stand without a source, and it is deliberate and narrow. An analogy is not a
# claim about the world and no source can be given for one; a derivation step is
# arithmetic the reader can check themselves. Forbidding them outright, which is
# what the unmodified citation contract does, is what turns an explanation back
# into the encyclopaedia entry. So they are permitted and required to be
# *visibly* what they are, and the rule that every actual assertion about the
# world is cited is restated rather than relaxed.
_EXPLAINING = """\
The reader asked to understand something, not to be told a fact about it. A
correct summary they cannot follow has failed the question, and the summary they
could not follow is usually the encyclopaedia entry they read just before asking
you. So do not write another one. Build the answer in this order, without
announcing the structure and without a heading per part unless it runs long:

1. **The answer in one sentence.** What the thing is or does, in ordinary words,
   with no jargon at all. If a term cannot be avoided, define it in the same
   sentence.
2. **The problem it solves.** What goes wrong without it, or what question it
   was invented to answer. A mechanism explained before its purpose is a parts
   list.
3. **How it actually works**, in the order the thing itself happens. Name each
   step, say in one clause what that step is for, and define every technical
   term at its first use in half a sentence. Never explain one unfamiliar term
   with another you have not defined yet.
4. **One concrete case, worked small.** The smallest real example, a short
   calculation, or an analogy that shares the *mechanism* and not merely the
   mood. Pick whichever the subject actually admits; an analogy that only sets
   an atmosphere is worse than none.
5. **Where the intuition breaks.** The thing readers get wrong here, the point
   at which your own analogy stops holding, what this is commonly confused
   with. One or two sentences, and never omitted, it is the part that makes the
   rest safe to use.

Derive rather than assert. Where the answer has a formula, show where it comes
from: introduce every symbol as it appears, change one thing per step, and say
in words what each step did. Write display maths as `$$…$$` on its own line and
inline maths as `$…$`; both are rendered for the reader.

**An analogy, a worked example and a derivation step carry no citation, and
that is deliberate.** They are not claims about the world, they are the
explaining itself, and there is no source to give for one. They must therefore
be visibly what they are, an analogy introduced as an analogy, an example whose
numbers are plainly stand-ins ("say the input is four pixels"), and they may
never smuggle a fact in: every statement about what is actually the case, every
real figure, name, date, result and attribution, is cited exactly as it is
anywhere else in this app. The anchoring rule below governs those; a number you
made up to carry a calculation states nothing about the world and is not one of
them. Where a step cannot be explained without asserting something the sources
do not carry, say that the sources stop there instead of filling the gap.

A diagram of the parts may be drawn above your answer when the sources describe
how they connect. It is not guaranteed, so never write "as shown above" or build
a sentence that needs it; simply use the sources' own names for the parts, so
that the picture and the text agree when both are there.

Length follows understanding rather than the length of the question. This is the
one kind of answer where a short reply is usually the wrong one, which is not
licence to pad: no preamble, no restating the question, nothing repeated, and no
summary at the end, an explanation that worked needs no recap."""


# The overview's spec names what the panel holds, because the generic version
# did not work. Told only that "a panel may be shown", Gemini Flash wrote the
# Brexit timeline out again as bullets under the timeline, and the EZB rates as
# a list under the facts card: the default answer rules ("the direct answer
# first, then the detail") outweighed an instruction about a panel it could not
# picture. Told that the reader already has *the dated events, in order*, it has
# something concrete to leave out.
#
# The panel is still a separate call and may be refused, so the prose must
# stand alone. That is why it is asked for an argument rather than an
# inventory, and allowed to name one or two items in passing: without the
# panel the reader loses the list, never the answer.
_OVERVIEWING = """\
Above your answer the reader already sees {shown}, each item cited.
Treat those as said. Do not list them again, not as bullets, not as a table, and
not as a run of sentences that each restate one item with its date or value.
Name one or two in passing where the argument needs them, and spend the words on
what a panel cannot carry: why it happened or why it matters, how the pieces
relate, what weighs most for the question as asked, where the sources disagree.

The panel is drawn only when the sources fill it, so never write "as shown
above" or build a sentence that needs it. The answer stands on its own as an
argument, not as an inventory."""


# A shape without a source that holds the number is formatting for an empty
# answer. `figure` and `series` questions therefore add the data lens themselves:
# the user asked for a price, not for a lesson in which corpus carries prices.
# `table` does not: league standings and product comparisons live on the web,
# and Wolfram Alpha has nothing to say about them.
#
# Images are deliberately absent from every `lenses` below. Adding the images
# lens to the main search mixes stock-photo pages into the pool the answer is
# written from, and the prose for "what does the Argentine flag look like" ends
# up cited to Pixabay. A picture is worth nothing as text
# evidence. Pictures are retrieved by their own search afterwards
# (`research.find_images`), so they can never displace evidence.
SHAPE_LIST: tuple[Shape, ...] = (
    Shape(
        id=FIGURE,
        guidance=(
            '  - "figure" for one current value: a share price, an exchange rate, a\n'
            '    population, the temperature right now, "how much/many is X".'
        ),
        spec="""\
The value, its change and its as-of moment are already shown above your answer
as a card. Do not lead by restating the number; the reader has it. Write what
the card cannot carry: what moved it, what it is being compared against, what
the figure means for the question as asked. Two to four sentences, cited. If the
sources only give the bare number, say that and stop.""",
        lenses=("data",),
        # A quote page is old; the quote on it is live. Search engines filter by
        # when they *indexed* a page, so "restricting to the past day" on "was
        # kostet die Apple-Aktie" hid finanzen.net and boerse.de and returned
        # apple.com, icloud.com and the Apple ID login: six sources, no price.
        # The same search without the window returns the quote sites first.
        timeless=True,
    ),
    Shape(
        id=SERIES,
        guidance=(
            '  - "series" for one quantity across time: a price over months, a\n'
            "    population by decade, a temperature by year. Three points at least."
        ),
        spec="""\
The points, the trend and the as-of moment are already shown above your answer
as a chart with its own table. Do not repeat the numbers as a list; the reader
has them. Write what the chart cannot carry: what drove the movement, where the
series is measured from, what changed definition partway through, whether the
sources disagree about a point. Two to four sentences, cited.""",
        lenses=("data",),
    ),
    Shape(
        id=TABLE,
        guidance=(
            '  - "table" for several entities compared on the same attributes:\n'
            "    league standings, three tools rated on the same criteria."
        ),
        spec="""\
Use one table, one row per entity, with the entity in the first column. Same
attributes for every row, units in the headers. Sort by whatever the question
implies (rank, date, size) and say in one sentence what it is sorted by if that
is not obvious. Where sources disagree on a value, put the figure in the cell and
the disagreement in a sentence below the table, not in a footnote the reader has
to hunt for.""",
        widget=False,
    ),
    Shape(
        id=COMPARISON,
        guidance=(
            '  - "comparison" for exactly two things weighed against each other:\n'
            '    "X vs Y", "should I use X or Y". Use "table" for three or more.'
        ),
        spec="""\
The two columns and the attributes they differ on are already shown above your
answer. Do not walk the reader back through the rows; they have them. Write
what the table cannot carry: which one the evidence actually favours and for
whom, what the comparison leaves out, where the two are not really comparable.
Three to five sentences, cited, and say plainly if the sources only cover one
side.""",
    ),
    Shape(
        id=STEPS,
        guidance=(
            '  - "steps" for an instruction or a process the reader is meant to\n'
            '    follow: "how do I install X", "how do I apply for Y". Only when\n'
            "    the sources give the actual steps, in order."
        ),
        spec="""\
The steps are already shown above your answer, numbered and in order. Do not
repeat them; the reader has them. Write what the list cannot carry: what has to
be true before step one, the version or platform this holds for, the place
people get it wrong, what to do when a step fails. Two to five sentences,
cited.""",
        lenses=("docs",),
    ),
    Shape(
        id=WEATHER,
        guidance=(
            '  - "weather" for conditions at a place: the temperature now, today, or\n'
            "    a forecast for the coming days. Put the bare place name in `place`."
        ),
        spec="""\
The conditions and the forecast are already shown above your answer. Do not
restate the temperatures; the reader has them. Write what the panel cannot
carry: what is changing and when, whether the sources disagree, anything worth
knowing that a number does not say. Two to four sentences, cited, naming the
moment the forecast was issued.""",
        lookup=True,
    ),
    Shape(
        id=MATCH,
        guidance=(
            '  - "match" for one fixture between two teams or countries: a result, a\n'
            '    scoreline, an upcoming game. Use "table" instead for a whole round\n'
            "    or a league table."
        ),
        spec="""\
The result is already shown above your answer as a scoreboard. Do not restate
the score as a sentence. Write what the scoreline does not carry: how it was
decided, what was at stake, what it changes. Three sentences at most, cited, and
say plainly if the sources only cover part of it.""",
        lenses=("news",),
    ),
    Shape(
        id=PROFILE,
        guidance=(
            '  - "profile" for one named thing the question asks *about* and wants\n'
            "    identified: a person, an organisation, a brand, a product, a place, a\n"
            "    building, an animal or plant species, a piece of software, a standard:\n"
            '    "who is X", "what is the company Y", "was ist Z", "wer ist Z". The\n'
            "    test is that the answer opens by saying what the thing is; a question\n"
            "    that merely mentions it, or that asks how to use it or whether to buy\n"
            "    it, is not a profile. Neither is a concept, a\n"
            '    mechanism or a piece of jargon: "was ist ein Autoencoder" wants the\n'
            '    idea taught, which is "explainer", and a card of facts about an idea\n'
            "    is a card with nothing on it."
        ),
        spec="""\
The name, the picture and the key facts are already shown above your answer as
a card. Do not open by restating them. Write what the card cannot carry: why
this is the answer to the question, what it is known for or used for, what has
changed recently. Three to five sentences, cited.""",
        lenses=("reference",),
        wants_image=True,
        image_if_asked=True,
        timeless=True,
    ),
    Shape(
        id=IMAGE,
        guidance=(
            '  - "image" when the answer is something to *look at*: what a flag, a\n'
            "    logo, an animal, a building or a painting looks like. The question\n"
            '    is usually "what does X look like", "show me X", "wie sieht X aus".'
        ),
        spec="""\
The picture is already shown above your answer. Do not describe what it depicts
in place of answering. Say what identifies it: the colours and their
arrangement, the proportions, what the elements mean, when it was adopted or
made. Two to five sentences, cited.""",
        wants_image=True,
    ),
    Shape(
        id=DIAGRAM,
        guidance=(
            '  - "diagram" when the answer is a structure or a process with named\n'
            "    parts and the relationships between them: how a request flows\n"
            "    through a system, how a cycle works, what causes what. Only when the\n"
            "    sources actually describe the parts and their connections; never to\n"
            '    decorate a definition. Choose "explainer" instead when the reader\n'
            "    wants the subject taught rather than mapped; that shape draws the\n"
            "    same picture when there is one to draw."
        ),
        spec="""\
The diagram is already shown above your answer. Walk the reader through it in
order, naming the same labels it uses so the text and the picture can be read
together. Do not introduce a step that is not in the diagram, and do not repeat
the diagram as a list.""",
    ),
    Shape(
        id=EXPLAINER,
        guidance=(
            '  - "explainer" when the reader is asking to *understand* something\n'
            "    rather than to be told a fact about it: how a mechanism works, why\n"
            "    something happens, what a concept or a piece of jargon actually\n"
            '    means. "Wie funktionieren Autoencoder", "warum ist der Himmel blau",\n'
            '    "explain transformers", "was bedeutet Impedanz". The test is that a\n'
            "    correct one-line answer would leave the reader no wiser, they asked\n"
            "    because the subject is unfamiliar, not because they mislaid a fact.\n"
            '    A question that merely opens with "what is" and wants a named thing\n'
            '    identified is "profile", and a procedure the reader is meant to\n'
            '    carry out is "steps". Not an explainer either: how events unfolded\n'
            '    ("wie kam es zu X", "was ist seit 2022 passiert", "Geschichte von\n'
            '    X"), a set of things to list ("welche Nebenwirkungen"), or a\n'
            '    decision to weigh ("lohnt sich X"). Those are prose, with `facets`.'
        ),
        spec=_EXPLAINING,
        # Written-down-carefully beats written-recently here, and the
        # encyclopaedic corpus is the one the general web ranks below tutorials
        # that explain the subject worse.
        lenses=("reference",),
        # How a thing works did not change this week. A recency window on "wie
        # funktionieren Autoencoder" buries the paper that introduced them under
        # this month's posts about them.
        timeless=True,
        widget=False,
        companion=DIAGRAM,
        heading="Explaining this so it is understood",
        aligns=False,
    ),
    # Not offered to the classifier as an `answer_shape`, which is why it has no
    # guidance: it is what an ordinary prose answer becomes when the classifier
    # names `facets` for it (see `FACET_LIST` below and `route.plan`). The fixed
    # shapes above always win; this is the space between them and bare prose.
    Shape(
        id=OVERVIEW,
        guidance="",
        spec=_OVERVIEWING,
        widget=False,
        companion=OVERVIEW,
        heading="Writing beside an overview",
        aligns=False,
    ),
)

SHAPES_BY_ID: dict[str, Shape] = {shape.id: shape for shape in SHAPE_LIST}
SHAPES = frozenset(SHAPES_BY_ID)
# Shapes rendered as a widget from a validated schema rather than as markdown.
# The prose still runs afterwards; the widget is what the reader sees first.
WIDGET_SHAPES = frozenset(shape.id for shape in SHAPE_LIST if shape.widget)

# Every widget the run can draw: the shapes that are one, and the ones a shape
# borrows. `widgets._WIDGETS` is held in lockstep with this, not with
# `WIDGET_SHAPES`, because a borrowed card is still a schema to fill.
CARDS = WIDGET_SHAPES | frozenset(shape.companion for shape in SHAPE_LIST if shape.companion)


@dataclass(frozen=True, slots=True)
class Facet:
    """One building block of an overview panel.

    The shapes above are whole layouts, chosen when the answer *is* a price or a
    scoreboard. Most answers are not, and still have a part that is structure
    rather than argument: the dates in "how did the strike come about", the
    trade-offs in "is a heat pump worth it". A facet is that part. The
    classifier names up to `MAX_FACETS` of them, a model composes the panel
    from those alone, and each block has one fixed renderer, so the layout is
    decided per answer while what reaches the screen is still validated data.

    The vocabulary is closed. An open one is a model writing layout,
    which is the thing `widgets.py` exists to stop.
    """

    id: str
    guidance: str
    # What the writer is told the reader already has, in a phrase that fits
    # "a panel with …". See `_OVERVIEWING`.
    shown: str = ""
    # The fewest items this block needs to be the thing it claims to be. Two
    # dates are not a timeline, and one fact is a sentence in a box.
    min_items: int = 2
    # And the most. A stat row past four tiles wraps into a grid of numbers,
    # and a callout is one caveat or it is a list.
    max_items: int = 8


FACT_BLOCK = "facts"
STATS_BLOCK = "stats"
TIMELINE_BLOCK = "timeline"
PROSCONS_BLOCK = "proscons"
LIST_BLOCK = "list"
GRID_BLOCK = "grid"
CALLOUT_BLOCK = "callout"

FACET_LIST: tuple[Facet, ...] = (
    Facet(
        FACT_BLOCK,
        '  - "facts" for the key attributes of the subject as label and value,\n'
        "    dates, sizes, owners, versions, places, where the reader would\n"
        "    otherwise scan a paragraph for them.",
        shown="the key data as label and value",
    ),
    Facet(
        STATS_BLOCK,
        '  - "stats" for one to four headline numbers the answer turns on, each\n'
        "    with its unit and its date.",
        min_items=1,
        max_items=4,
        shown="the headline figures",
    ),
    Facet(
        TIMELINE_BLOCK,
        '  - "timeline" for events in order, how something came about, what\n'
        "    happened when. Three dated events at least.",
        min_items=3,
        shown="the dated events in order",
    ),
    Facet(
        PROSCONS_BLOCK,
        '  - "proscons" for arguments on both sides, advantages and drawbacks,\n'
        '    "is X worth it", "lohnt sich X", "should I".',
        shown="the arguments for and against",
    ),
    # The loosest block, and the one most often chosen for the wrong reason.
    # Almost any answer can be cut into bullets, and a panel of "reasons" or
    # "factors" is the argument chopped into fragments with the connections
    # removed, shown above the prose that then makes the same points properly.
    # So the list is for things the reader would look up one by one, each with a
    # name of its own, and three at least: two items are a sentence.
    Facet(
        LIST_BLOCK,
        '  - "list" for a set of distinct named things the reader would look up\n'
        "    one by one: the side effects of a drug, the documents an application\n"
        "    needs, the alternatives to a product. Each item has a name of its\n"
        "    own, and there are three at least. Reasons, causes, factors, tips\n"
        "    or the points of an explanation are not a list, they are the\n"
        "    argument, and belong in the prose.",
        min_items=3,
        shown="the individual items, one per line",
    ),
    Facet(
        GRID_BLOCK,
        '  - "grid" for a few items on the same two to four attributes when that\n'
        "    is only part of the answer; when the table *is* the answer, use the\n"
        '    "table" shape instead.',
        shown="the items compared side by side",
    ),
    Facet(
        CALLOUT_BLOCK,
        '  - "callout" for the one caveat the reader must not miss, a deadline, a\n'
        "    safety note, the common mistake.",
        min_items=1,
        max_items=1,
        shown="the one caveat not to miss",
    ),
)
FACETS_BY_ID: dict[str, Facet] = {facet.id: facet for facet in FACET_LIST}
FACETS = frozenset(FACETS_BY_ID)
FACETS_ORDER = tuple(facet.id for facet in FACET_LIST)
# Past three blocks a panel is a second answer above the first, and the reader
# has to decide which one to read.
MAX_FACETS = 3

# What the classifier is told, assembled from the registry so a new shape cannot
# exist without being offered. The overview has no guidance line: it is reached
# through `facets`, never named as a shape.
CLASSIFIER_GUIDANCE = "\n".join(
    [
        "Set `answer_shape` when the answer has a better container than a paragraph,",
        "so it can be laid out rather than written as prose:",
        *(shape.guidance for shape in SHAPE_LIST if shape.guidance),
        "",
        "Leave it empty for everything else. A three-row table explaining a concept",
        "is worse than the sentence it replaced, and a diagram of a definition is",
        "worse still: a shape is chosen because the answer has that form, never",
        "because the subject sounds like it should.",
        "",
        "When `answer_shape` is empty, `facets` may name the parts of the answer",
        "that are structure rather than argument, so they can be laid out in a",
        "panel above the prose. Most important first, at most three:",
        *(facet.guidance for facet in FACET_LIST),
        "",
        "Leave `facets` empty for most questions, and always when `answer_shape` is",
        "set. Name a facet only when the sources will state that structure, an answer",
        "that is one explanation or one argument has none, and a panel that merely",
        "repeats the prose is noise. The usual matches: a history or a sequence of",
        'events is "timeline", a decision is "proscons", side effects, required',
        'documents or named alternatives are "list", a subject with key data is',
        '"facts". "Why does X happen" or "how does X work" names no facet: its',
        "parts are steps of one argument, and a list of them is worse than prose.",
    ]
)


def definition(name: str | None) -> Shape | None:
    """The registry entry, or None for prose."""
    return SHAPES_BY_ID.get(get(name))


def lenses_for(shape: str) -> tuple[str, ...]:
    """Corpora this shape needs on top of whatever the user selected."""
    entry = definition(shape)
    return entry.lenses if entry else ()


def wants_image(shape: str) -> bool:
    entry = definition(shape)
    return bool(entry and entry.wants_image)


def picture_for(shape: str, asked: bool) -> bool:
    """Whether this answer runs the image search, given what the classifier said.

    A shape that is always a picture gets one. A widget that draws its own card
    (a forecast, a scoreboard) never does, whatever the classifier said: the
    card is what the reader came to see. Everywhere else, prose included, the
    classifier's `wants_image` decides.
    """
    entry = definition(shape)
    if entry is None or not entry.widget or entry.image_if_asked:
        return asked
    return entry.wants_image


def is_timeless(shape: str) -> bool:
    entry = definition(shape)
    return bool(entry and entry.timeless)


def is_lookup(shape: str) -> bool:
    entry = definition(shape)
    return bool(entry and entry.lookup)


def get(name: str | None) -> str:
    """Resolve a shape, falling back to prose rather than failing. An unknown
    value from a model that improvised must degrade, not 500."""
    value = (name or "").strip().lower()
    return value if value in SHAPES_BY_ID else PROSE


def companion(shape: str) -> str:
    """The widget this shape may borrow, or "".

    Distinct from *being* that widget's shape: the borrower's prose is written
    without knowing whether the card arrived, so a refusal costs it nothing.
    """
    entry = definition(shape)
    return entry.companion if entry else ""


def card_for(shape: str) -> tuple[str, bool] | None:
    """The widget to build for this shape, and whether it was promised.

    A shape that is a widget gets its own card, and its absence is worth a word
    to the reader. A shape that borrows one, the explainer's diagram, the
    overview's panel, gets the companion, and its prose stands without it, so a
    refusal there is silent. None for a shape that draws nothing.
    """
    if shape in WIDGET_SHAPES:
        return shape, True
    borrowed = companion(shape)
    return (borrowed, False) if borrowed else None


def panel_contents(facets: tuple[str, ...]) -> str:
    """What an overview panel holds, as a phrase: "a panel with the dated events
    in order and the arguments for and against"."""
    phrases = [FACETS_BY_ID[facet].shown for facet in facets if facet in FACETS_BY_ID]
    if not phrases:
        return "a panel with the key points laid out"
    if len(phrases) == 1:
        return f"a panel with {phrases[0]}"
    return "a panel with " + ", ".join(phrases[:-1]) + " and " + phrases[-1]


def facets(names) -> tuple[str, ...]:
    """The classifier's facets, cleaned: known, unique, in order, capped.

    An unknown name is dropped rather than failing, the same rule as `get`: a
    model that improvised a block kind costs that block, not the panel.
    """
    picked: list[str] = []
    for name in names or ():
        value = str(name or "").strip().lower()
        if value in FACETS and value not in picked:
            picked.append(value)
    return tuple(picked[:MAX_FACETS])


def spec(shape: str, facets: tuple[str, ...] = ()) -> str:
    """The writer's layout instructions, or "" for ordinary prose.

    `facets` matter only for the overview, whose spec names what its panel
    holds; every other shape's spec is fixed.
    """
    entry = definition(shape)
    if entry is None:
        return ""
    # A widget shape writes prose *around* something already on the page; a
    # markdown shape lays the figures out itself. The heading says which, and a
    # shape that is neither, an explanation, names its own.
    heading = entry.heading or (
        "Writing alongside what is shown" if entry.widget else "Presenting the figures"
    )
    # No alignment rules for a widget: that prose has no table to align. None
    # for an explanation either, for the same reason.
    text = entry.spec
    if shape == OVERVIEW:
        text = text.format(shown=panel_contents(facets))
    parts = [f"## {heading}", text]
    if entry.aligns and not entry.widget:
        parts.append(_ALIGNMENT)
    parts.append(_ANCHORING)
    return "\n\n".join(parts)
