"""Depth presets.

Depth is expressed as *budgets*, never as an iteration count. The fork's
"quality" mode allows 25 tool-loop iterations with no wall clock, which is how a
single question runs for twenty minutes and returns nothing.

Each preset answers four questions: how wide to search, how much to read, how
much evidence reaches the model, and when to stop regardless.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace


@dataclass(frozen=True, slots=True)
class Depth:
    id: str
    label: str
    description: str

    # Retrieval width
    queries: int  # search queries the classifier may produce
    # How many results per query survive to be *ranked*. Not how many are cited:
    # `max_sources` is that, and it is applied afterwards, so this number costs
    # no network and no model call. Set as though it were the source budget, it
    # does real damage, measured on "wann waren wir zuletzt auf dem Mond": the
    # engines returned 110 hits across two queries, the first six of each were
    # kept *in SearXNG's own order*, and the answer was written from
    # three sources, two of them the same article on one museum's site. Every
    # signal this app computes, provenance and agreement and relevance alike, was
    # choosing between six results a metasearch engine had already picked.
    results_per_query: int
    max_sources: int  # citable sources kept for the whole run
    fetch_pages: int  # pages read in full; 0 means snippets only
    passages: int  # ranked chunks handed to the writer

    # Deep-only structure
    sub_questions: int  # 0 disables the plan/parallel/gap shape
    max_rounds: int  # follow-up research rounds after the first

    # Hard stops
    wall_clock_s: float
    max_llm_calls: int
    answer_tokens: int

    # One evidence-informed second look, for the presets that have no plan.
    # Every query in a run is written *before* anything has been retrieved, so a
    # question whose answer needs a term the model could not guess gets searched
    # for once, badly, and answered from whatever that returned. This is the
    # only step that gets to write a query knowing what the first one found.
    gap_check: bool = False

    # One call after the answer is written, checking each cited sentence against
    # the source it cites (`agent/verify.py`). On for every preset, including the
    # cheap ones: an answer built on six snippets is the one most worth checking,
    # not the one least worth it. The field exists so a deployment can turn the
    # cost off, not because any preset here declines it.
    verify: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


QUICK = Depth(
    id="quick",
    label="Quick",
    description="Two searches, snippets only. Seconds, not minutes.",
    queries=2,
    results_per_query=18,
    max_sources=6,
    fetch_pages=0,
    passages=8,
    sub_questions=0,
    max_rounds=0,
    wall_clock_s=90.0,
    max_llm_calls=7,
    answer_tokens=1200,
)

BALANCED = Depth(
    id="balanced",
    label="Balanced",
    description="Reads the best pages before answering. The everyday setting.",
    queries=3,
    results_per_query=24,
    max_sources=10,
    fetch_pages=5,
    passages=14,
    sub_questions=0,
    max_rounds=0,
    # The everyday preset is where the second look matters: Deep already
    # re-plans against its own findings, Quick and Simple are meant to be one
    # shot and one shot only.
    gap_check=True,
    wall_clock_s=240.0,
    max_llm_calls=13,
    answer_tokens=2000,
)

DEEP = Depth(
    id="deep",
    label="Deep",
    description="Plans sub-questions, researches them in parallel, then writes a report.",
    queries=3,
    results_per_query=24,
    max_sources=28,
    fetch_pages=4,  # per sub-question
    passages=28,
    sub_questions=5,
    max_rounds=1,
    wall_clock_s=720.0,
    max_llm_calls=46,
    answer_tokens=4000,
)

AUTO = Depth(
    id="auto",
    label="Auto",
    description="Matches the effort to the question. A lookup stays a lookup.",
    # Never spent. `agent/route.plan()` resolves AUTO into one of the presets
    # below before a Budget is built, and `resolve()` is the belt to that
    # brace: the numbers here mirror BALANCED so that a path which somehow
    # skipped the router degrades to the everyday setting rather than to
    # nonsense.
    queries=3,
    results_per_query=24,
    max_sources=10,
    fetch_pages=5,
    passages=14,
    sub_questions=0,
    max_rounds=0,
    wall_clock_s=240.0,
    max_llm_calls=13,
    answer_tokens=2000,
)

# Below QUICK, and not user-selectable: the router derives it for a question it
# has judged simple. Derived from QUICK with `replace` rather than written out,
# so a change to the cheap end of the ladder cannot forget it.
#
# 45s is a *ceiling*, not a target. A five-second lookup is not slowed by it.
# It works because `Budget` scales the writer's reserve to the preset (see
# budget.WRITE_RESERVE_S); at a flat 45s reserve this preset would forbid
# research outright.
SIMPLE = replace(
    QUICK,
    id="simple",
    label="Simple",
    description="One search, the best page read. For questions with one answer.",
    queries=1,
    results_per_query=18,
    max_sources=6,
    # One page, because "simple" means *one good source settles it*, and a
    # source that is never opened settles nothing. Asked what DiCaprio's parents
    # did, a run at this preset put both Wikipedia articles in its source list,
    # read neither, and answered that the sources do not say. The fetch costs a
    # second or two; a wrong "not stated" costs the answer.
    fetch_pages=1,
    passages=6,
    # The thinnest evidence in the app is here (a single query, however many
    # results it ranks), so this is where a query written *after* seeing them is
    # worth the most: one query can miss the subject entirely, and no amount of
    # ranking rescues a search for the wrong thing. It
    # is also where the router sends "what did X's parents do", which is a
    # simple question with an answer no first query can name. One extra call is
    # budgeted for it; without the raise, `can_research` reserves the last call
    # for the writer and the check could never be paid for.
    gap_check=True,
    # 90s, matching QUICK, because this preset does not do one search and stop:
    # it reads a page and may search a second time. At 45s the
    # reserve is 22.5s, and a run that spent 25s on those two steps had
    # `can_research` refuse the widget, so "was kostet die Apple-Aktie" got a
    # paragraph about a trading venue and no card with the price on it. Still a
    # ceiling, not a target: the lookups that finish in five seconds are
    # unaffected.
    wall_clock_s=90.0,
    max_llm_calls=5,
    answer_tokens=900,
)

# What a request may name. SIMPLE is absent: it is a routing
# decision, not a setting, and offering it would make "Auto" mean two things.
DEPTHS: dict[str, Depth] = {d.id: d for d in (AUTO, QUICK, BALANCED, DEEP)}
DEFAULT_DEPTH = AUTO.id


def get(name: str | None) -> Depth:
    """Resolve a depth id, falling back rather than failing: an unknown value
    from an old client should degrade, not 500."""
    return DEPTHS.get((name or "").lower(), DEPTHS[DEFAULT_DEPTH])


def resolve(depth: Depth) -> Depth:
    """A concrete preset. AUTO is a request, not a budget, and nothing that
    spends may be handed one."""
    return BALANCED if depth.id == AUTO.id else depth


def catalog() -> list[dict]:
    """What the UI needs to render the depth selector, straight from the source
    of truth. No duplicated copy in the frontend."""
    return [
        {"id": d.id, "label": d.label, "description": d.description, "deep": d.sub_questions > 0}
        for d in (AUTO, QUICK, BALANCED, DEEP)
    ]
