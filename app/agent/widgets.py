"""Structured answers: a schema the model fills in, not a layout it must write.

Asking a model to emit a widget as markdown makes the *model* responsible for
the presentation, and a small model will get the syntax subtly wrong: a missing
divider row, a stray bold, a table with four columns in one row and three in the
next. The output is then either broken or silently plain text, and there is no
way to tell which without reading it.

So the model is asked for **data**, against a schema, and the server does the
presenting. A weak model that writes clumsy prose still produces a correct
widget, because the only thing it had to get right was a handful of named
fields. If it gets even that wrong, validation fails and the run falls back
to prose rather than rendering something broken.

*Not* OpenAI tool-calling. Free and local models (exactly the ones
this is meant to serve) support it inconsistently, while `LLM.structured()`
already has the fallback ladder that matters: native JSON-schema mode, then a
prompt carrying the schema. That ladder is the whole reason this is reliable on
a model that has never seen a tool definition.

**Nothing here may invent evidence.** Every widget names the source it came
from, and the index is checked against the sources the run actually registered.
An image is stronger still: the model may not name a URL at all, only the number
of a result that already carries one, so a picture in an answer is always a
picture something retrieved, never one a model remembered.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, get_args, get_origin

from annotated_types import MaxLen
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.fields import FieldInfo

from app.agent import blocks, prompts, shapes
from app.agent.context import RunContext
from app.embed.rerank import Passage
from app.llm import LLMError
from app.search.base import SearchResult

log = logging.getLogger(__name__)

# A widget is a small structured call; it does not need the answer's budget.
WIDGET_TOKENS = 700

# A diagram past this size is a wiring chart nobody reads, and the layout has to
# fit the width of an answer. Refusing an oversized one is better than drawing it
# unreadably small.
MAX_NODES = 12
MAX_EDGES = 20

# A series past two dozen points is a chart that wants axes, and this one has
# none by design. Rows in a comparison or a procedure are capped where a reader
# stops reading them as a unit.
MAX_POINTS = 24
MIN_POINTS = 3
MAX_ROWS = 8
MIN_ROWS = 2
# A grid past four attribute columns does not fit the width of an answer beside
# the column that names each row.
MAX_COLUMNS = 4

_WHITESPACE = re.compile(r"\s+")
# Width of one string inside a list of strings, a grid cell or a column name.
# A list field's `max_length` caps how many there are, not how long each is.
LIST_ITEM_CHARS = 60


def _tidy(value: Any, limit: int | None = None) -> str:
    """One line of text: whitespace collapsed, cut to `limit` if there is one."""
    text = _WHITESPACE.sub(" ", str(value)).strip()
    return text[:limit] if limit else text


def _fit(value: Any, field: FieldInfo) -> Any:
    """Coerce one raw value toward its field's type, the forgiving way.

    Aimed at what models actually send rather than at what the schema says. A
    temperature written as `18` rather than `"18"`, or a `null` where the schema
    wanted `""`, would fail validation and cost the whole card: pydantic does
    not turn a number into a string, and a missing value is not an empty one.
    Native schema mode rarely does this; prompt mode, which is where the small
    and local models this layer is for end up, does it all the time.

    Anything this does not recognise is passed through for pydantic to judge.
    """
    annotation = field.annotation
    limit = next((rule.max_length for rule in field.metadata if isinstance(rule, MaxLen)), None)
    if annotation is str:
        if value is None:
            return ""
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            return _tidy(value, limit)
        return value
    if annotation is int and value is None:
        return 0
    if get_origin(annotation) is list:
        if value is None:
            return []
        if not isinstance(value, list):
            return value
        items = value[:limit] if limit else value
        if get_args(annotation) == (str,):
            return [_tidy(item, LIST_ITEM_CHARS) for item in items if item is not None]
        return items
    return value


class Filled(BaseModel):
    """Base for every widget schema: too long is trimmed, never refused.

    `max_length` on a field reads as a layout rule ("this cell is 80 characters
    wide"), but pydantic enforces it as a rejection, and a rejection here costs
    the entire card. That is not theoretical: a model answering "who is Ada
    Lovelace" wrote one fact of 92 characters, lost the widget, and the retry
    cost a second round-trip to be told the same thing again. Nothing about the
    answer was wrong; a sentence was four words long.

    So the caps are applied *before* validation and mean what they look like:
    strings are tidied to the limit, lists are cut to it, and the near-misses of
    type a model produces are coerced (see `_fit`). What still fails is what a
    trim cannot fix (a missing name, a source the run never saw), which is where
    refusing is the right answer.

    This is also the one place text is tidied, so no schema needs a whitespace
    validator of its own with a limit that disagrees with its `max_length`.
    """

    @model_validator(mode="before")
    @classmethod
    def _fit_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        fitted = dict(data)
        for name, field in cls.model_fields.items():
            # A field may arrive under its alias, `from` for an edge's `source`.
            key = name if name in fitted else field.alias
            if key in fitted:
                fitted[key] = _fit(fitted[key], field)
        return fitted


@dataclass(frozen=True, slots=True)
class Evidence:
    """What a card is checked against: the sources this run registered.

    A plain value rather than the run context, so a schema can judge itself
    without knowing what a run is, and a test can hand it three sources and
    nothing else.
    """

    results: dict[int, SearchResult]
    # The overview's building blocks, the only ones its panel may use.
    facets: tuple[str, ...] = ()

    @classmethod
    def of(cls, ctx: RunContext) -> Evidence:
        return cls(dict(ctx.sources.all()), ctx.facets or shapes.FACETS_ORDER)

    def knows(self, index: int) -> bool:
        """Whether `index` names a source the run actually registered. A
        hallucinated `[9]` fails here rather than appearing next to a number."""
        return index in self.results

    def picture(self, index: int, caption: str = "") -> dict[str, Any] | None:
        """The retrieved picture behind a source number, or None.

        The model names a number, never an address, so every picture on screen
        is one the search actually returned.
        """
        result = self.results.get(index)
        if not index or result is None or not result.image:
            return None
        return picture_payload(index, result, caption)


def picture_payload(index: int, result: SearchResult, caption: str = "") -> dict[str, Any]:
    """An image block's data. The URL always comes from the retrieved result."""
    return {
        "source": index,
        "caption": caption,
        "url": result.image,
        "thumbnail": result.thumbnail or result.image,
        "title": result.title,
        "domain": result.domain,
        "page_url": result.url,
    }


class Card(Filled):
    """A top-level widget schema: something the model fills and the run shows.

    Each card judges itself. The rules differ, a series needs three points, a
    comparison two named sides, a diagram a connected graph, and they used to
    live in one function that asked every object which class it was. Here they
    sit next to the fields they are about.
    """

    def payload(self, evidence: Evidence, kind: str) -> tuple[dict[str, Any] | None, str]:
        """The block data, or None and, where the reader deserves one, a reason.

        A citation is required for the same reason the prose requires one: a
        figure the reader cannot check is not an answer.
        """
        raise NotImplementedError

    def _cited(self, evidence: Evidence, kind: str, **overrides: Any) -> tuple[dict | None, str]:
        """The common ending: refuse an unknown source, else dump with overrides."""
        source = getattr(self, "source", 0)
        if not evidence.knows(source):
            log.info("widget %s cited source %s, which this run has not seen", kind, source)
            return None, ""
        return {**self.model_dump(), **overrides, "kind": kind}, ""


class Row(Filled):
    """One qualifying figure: `previous close` / `$304.91`."""

    label: str = Field(default="", max_length=60)
    value: str = Field(default="", max_length=80)


class Figure(Card):
    """One quantity, the way a reader wants to see it: the number first.

    `value` stays a string. Coercing to a float would lose "3.782 million" and
    "€86.62", and inviting the model to normalise a number is inviting it to
    compute one, which is the failure this whole layer exists to prevent.
    """

    label: str = Field(default="", max_length=80)
    value: str = Field(default="", max_length=60)
    unit: str = Field(default="", max_length=24)
    change: str = Field(default="", max_length=48)
    direction: str = Field(default="")
    as_of: str = Field(default="", max_length=60)
    rows: list[Row] = Field(default_factory=list, max_length=6)
    source: int = 0

    @field_validator("direction")
    @classmethod
    def _known_direction(cls, value: str) -> str:
        return value if value in ("up", "down", "flat") else ""

    def payload(self, evidence: Evidence, kind: str) -> tuple[dict | None, str]:
        if not self.value or not self.label:
            return None, ""
        return self._cited(evidence, kind)


class Side(Filled):
    """One team. `code` is ISO 3166-1 alpha-2 for a country, "" for a club.

    The code is what produces the flag, and it is the one field a model cannot
    fabricate an image with: an unknown code renders no flag rather than a wrong
    one.
    """

    name: str = Field(default="", max_length=60)
    # No `max_length`: a model writing "DEU" or "GER" should lose its flag, not
    # fail the whole widget. Rejecting the value here would throw away a correct
    # scoreline over a wrong country code.
    code: str = Field(default="")
    score: str = Field(default="", max_length=12)

    @field_validator("code")
    @classmethod
    def _iso(cls, value: str) -> str:
        code = str(value or "").strip().lower()
        return code if len(code) == 2 and code.isalpha() else ""


class Match(Card):
    home: Side = Field(default_factory=Side)
    away: Side = Field(default_factory=Side)
    competition: str = Field(default="", max_length=80)
    played_at: str = Field(default="", max_length=60)
    status: str = Field(default="")
    note: str = Field(default="", max_length=120)
    source: int = 0

    @field_validator("status")
    @classmethod
    def _known_status(cls, value: str) -> str:
        return value if value in ("final", "live", "scheduled") else ""

    def payload(self, evidence: Evidence, kind: str) -> tuple[dict | None, str]:
        if not self.home.name or not self.away.name:
            return None, ""
        return self._cited(evidence, kind)


# A small, closed vocabulary. Every value maps to an icon the app already
# vendors, so a condition the model improvises renders nothing rather than a
# broken image, and the set is short enough that a small model can hit it.
CONDITIONS = (
    "clear",
    "partly-cloudy",
    "cloudy",
    "fog",
    "drizzle",
    "rain",
    "showers",
    "snow",
    "sleet",
    "thunderstorm",
    "wind",
)


class Day(Filled):
    """One day of a forecast."""

    day: str = Field(default="", max_length=24)
    high: str = Field(default="", max_length=12)
    low: str = Field(default="", max_length=12)
    condition: str = Field(default="")
    precipitation: str = Field(default="", max_length=16)

    @field_validator("condition")
    @classmethod
    def _known(cls, value: str) -> str:
        return _condition(value)


class Weather(Card):
    """Conditions for one place: now, and the days after.

    Temperatures stay strings for the same reason a price does: the source's
    own precision is what gets shown, and a model asked to normalise a number is
    a model one step from computing one.
    """

    location: str = Field(default="", max_length=80)
    temperature: str = Field(default="", max_length=12)
    unit: str = Field(default="", max_length=8)
    condition: str = Field(default="")
    summary: str = Field(default="", max_length=80)
    feels_like: str = Field(default="", max_length=12)
    wind: str = Field(default="", max_length=24)
    humidity: str = Field(default="", max_length=12)
    as_of: str = Field(default="", max_length=60)
    days: list[Day] = Field(default_factory=list, max_length=7)
    source: int = 0

    @field_validator("condition")
    @classmethod
    def _known(cls, value: str) -> str:
        return _condition(value)

    def payload(self, evidence: Evidence, kind: str) -> tuple[dict | None, str]:
        # A forecast with neither a temperature now nor a single day is not a
        # forecast; a location-less one is a temperature for nowhere.
        if not self.location or not (self.temperature or self.days):
            return None, ""
        return self._cited(evidence, kind)


def _condition(value: str) -> str:
    """Map to the closed vocabulary, or "". Never to a guess.

    Tolerant of the near-misses models actually produce ("sunny", "partly
    cloudy", "rainy") because rejecting those would drop the icon from an
    otherwise correct forecast.
    """
    text = str(value or "").strip().lower().replace("_", "-").replace(" ", "-")
    if text in CONDITIONS:
        return text
    aliases = {
        "sunny": "clear",
        "sun": "clear",
        "fair": "clear",
        "mostly-clear": "clear",
        "partly-sunny": "partly-cloudy",
        "mostly-cloudy": "cloudy",
        "overcast": "cloudy",
        "mist": "fog",
        "haze": "fog",
        "rainy": "rain",
        "light-rain": "drizzle",
        "heavy-rain": "showers",
        "rain-showers": "showers",
        "snowy": "snow",
        "storm": "thunderstorm",
        "thunder": "thunderstorm",
        "thunderstorms": "thunderstorm",
        "windy": "wind",
    }
    return aliases.get(text, "")


class Point(Filled):
    """One reading in a series: `2024` / `4.7`.

    Both stay strings, for the reason `Figure.value` does: the source's own
    precision is what gets shown, and a model asked to normalise a number is a
    model one step from computing one. The client parses `value` leniently for
    the curve only; a point it cannot parse still appears in the table.
    """

    at: str = Field(default="", max_length=24)
    value: str = Field(default="", max_length=24)


class Series(Card):
    """One quantity across time. Rendered as a sparkline over its own table."""

    label: str = Field(default="", max_length=80)
    unit: str = Field(default="", max_length=24)
    points: list[Point] = Field(default_factory=list, max_length=MAX_POINTS)
    change: str = Field(default="", max_length=48)
    direction: str = Field(default="")
    as_of: str = Field(default="", max_length=60)
    source: int = 0

    @field_validator("direction")
    @classmethod
    def _known_direction(cls, value: str) -> str:
        return value if value in ("up", "down", "flat") else ""

    def payload(self, evidence: Evidence, kind: str) -> tuple[dict | None, str]:
        # Two points are a comparison, not a series, and a curve through them is
        # a straight line pretending to be a trend.
        points = [point for point in self.points if point.at and point.value]
        if len(points) < MIN_POINTS:
            return None, ""
        return self._cited(evidence, kind, points=[p.model_dump() for p in points])


class Column(Filled):
    """One side of a comparison. No picture in this version: two logos in a
    three-column grid is a layout problem for a widget whose job is the rows."""

    name: str = Field(default="", max_length=60)


class CompareRow(Filled):
    """One attribute, and what each side has for it."""

    label: str = Field(default="", max_length=48)
    left: str = Field(default="", max_length=90)
    right: str = Field(default="", max_length=90)


class Comparison(Card):
    """Two things weighed on the same attributes."""

    title: str = Field(default="", max_length=80)
    left: Column = Field(default_factory=Column)
    right: Column = Field(default_factory=Column)
    rows: list[CompareRow] = Field(default_factory=list, max_length=MAX_ROWS)
    source: int = 0

    def payload(self, evidence: Evidence, kind: str) -> tuple[dict | None, str]:
        rows = [row for row in self.rows if row.label and (row.left or row.right)]
        if not self.left.name or not self.right.name or len(rows) < MIN_ROWS:
            return None, ""
        return self._cited(evidence, kind, rows=[r.model_dump() for r in rows])


class Step(Filled):
    """One instruction. `text` is the imperative, `detail` the qualification."""

    text: str = Field(default="", max_length=140)
    detail: str = Field(default="", max_length=200)


class Steps(Card):
    """A procedure, in order. Named for the block kind, not for `context.Steps`,
    which is the transcript's sub-steps and has nothing to do with this."""

    title: str = Field(default="", max_length=80)
    steps: list[Step] = Field(default_factory=list, max_length=MAX_ROWS)
    source: int = 0

    def payload(self, evidence: Evidence, kind: str) -> tuple[dict | None, str]:
        steps = [step for step in self.steps if step.text]
        if len(steps) < MIN_ROWS:
            return None, ""
        return self._cited(evidence, kind, steps=[s.model_dump() for s in steps])


class Profile(Card):
    """One named thing (person, organisation, place, product) as an identity card.

    `image_source` follows the same rule as `Picture`: a number, never a URL.
    Unlike `Picture` a missing image is not a failure: a profile without a
    portrait is a profile, so the card renders without one.
    """

    name: str = Field(default="", max_length=80)
    subtitle: str = Field(default="", max_length=80)
    image_source: int = 0
    facts: list[Row] = Field(default_factory=list, max_length=6)
    source: int = 0

    def payload(self, evidence: Evidence, kind: str) -> tuple[dict | None, str]:
        facts = [fact for fact in self.facts if fact.label and fact.value]
        if not self.name or len(facts) < MIN_ROWS:
            return None, ""
        # No portrait is not a failure; the card lays out without one. What is
        # refused is a portrait the run did not retrieve.
        image = evidence.picture(self.image_source)
        if self.image_source and image is None:
            log.info(
                "widget profile cited image source %s, which carries no picture", self.image_source
            )
        return self._cited(evidence, kind, facts=[f.model_dump() for f in facts], image=image)


class Picture(Card):
    """A retrieved image, named by source number rather than by URL.

    The model never supplies the address. That is the entire point: a picture in
    an answer is then always one the search returned, and "show me the Argentine
    flag" cannot be answered with a remembered link to something else.
    """

    source: int = 0
    caption: str = Field(default="", max_length=140)

    def payload(self, evidence: Evidence, kind: str) -> tuple[dict | None, str]:
        # The one card whose source is resolved rather than merely checked: the
        # image URL comes from the retrieved result, never from the model.
        picture = evidence.picture(self.source, self.caption)
        if picture is None:
            log.info("widget image cited source %s, which carries no retrieved image", self.source)
        return picture, ""


class Node(Filled):
    id: str = Field(default="", max_length=24)
    label: str = Field(default="", max_length=48)

    @field_validator("id")
    @classmethod
    def _slug(cls, value: str) -> str:
        return re.sub(r"[^a-z0-9_-]", "", str(value or "").strip().lower())[:24]


class Edge(Filled):
    # `from` is a Python keyword, so the field is named `source` and aliased,
    # models write `from`, which is the word the schema should show them.
    source: str = Field(default="", alias="from", max_length=24)
    target: str = Field(default="", alias="to", max_length=24)
    label: str = Field(default="", max_length=32)

    model_config = {"populate_by_name": True}

    @field_validator("source", "target")
    @classmethod
    def _slug(cls, value: str) -> str:
        return re.sub(r"[^a-z0-9_-]", "", str(value or "").strip().lower())[:24]


class Diagram(Card):
    """A structure or a process as boxes and arrows.

    The only widget whose content is not a value copied from a source but a
    *reading* of one, which is why the validation below is the strictest here: a
    diagram is persuasive out of proportion to its evidence, and one drawn from
    a definition rather than from a described mechanism is a picture of the
    model's own guess.
    """

    title: str = Field(default="", max_length=60)
    nodes: list[Node] = Field(default_factory=list, max_length=MAX_NODES)
    edges: list[Edge] = Field(default_factory=list, max_length=MAX_EDGES)
    caption: str = Field(default="", max_length=140)
    source: int = 0

    def payload(self, evidence: Evidence, kind: str) -> tuple[dict | None, str]:
        """Keep the diagram only if it is actually a graph, and drop what dangles.

        Edges are pruned rather than rejected: a model that names one node it never
        declared has drawn a diagram that is right apart from one arrow, and losing
        the arrow is better than losing the diagram.

        Unconnected *nodes* are pruned too, as long as two connected boxes
        survive: rejecting the diagram over them would throw away a correct
        five-box picture over a sixth box the model could not place, and do it
        invisibly, since an empty card is hidden by CSS. Below that there
        is no structure to draw, and the caller says so out loud instead of emitting
        a card with nothing in it.
        """
        nodes = [node for node in self.nodes if node.id and node.label]
        ids: set[str] = set()
        unique: list[Node] = []
        for node in nodes:
            if node.id in ids:
                continue
            ids.add(node.id)
            unique.append(node)

        edges = [
            edge
            for edge in self.edges
            # A self-loop carries no structure and cannot be laid out on a layered
            # graph; a dangling end names a box that was never declared.
            if edge.source in ids and edge.target in ids and edge.source != edge.target
        ]

        connected = {edge.source for edge in edges} | {edge.target for edge in edges}
        kept = [node for node in unique if node.id in connected]
        if len(kept) < 2 or not edges:
            log.info(
                "widget diagram: %d nodes and %d usable edges is not a graph", len(kept), len(edges)
            )
            return None, (
                "The sources describe the parts but not how they connect, so no diagram is drawn."
            )
        if not evidence.knows(self.source):
            return None, ""
        if len(kept) < len(unique):
            log.info("widget diagram: pruned %d unconnected node(s)", len(unique) - len(kept))

        return {
            "kind": kind,
            "title": self.title,
            "caption": self.caption,
            "source": self.source,
            "nodes": [node.model_dump() for node in kept],
            "edges": [
                {"from": edge.source, "to": edge.target, "label": edge.label} for edge in edges
            ],
        }, ""


class Item(Filled):
    """One entry in an overview section. Which fields matter depends on the
    section's kind; the rest stay empty and are dropped from the payload.

    `source` is per item rather than per card, and that is the price of a panel
    composed from several blocks: a timeline's dates come from three pages, and
    one number for the whole card would be a citation for one of them.
    """

    label: str = Field(default="", max_length=60)
    text: str = Field(default="", max_length=200)
    note: str = Field(default="", max_length=60)
    cells: list[str] = Field(default_factory=list, max_length=MAX_COLUMNS)
    source: int = 0


class Section(Filled):
    """One block of an overview: a kind from `shapes.FACETS`, a title the model
    writes for this answer, and its items."""

    kind: str = Field(default="")
    title: str = Field(default="", max_length=60)
    # Only a grid has columns; every other kind ignores them.
    columns: list[str] = Field(default_factory=list, max_length=MAX_COLUMNS)
    items: list[Item] = Field(default_factory=list, max_length=MAX_ROWS)

    @field_validator("kind")
    @classmethod
    def _known(cls, value: str) -> str:
        kind = str(value or "").strip().lower()
        return kind if kind in shapes.FACETS else ""

    def payload(self, evidence: Evidence) -> dict[str, Any] | None:
        """One overview section as a payload, or None when too little survives.

        Built from copies, the model's own objects are left as it sent them, so
        a section judged twice is judged the same way both times.
        """
        facet = shapes.FACETS_BY_ID.get(self.kind)
        if facet is None:
            return None
        columns = [column for column in self.columns if column]
        if self.kind == shapes.GRID_BLOCK and not columns:
            return None

        rows: list[dict[str, Any]] = []
        for item in self.items:
            if not evidence.knows(item.source):
                continue
            row = {"label": item.label, "text": item.text, "note": item.note, "source": item.source}
            if self.kind == shapes.PROSCONS_BLOCK:
                row["label"] = _SIDES.get(item.label.lower(), "")
            elif self.kind == shapes.GRID_BLOCK:
                # Padded rather than refused: a row one value short is a row with
                # an empty cell, which renders as absent.
                row["cells"] = (item.cells + [""] * len(columns))[: len(columns)]
            if _COMPLETE[self.kind](row):
                rows.append(row)

        # One side only is a list pretending to be a weighing.
        if self.kind == shapes.PROSCONS_BLOCK and {r["label"] for r in rows} != {"pro", "con"}:
            return None
        rows = rows[: facet.max_items]
        if len(rows) < facet.min_items:
            return None
        payload: dict[str, Any] = {"kind": self.kind, "title": self.title, "items": rows}
        if self.kind == shapes.GRID_BLOCK:
            payload["columns"] = columns
        return payload


class Overview(Card):
    """A panel composed per answer from a closed set of blocks.

    The one widget whose layout the model chooses, and the reason it is safe to
    let it: the choice is between renderers that already exist, the blocks are
    the ones the classifier named, and every item is checked against the sources
    on its own, so the freedom is in the arrangement and never in the evidence.
    """

    sections: list[Section] = Field(default_factory=list, max_length=shapes.MAX_FACETS)

    def payload(self, evidence: Evidence, kind: str) -> tuple[dict | None, str]:
        """Keep each section that is still its kind once unsourced items are gone.

        Pruned from the bottom up, items first, then sections, then the panel, so
        that one uncited date costs that date and not the timeline, and a timeline
        the sources could not fill costs the timeline and not the facts beside it.
        Every refusal is silent: nothing promised the reader this panel.

        Only the kinds the classifier named survive. The model composing the panel
        is shown only those, and one that adds a callout anyway is decorating, which
        is the failure a closed, per-answer vocabulary is there to prevent.
        """
        sections: list[dict[str, Any]] = []
        seen: set[str] = set()
        for section in self.sections:
            if section.kind not in evidence.facets or section.kind in seen:
                continue
            payload = section.payload(evidence)
            if payload is None:
                log.info("widget overview: dropped the %s section", section.kind or "unnamed")
                continue
            seen.add(section.kind)
            sections.append(payload)
        if not sections:
            return None, ""
        return {"kind": kind, "sections": sections}, ""


# What an item needs before it counts, per block. A fact without its label is a
# value for nothing, a timeline entry without its date is not on a timeline.
def _labelled(row: dict[str, Any]) -> bool:
    return bool(row["label"] and row["text"])


_COMPLETE = {
    shapes.FACT_BLOCK: _labelled,
    shapes.STATS_BLOCK: _labelled,
    shapes.TIMELINE_BLOCK: _labelled,
    shapes.PROSCONS_BLOCK: _labelled,
    # A list item needs its name for the same reason: unlabelled, the entries
    # are clauses of the argument with the connections cut, which is the list
    # that makes an answer worse rather than easier to scan.
    shapes.LIST_BLOCK: _labelled,
    shapes.CALLOUT_BLOCK: lambda row: bool(row["text"]),
    shapes.GRID_BLOCK: lambda row: bool(row["label"] and any(row["cells"])),
}


# The words a model reaches for instead of "pro" and "con", in the two languages
# this app is asked in. Anything else loses the item, not the section.
_SIDES = {
    "pro": "pro",
    "pros": "pro",
    "for": "pro",
    "advantage": "pro",
    "dafür": "pro",
    "vorteil": "pro",
    "con": "con",
    "cons": "con",
    "contra": "con",
    "against": "con",
    "drawback": "con",
    "disadvantage": "con",
    "dagegen": "con",
    "nachteil": "con",
}


# --------------------------------------------------------------------- emitting


# Which schema and which instructions each shape uses. The shape is chosen by
# the classifier; this is the only place that knows what it turns into.
# `tests/test_widgets.py` holds this in lockstep with `shapes.WIDGET_SHAPES`,
# so a shape cannot be offered to the classifier without arriving here.
_WIDGETS: dict[str, tuple[type[Card], str]] = {
    shapes.FIGURE: (Figure, prompts.FIGURE_WIDGET),
    shapes.SERIES: (Series, prompts.SERIES_WIDGET),
    shapes.COMPARISON: (Comparison, prompts.COMPARISON_WIDGET),
    shapes.STEPS: (Steps, prompts.STEPS_WIDGET),
    shapes.PROFILE: (Profile, prompts.PROFILE_WIDGET),
    shapes.MATCH: (Match, prompts.MATCH_WIDGET),
    shapes.IMAGE: (Picture, prompts.PICTURE_WIDGET),
    shapes.WEATHER: (Weather, prompts.WEATHER_WIDGET),
    shapes.DIAGRAM: (Diagram, prompts.DIAGRAM_WIDGET),
    # The instructions here describe every block; `_messages` narrows them to
    # the ones this run's classifier named.
    shapes.OVERVIEW: (Overview, prompts.overview_widget(shapes.FACETS_ORDER)),
}


async def emit(
    ctx: RunContext,
    shape: str,
    question: str,
    passages: list[Passage],
    *,
    announce: bool = True,
) -> bool:
    """Ask for one widget and emit it. Returns whether anything was rendered.

    Never raises. A widget is an enhancement to an answer that is about to be
    written anyway, so a model that cannot fill the schema costs the widget and
    nothing else: the prose still runs, and the transcript still says what the
    run did.

    `announce` is whether a refusal is worth telling the reader about, and it is
    false for a widget nothing promised them. A diagram *answer* owes them the
    reason its picture is missing, the run's own transcript said it was coming;
    a diagram an explanation merely asked for owes them nothing, and a notice
    about a card they were never expecting reads as a failure rather than as the
    restraint it is.
    """
    entry = _WIDGETS.get(shape)
    if entry is None:
        return False
    schema, instructions = entry

    messages = _messages(ctx, shape, instructions, question, passages)
    if messages is None:
        return False

    # The router model first when one is configured, for the same reason the
    # classifier uses it: filling six named fields from evidence already in
    # front of the model is extraction, not writing, and on a reasoning model
    # that call thought for fifteen seconds about someone's date of birth.
    # The answer model is the fallback, not the default: a small router that
    # returns a card of empty labels costs one cheap call, not the card. Both
    # run beside the writer, so the second attempt is usually free in wall clock.
    # Two attempts either way. With no router configured the second is the same
    # model again, which is worth a call rather than a lost card: "could not
    # return usable JSON" is a per-response failure, and on `openrouter/auto` the
    # retry is not even the same model underneath. It cost the Apollo 17 card on
    # one bad response, and `can_research` still refuses it when the budget is
    # genuinely thin.
    attempts = [ctx.router, ctx.llm] if ctx.router is not None else [ctx.llm, ctx.llm]
    refusal = ""

    for attempt, model in enumerate(attempts, start=1):
        if attempt > 1 and not ctx.budget.can_research():
            break
        try:
            ctx.budget.spend_llm()
            data = await model.structured(messages, schema, max_tokens=WIDGET_TOKENS)
        except LLMError as exc:
            log.info("widget %s: %s could not fill the schema (%s)", shape, model.model, exc)
            continue

        payload, refusal = data.payload(Evidence.of(ctx), shape)
        if payload is not None:
            await ctx.stream.emit_block(blocks.block(shape, payload))
            # A profile that declined its portrait leaves the run holding
            # pictures it has already searched for and paid for. Asked "was ist
            # ubuntu", one run retrieved six, including a screenshot of the
            # desktop, filled the card without one and showed none of them.
            #
            # Shown as a picture of its own rather than promoted into the
            # portrait well: the model said none of these shows the subject, and
            # overriding that on an identity card is how the wrong face ends up
            # under the right name. As its own block it carries its title,
            # domain and source number, so the reader can see what it is.
            if shape == shapes.PROFILE and not payload.get("image") and ctx.stray_pictures:
                await _best_picture(ctx, "the profile card declined a portrait")
            return True
        # Silent to the reader, never to the log: a card that quietly never
        # appears is otherwise indistinguishable from one that was never asked
        # for, and the reason is always in the object the model returned.
        log.info("widget %s refused from %s: %s", shape, model.model, data.model_dump())

    # The card is lost; the pictures are not. A `wants_image` shape has already
    # paid for an image search, and what it found is registered and numbered,
    # so the run holds a picture of the thing that was asked about and would
    # otherwise show nothing at all because a *different* model call failed to
    # fill a schema. Asked "apollo 17", it retrieved six pictures, lost the
    # profile card to one bad JSON response, and answered in bare prose.
    if shapes.wants_image(shape) and ctx.stray_pictures and await _best_picture(ctx):
        return True

    # Most refusals stay silent: the prose answers the question either way and a
    # notice per missing card would be noise. A diagram is the exception: it is
    # the one widget the reader may have been promised by the run's own
    # transcript, so it says why it is not there, unless it was asked for on
    # spec by a shape that can do without it.
    if refusal and announce:
        await ctx.notice(refusal, "info")
    return False


async def _best_picture(ctx: RunContext, why: str = "the widget card was lost") -> bool:
    """Show the best retrieved picture, chosen by rank rather than by a model.

    The fallback is deliberately model-free. Choosing between pictures is a
    judgement worth a call when there is one to spend, but every path that
    reaches here has already spent it, and the ranking has an opinion already:
    sources carry citation numbers in the order they were ranked, so the first
    one holding an image is the one that won.

    No caption is written. The source's own title is a fact about it, and an
    invented caption would be the only unsourced string on the card, on the one
    widget whose entire design is that the model never supplies the picture.
    """
    for index, result in ctx.sources.all():
        if not result.image:
            continue
        await ctx.stream.emit_block(blocks.block(shapes.IMAGE, picture_payload(index, result)))
        log.info("%s; showed the retrieved picture from source %s instead", why, index)
        return True
    return False


def _messages(
    ctx: RunContext, shape: str, instructions: str, question: str, passages: list[Passage]
) -> list[dict[str, Any]] | None:
    """The call for this widget, or None when there is nothing to build one from.

    A picture is chosen from a list of retrieved images rather than from the
    evidence: the evidence never says which results carry a picture, so asking
    the model to find one there is asking it to guess.
    """
    candidates = [(index, result) for index, result in ctx.sources.all() if result.image]

    if shape == shapes.IMAGE:
        if not candidates:
            log.info("widget image: the search returned no images to choose from")
            return None
        return prompts.picture_widget(question, candidates)

    if not passages:
        return None
    if shape == shapes.OVERVIEW:
        instructions = prompts.overview_widget(ctx.facets or shapes.FACETS_ORDER)
    # A profile carries a portrait beside its facts, so it needs both halves:
    # the evidence for the facts and the candidate list for the picture.
    return prompts.widget(
        instructions, question, passages, candidates if shape == shapes.PROFILE else None
    )
