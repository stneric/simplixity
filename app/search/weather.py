"""Weather from SearXNG's own weather engines, as data rather than as prose.

SearXNG has working weather engines (`wttr.in` and `duckduckgo weather`), and
they return a fully structured reading: temperature with its unit, condition,
apparent temperature, humidity, wind, and an hourly forecast with timestamps.
None of that needs a language model, and routing it through one would be strictly
worse: an extracted number can be wrong in ways a fetched one cannot.

Two things had to be understood before it worked, and both are the caller's
fault rather than the engine's:

* **The query must be a bare place name.** The engines hand the whole search
  string to Open-Meteo's geocoder, so `Berlin` resolves and `weather Berlin`
  raises, which surfaces as an "unexpected crash" in the engine list and looks
  like a broken engine.
* **The reading arrives in `answers`, not in `results`.** A caller reading only
  `results` sees an empty response from an engine that answered perfectly.

Nothing here is inferred. Hourly points are grouped into days and reduced to the
high, the low and the condition that held longest: all arithmetic over values
the engine gave, never an estimate.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app import net
from app.config import settings
from app.search.base import SearchError

log = logging.getLogger(__name__)

# Both are asked at once and the first usable answer wins; they disagree by a
# degree or two, which is weather, not a fault.
ENGINES = ("wttr.in", "duckduckgo weather")

MAX_DAYS = 6

# SearXNG normalises every engine's condition text to this vocabulary, and the
# UI has one icon per value. Anything unmapped renders no icon rather than the
# wrong sky.
_CONDITIONS = {
    "clear sky": "clear",
    "fair": "clear",
    "sunny": "clear",
    "partly cloudy": "partly-cloudy",
    "partly-cloudy": "partly-cloudy",
    "cloudy": "cloudy",
    "mostly cloudy": "cloudy",
    "overcast": "cloudy",
    "fog": "fog",
    "mist": "fog",
    "haze": "fog",
    "light rain": "drizzle",
    "drizzle": "drizzle",
    "light drizzle": "drizzle",
    "rain": "rain",
    "moderate rain": "rain",
    "heavy rain": "showers",
    "rain showers": "showers",
    "showers": "showers",
    "light snow": "snow",
    "snow": "snow",
    "heavy snow": "snow",
    "sleet": "sleet",
    "freezing rain": "sleet",
    "thunderstorm": "thunderstorm",
    "thundery outbreaks": "thunderstorm",
    "windy": "wind",
}


def condition_of(text: str) -> str:
    """Map an engine's condition text to the UI's closed vocabulary."""
    key = " ".join(str(text or "").lower().split())
    if key in _CONDITIONS:
        return _CONDITIONS[key]
    # "patchy light drizzle", "light rain shower": engines qualify the base
    # term, so the longest matching phrase wins over an arbitrary first hit.
    matches = [(len(phrase), value) for phrase, value in _CONDITIONS.items() if phrase in key]
    return max(matches)[1] if matches else ""


@dataclass(slots=True)
class Day:
    day: str = ""
    date: str = ""
    high: str = ""
    low: str = ""
    condition: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "day": self.day,
            "high": self.high,
            "low": self.low,
            "condition": self.condition,
            "precipitation": "",
        }


@dataclass(slots=True)
class Report:
    """One place's conditions, exactly as an engine reported them."""

    location: str = ""
    temperature: str = ""
    unit: str = ""
    condition: str = ""
    summary: str = ""
    feels_like: str = ""
    wind: str = ""
    humidity: str = ""
    as_of: str = ""
    engine: str = ""
    days: list[Day] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "location": self.location,
            "temperature": self.temperature,
            "unit": self.unit,
            "condition": self.condition,
            "summary": self.summary,
            "feels_like": self.feels_like,
            "wind": self.wind,
            "humidity": self.humidity,
            "as_of": self.as_of,
            "days": [day.as_dict() for day in self.days],
            # No citation number: this did not come from a page the run
            # registered. The engine is the attribution, and naming it is the
            # same promise the source list makes everywhere else.
            "source": 0,
            "service": self.engine,
            "kind": "weather",
        }


async def lookup(place: str) -> Report | None:
    """Fetch conditions for a place, or None if no engine could answer."""
    place = " ".join(str(place or "").split())
    if not place:
        return None

    payload = await _get(place)
    answers = [a for a in payload.get("answers") or [] if isinstance(a, dict) and "current" in a]
    # In `ENGINES` order, not in the order the response happened to list them:
    # wttr.in reports wind in km/h where DuckDuckGo reports mi/h regardless of
    # the query language, and a German forecast in miles per hour is a small
    # wrongness with no upside.
    answers.sort(key=lambda a: _preference(str(a.get("engine") or "")))

    for answer in answers:
        report = _parse(answer)
        if report and report.temperature:
            return report
    return None


