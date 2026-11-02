"""How much work a question is worth, decided before the work starts.

Two routers, and the cheap one runs first.

`triage()` is pure string matching over the raw question: no model call, no
network. It only ever fires when it is *certain*, which in practice means two
cases: a greeting, and a weather question with a place in it. "danke!" costs a
full classifier round-trip today; a forecast costs three model calls, two
searches and five page reads for an answer a structured service returns in a
third of a second. Both of those are the pre-router's job.

`plan()` runs after the classifier, on the two fields it now also answers, and
maps *complexity × requested depth* onto a concrete preset.

One rule holds over both, and it is the reason this can be trusted with the
default setting: **the router may never spend more than it was asked to.** A
depth the user chose explicitly is a ceiling, never a floor, so "Deep" can be
clamped to "Balanced" for a question that reads as ordinary but "Quick" is never
widened into a research run. Every clamp says so in the transcript, a run that
quietly did less than it was asked to is the thing that would make this feature
untrustable.

Inside "Auto" there is no such ceiling to respect, because Auto is this module's
own guess rather than the reader's instruction, and one guess is corrected here:
a question asking for an *explanation* never resolves below Balanced. See
`plan()` for why the cheap preset gets that particular question wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.agent import depth as depth_module
from app.agent import shapes
from app.agent.depth import Depth

# Modes. What the run will actually do, which is more specific than the depth.
DIRECT = "direct"  # answer from the model, no retrieval
LOOKUP = "lookup"  # a structured service has the answer
SEARCH = "search"  # search, maybe read, then write
RESEARCH = "deep"  # plan sub-questions, research them, then report

# Complexity, as the classifier reports it.
SIMPLE = "simple"
STANDARD = "standard"
RESEARCHY = "research"
COMPLEXITIES = frozenset({SIMPLE, STANDARD, RESEARCHY})


@dataclass(frozen=True, slots=True)
class Route:
    """One routing decision. `depth` is already resolved and already clamped:
    nothing downstream needs to know what was asked for versus what was chosen,
    except the footer, which reads `reason`."""

    mode: str
    depth: Depth
    shape: str = shapes.PROSE
    wants_images: bool = False
    place: str = ""
    reason: str = ""
    # The overview's building blocks, set only when `shape` is the overview.
    facets: tuple[str, ...] = ()


# --------------------------------------------------------------------- plan


def plan(classification, requested: Depth) -> Route:
    """Map the classifier's reading onto a concrete budget.

    `requested` may be AUTO; the returned route's depth never is.
    """
    complexity = str(getattr(classification, "complexity", "") or "").strip().lower()
    if complexity not in COMPLEXITIES:
        complexity = STANDARD

    shape = shapes.get(getattr(classification, "answer_shape", ""))
    auto = requested.id == depth_module.AUTO.id

    raised = False
    if auto:
        depth = {
            SIMPLE: depth_module.SIMPLE,
            RESEARCHY: depth_module.DEEP,
        }.get(complexity, depth_module.BALANCED)
        # An explanation is the one answer the cheap end of the ladder cannot
        # produce. Simple is one query, one page and 900 tokens, which is the
        # right budget for a date and the wrong one for "wie funktionieren
        # Autoencoder": the classifier calls that simple, quite reasonably, a
        # definition does settle it, and the reader who asked because they did
        # not understand the definition then gets the definition. Auto is a
        # guess about effort, so this corrects the guess before it is spent;
        # nothing here overrides a preset the reader chose, which is still a
        # ceiling below.
        if shape == shapes.EXPLAINER and depth.id == depth_module.SIMPLE.id:
            depth = depth_module.BALANCED
            raised = True
    else:
        # An explicitly chosen preset is honoured, in both directions. Trimming
        # it down when the classifier calls the question simple is tempting and
        # it does save a search, but it is also how a reader who deliberately
        # asked for a plan-and-report run gets a one-search answer because a
        # cheap router model judged the subject ordinary, with no way to insist.
        # The chip is the one place the reader can outrank the guess, so it has
        # to mean what it says; Auto is where the guess decides.
        depth = requested
    depth = depth_module.resolve(depth)

    if shapes.is_lookup(shape):
        mode = LOOKUP
    elif not getattr(classification, "needs_search", True):
        mode = DIRECT
    elif depth.sub_questions:
        mode = RESEARCH
    else:
        mode = SEARCH

    # The classifier's own reading of "the reader wants to see this", honoured
    # for prose, ignored where the shape already decides. A weather card is what
    # the reader came to see, and the picture search behind it cost a second
    # search and put "bing images" in the footer of a forecast.
    asked_to_see = bool(getattr(classification, "wants_image", False))
    if shape in shapes.WIDGET_SHAPES:
        asked_to_see = False

    shape, facets = _overview(shape, getattr(classification, "facets", ()), mode, depth)
    return Route(
        mode=mode,
        depth=depth,
        shape=shape,
        wants_images=shapes.wants_image(shape) or asked_to_see,
        place=str(getattr(classification, "place", "") or "").strip(),
        reason=_reason(complexity, requested, depth, auto, raised),
        facets=facets,
    )


def _overview(shape: str, named, mode: str, depth: Depth) -> tuple[str, tuple[str, ...]]:
    """Turn a prose answer into an overview when the classifier named facets.

    Three conditions, each a way the panel would cost more than it shows:

    * **A fixed shape wins.** A figure card with a facts panel under it is two
      layouts arguing about which one is the answer.
    * **Only a searched answer.** Every item in the panel must cite a source
      the run registered, and a direct answer registers none, so the panel
      would be refused after its call had been paid for.
    * **Not on the quick presets.** The panel is a second model call, and the
      reader who chose Quick, or the question Auto judged simple, gets a short
      answer that a panel above it would mostly repeat. This is the router
      taking work away, never adding it past what was asked.

    The overview is also never accepted as a shape named directly: without
    facets there is nothing to compose, and an empty panel call is a wasted one.
    """
    if shape == shapes.OVERVIEW:
        shape = shapes.PROSE
    if shape != shapes.PROSE or mode not in (SEARCH, RESEARCH):
        return shape, ()
    if depth.id in (depth_module.QUICK.id, depth_module.SIMPLE.id):
        return shape, ()
    facets = shapes.facets(named)
    return (shapes.OVERVIEW, facets) if facets else (shape, ())


def _reason(
    complexity: str, requested: Depth, depth: Depth, auto: bool, raised: bool = False
) -> str:
    """One line for the transcript. A clamp is always said out loud."""
    if auto:
        if raised:
            return f"Read as a question to explain · {depth.label}"
        return f"Read as a {complexity} question · {depth.label}"
    if depth.id != requested.id:
        return f"Read as a {complexity} question · {requested.label} trimmed to {depth.label}"
    return f"Read as a {complexity} question · {depth.label} as chosen"


def widened(route: Route, requested: Depth) -> Route:
    """The same question, as an ordinary search at full depth.

    Used when a lookup route's service did not answer. The cheap preset was
    justified by the lookup succeeding; without it the question needs the
    reading budget it would have had anyway.
    """
    depth = depth_module.resolve(requested)
    return Route(
        mode=RESEARCH if depth.sub_questions else SEARCH,
        depth=depth,
        shape=route.shape,
        wants_images=route.wants_images,
        place=route.place,
        reason=f"No service answered · searching at {depth.label}",
        facets=route.facets,
    )


# ------------------------------------------------------------------ triage

# Word-bounded. `\bwetter\b` matches "wie ist das Wetter" and misses
# "Wetterballon", "Wetterstation" and "Wettervorhersagemodell": the compound
# nouns are exactly the questions that must reach the full research path.
#
# Split into two strengths because the weak words are ambiguous on their own.
# "Wie hoch ist die Temperatur der Sonne" is a temperature question and not a
# forecast, so a weak word only routes here when the question also names a
# place; a strong word may fall back to the configured home location.
_STRONG_INTENT = re.compile(
    r"\b(wetter|wettervorhersage|weather|forecast|regnet|schneit|raining|snowing)\b",
    re.IGNORECASE,
)
_WEAK_INTENT = re.compile(r"\b(temperatur|temperature|regen|niederschlag|rain)\b", re.IGNORECASE)

# Any of these and the question is about how something *works*, not about what
# it is doing right now. "Wie funktioniert ein Wetterballon" has both a weather
# word and a place-shaped noun, and it is not a lookup.
_EXPLANATION = re.compile(
    r"\b(wie funktioniert|funktionsweise|warum|weshalb|wieso|erklär\w*|entsteh\w*|"
    r"unterschied|bedeutet|definition|geschichte|einfluss|klimawandel|durchschnitt\w*|"
    r"how does|how do|how is .* made|why|explain|difference|history|meaning|works|"
    r"climate|average)\b",
    re.IGNORECASE,
)

# The place, in the three forms a weather question actually takes. Case is not
# required: people type "wetter in reutlingen" in lower case, and demanding a
# capital would mean the pre-router only ever fires for careful typists.
# `_clean_place` is what keeps this honest.
_AFTER_PREPOSITION = re.compile(
    r"\b(?:in|im|für|fuer|for|at|around|bei|auf|über|ueber|von)\s+(.{1,60})",
    re.IGNORECASE,
)
_WEATHER_FIRST = re.compile(r"^\s*(?:wetter|weather)\s+(?:in\s+)?(.{1,60})$", re.IGNORECASE)
_PLACE_FIRST = re.compile(r"^\s*(.{1,40}?)\s+(?:wetter|weather)\s*\??\s*$", re.IGNORECASE)

# Stripped off the tail of an extracted place. These are the words that follow a
# place name in a weather question and are not part of it.
_TIME_WORDS = frozenset(
    {
        "heute",
        "morgen",
        "übermorgen",
        "uebermorgen",
        "jetzt",
        "gerade",
        "aktuell",
        "wochenende",
        "woche",
        "tage",
        "tagen",
        "today",
        "tomorrow",
        "now",
        "tonight",
        "weekend",
        "week",
        "days",
        # Months. "Wie wird das Wetter im August in Berlin" is a question about
        # a season, not about the conditions now, and a month captured as a
        # place would be looked up as one.
        "januar",
        "februar",
        "märz",
        "maerz",
        "april",
        "mai",
        "juni",
        "juli",
        "august",
        "september",
        "oktober",
        "november",
        "dezember",
        "january",
        "february",
        "march",
        "may",
        "june",
        "july",
        "october",
        "december",
        "sommer",
        "winter",
        "herbst",
        "frühling",
        "summer",
        "autumn",
        "spring",
    }
)

# A capture stops here. A conjunction starts a second place, a preposition
# starts a second phrase, and a time word is never part of a name.
_STOP_TOKENS = _TIME_WORDS | {
    "und",
    "oder",
    "and",
    "or",
    "aber",
    "but",
    "wie",
    "ist",
    "wird",
    "war",
    "es",
    "denn",
    "bitte",
    "please",
    # Prepositions end a capture too: "im August in Berlin" must not come back
    # as the three-word name "August in Berlin".
    "in",
    "im",
    "für",
    "fuer",
    "for",
    "at",
    "bei",
    "auf",
    "über",
    "ueber",
    "von",
    "of",
}

# Never a place on their own. A capture that reduces to one of these is a
# capture that found the grammar rather than the name.
_ARTICLES = frozenset(
    {
        "der",
        "die",
        "das",
        "dem",
        "den",
        "des",
        "ein",
        "eine",
        "einer",
        "einem",
        "einen",
        "meiner",
        "meinem",
        "unserer",
        "unserem",
        "the",
        "a",
        "an",
        "my",
        "our",
        "this",
        "dieser",
        "diesem",
        "diese",
    }
)

# Small talk, matched against the *whole* question. Anything with a subject
# after it is a real question and falls through.
_PLEASANTRIES = frozenset(
    {
        "hi",
        "hallo",
        "hey",
        "hej",
        "moin",
        "servus",
        "guten morgen",
        "guten tag",
        "guten abend",
        "good morning",
        "good evening",
        "hello",
        "yo",
        "danke",
        "danke dir",
        "dankeschön",
        "danke schön",
        "vielen dank",
        "thanks",
        "thank you",
        "thx",
        "ty",
        "cheers",
        "ok",
        "okay",
        "alles klar",
        "passt",
        "super",
        "perfekt",
        "perfect",
        "nice",
        "cool",
        "great",
        "tschüss",
        "ciao",
        "bye",
        "goodbye",
        "bis später",
        "gute nacht",
        "good night",
        "wie geht es dir",
        "wie gehts",
        "how are you",
    }
)

# A weather question is short. Anything longer is carrying a second clause, and
# a second clause is a question the pre-router has no business answering.
_MAX_LOOKUP_WORDS = 12


def triage(query: str) -> Route | None:
    """A route decided from the question alone, or None to ask the model.

    Returning None is the default and the safe answer. Every rule here has to
    earn its place by being certain, because a wrong shortcut answers the wrong
    question with no model call in between to catch it.
    """
    text = " ".join(str(query or "").split())
    if not text:
        return None

    stripped = text.strip(" .!?,;:").lower()
    if stripped in _PLEASANTRIES:
        return Route(
            mode=DIRECT,
            depth=depth_module.SIMPLE,
            reason="Small talk · answered without searching",
        )

    if len(text.split()) > _MAX_LOOKUP_WORDS:
        return None
    if _EXPLANATION.search(text):
        return None

    strong = bool(_STRONG_INTENT.search(text))
    if not strong and not _WEAK_INTENT.search(text):
        return None

    place = _place_in(text)
    # A weak word with no place is a temperature question about something that
    # is not a location: the sun, an oven, a fever. Those belong to the search.
    if not place and not strong:
        return None

    return Route(
        mode=LOOKUP,
        depth=depth_module.SIMPLE,
        shape=shapes.WEATHER,
        place=place,
        reason=(
            f"Weather for {place} · fetched, not searched"
            if place
            else "A weather question · fetched, not searched"
        ),
    )


def _place_in(text: str) -> str:
    """The bare place name, or "" when the question did not give one.

    "" is a real answer: the caller falls back to the configured home location
    and, failing that, says so and searches. Guessing a place would be the one
    way this feature could confidently answer about the wrong city.
    """
    for pattern in (_WEATHER_FIRST, _PLACE_FIRST, _AFTER_PREPOSITION):
        match = pattern.search(text)
        if not match:
            continue
        place = _clean_place(match.group(1))
        if place:
            return place
    return ""


_WORDLIKE = re.compile(r"^[^\W\d_][\w.'’\-]*$", re.UNICODE)


def _clean_place(raw: str) -> str:
    """Reduce a captured phrase to a name, or to "" if it is not one.

    Case-insensitive capture means the pattern also matches grammar: "in der
    Stadt", "for the week". Everything that makes a phrase not-a-name is
    filtered here rather than in the pattern, where it would be unreadable.
    """
    kept: list[str] = []
    for token in raw.split():
        word = token.strip(" .,!?;:\"'()")
        low = word.lower()
        if not word or low in _STOP_TOKENS or not _WORDLIKE.match(word):
            break
        if not kept and low in _ARTICLES:
            continue  # a leading article is not part of the name
        kept.append(word)
        if len(kept) == 3:  # "New York City" is the longest shape worth keeping
            break

    while kept and kept[-1].lower() in _TIME_WORDS:
        kept.pop()
    if not kept or all(word.lower() in _ARTICLES for word in kept):
        return ""
    return " ".join(kept)