def _preference(engine: str) -> int:
    return ENGINES.index(engine) if engine in ENGINES else len(ENGINES)


async def _get(place: str) -> dict:
    params = {
        "q": place,  # bare place name; see the module docstring
        "format": "json",
        "engines": ",".join(ENGINES),
        "language": settings.search_language,
    }
    try:
        response = await net.client().get(
            f"{settings.searxng_api_url.rstrip('/')}/search",
            params=params,
            timeout=settings.search_timeout_s,
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        # A weather lookup is an enhancement to an answer that will be written
        # anyway; it never fails a run.
        log.info("weather lookup for %r failed: %s", place, _brief(exc))
        return {}


def _brief(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:160]


def _parse(answer: dict) -> Report | None:
    current = answer.get("current") or {}
    if not isinstance(current, dict):
        return None

    report = Report(
        location=str((current.get("location") or {}).get("name") or ""),
        temperature=_value(current.get("temperature")),
        unit=_unit(current.get("temperature")),
        condition=condition_of(current.get("condition") or ""),
        summary=str(current.get("summary") or ""),
        feels_like=_measure(current.get("feels_like")),
        wind=_measure(current.get("wind_speed")),
        humidity=_percent(current.get("humidity")),
        engine=str(answer.get("engine") or ""),
    )
    report.days = _days(answer.get("forecasts") or [], report.unit)
    report.as_of = _now_of(current, answer.get("forecasts") or [])
    return report


def _days(forecasts: list, unit: str) -> list[Day]:
    """Group hourly points into days.

    Engines report every one to three hours; a reader wants a day. The high and
    the low are the extremes of the points given, and the condition is the one
    that held for the most of them: arithmetic over reported values, never an
    estimate of what was not reported.
    """
    buckets: dict[str, list[dict]] = {}
    for point in forecasts:
        if not isinstance(point, dict):
            continue
        stamp = _datetime_of(point)
        if stamp is None:
            continue
        buckets.setdefault(stamp.date().isoformat(), []).append(point)

    days: list[Day] = []
    for date in sorted(buckets)[:MAX_DAYS]:
        points = buckets[date]
        readings = [_number(point.get("temperature")) for point in points]
        readings = [value for value in readings if value is not None]
        if not readings:
            continue
        conditions = Counter(
            condition_of(point.get("condition") or "")
            for point in points
            if condition_of(point.get("condition") or "")
        )
        days.append(
            Day(
                day=_weekday(date),
                date=date,
                high=_round(max(readings)),
                low=_round(min(readings)),
                condition=conditions.most_common(1)[0][0] if conditions else "",
            )
        )
    return days


def _weekday(date: str) -> str:
    try:
        return datetime.fromisoformat(date).strftime("%a")
    except ValueError:
        return date


def _datetime_of(point: dict) -> datetime | None:
    raw = point.get("datetime")
    text = raw.get("datetime") if isinstance(raw, dict) else raw
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _now_of(current: dict, forecasts: list) -> str:
    """When the reading describes. `current.datetime` is often null, in which
    case the first forecast point is the nearest honest answer."""
    stamp = _datetime_of(current)
    if stamp is None:
        for point in forecasts:
            if isinstance(point, dict):
                stamp = _datetime_of(point)
                if stamp is not None:
                    break
    return stamp.strftime("%d %b %Y, %H:%M") if stamp else ""


def _number(measure: Any) -> float | None:
    if not isinstance(measure, dict):
        return None
    try:
        return float(measure.get("val"))
    except (TypeError, ValueError):
        return None


def _round(value: float) -> str:
    """Whole degrees. The engines disagree in the decimals and nobody reads
    them; `21` and `21.33` are the same forecast."""
    return str(round(value))


def _value(measure: Any) -> str:
    number = _number(measure)
    return _round(number) if number is not None else ""


def _unit(measure: Any) -> str:
    return str(measure.get("unit") or "") if isinstance(measure, dict) else ""


def _measure(measure: Any) -> str:
    number = _number(measure)
    if number is None:
        return ""
    unit = _unit(measure)
    return f"{_round(number)}{unit}" if unit.startswith("°") else f"{_round(number)} {unit}".strip()


def _percent(measure: Any) -> str:
    number = _number(measure)
    return f"{_round(number)}%" if number is not None else ""


__all__ = ["ENGINES", "Report", "SearchError", "condition_of", "lookup"]
