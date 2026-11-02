"""What a run is allowed to spend, and how it stops.

Two ideas do the work:

* **Reserve.** The writer is the only phase whose output the user sees, so time
  and calls are reserved for it up front. Research stops early; the answer still
  gets written.
* **Stop, don't raise.** Running out of budget is a normal outcome. Research
  phases ask `can_research()` and wind down; only cancellation unwinds the
  stack, because that one really is an abort.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from app.agent.depth import Depth
from app.config import settings

log = logging.getLogger(__name__)

# Time and calls held back so an out-of-budget run still produces an answer.
WRITE_RESERVE_S = 45.0
WRITE_RESERVE_CALLS = 1


class Cancelled(RuntimeError):
    """The user pressed stop. The only exception that should unwind a run."""


@dataclass(slots=True)
class Budget:
    depth: Depth
    started_at: float = field(default_factory=time.monotonic)
    llm_calls: int = 0
    searches: int = 0
    pages_fetched: int = 0
    # Room the citation check may use past the preset, granted by `allow_check`
    # where a check cut short costs more than a slower verdict.
    check_calls: int = 0
    check_s: float = 0.0

    @property
    def wall_clock_s(self) -> float:
        """The preset's ceiling, clamped by the deployment-wide maximum."""
        return min(self.depth.wall_clock_s, settings.max_wall_clock_s)

    @property
    def write_reserve_s(self) -> float:
        """Time held back for the writer, never more than half the preset.

        A flat 45s reserve is right for every preset that lasts a minute or
        more, and fatal below that: a 45s ceiling with a 45s reserve means
        `can_research()` is false before the run has begun. Halving instead
        leaves the existing presets untouched (45s is already less than half
        of QUICK's 90) while letting a cheap preset exist at all.
        """
        return min(WRITE_RESERVE_S, self.wall_clock_s / 2)

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def remaining_s(self) -> float:
        return max(0.0, self.wall_clock_s - self.elapsed_s)

    def spend_llm(self, count: int = 1) -> None:
        self.llm_calls += count

    def can_research(self) -> bool:
        """True while there is room for another research step."""
        return (
            self.remaining_s > self.write_reserve_s
            and self.llm_calls < self.depth.max_llm_calls - WRITE_RESERVE_CALLS
        )

    def can_continue(self) -> bool:
        """True while an answer cut off at its token ceiling may be carried on.

        Not `can_research`: that holds the writer's reserve back, and by the
        time an answer has hit its ceiling the writer has spent the reserve on
        exactly that answer. Asked there, a long answer, the one most likely to
        hit the ceiling, was almost never continued, and reached the reader
        stopping mid-sentence under a "cut short" notice. The continuation is
        the writer finishing its own work, so it only needs the clock not to
        have run out; it is one call (`research.CONTINUE_ATTEMPTS`), and the
        runner's hard limit already allows a full answer stream past the
        preset."""
        return self.remaining_s > 0

    def can_verify(self) -> bool:
        """True while the citation check can still be paid for.

        Separate from `can_research` because it is asked at a different moment:
        the check runs *after* the answer is written, so the writer's reserve is
        no longer being held for anything. Reserving against it here would make
        the check unaffordable on exactly the presets that budget it: every
        one of them, since the reserve is what the writer just spent.
        """
        return (
            self.wall_clock_s + self.check_s - self.elapsed_s > 0
            and self.llm_calls < self.depth.max_llm_calls + self.check_calls
        )

    def allow_check(self, *, calls: int, seconds: float) -> None:
        """Let the citation check run past the preset by this much.

        For literature projects, where every sentence is a claim and the
        verdicts are what a thesis rests on: there an answer written at the
        end of its budget came back with half its sentences unchecked, not
        because anything failed but because the clock had run out while the
        reader was already reading. The answer is on screen by then, so the
        extra time costs a later verdict, never a later answer. Bounded, and
        well inside the runner's hard limit (`runner.hard_limit_s`).
        """
        self.check_calls = max(self.check_calls, calls)
        self.check_s = max(self.check_s, seconds)

    def note_exhaustion(self, phase: str) -> str | None:
        """A human-readable reason, for the transcript. None if nothing was cut."""
        if self.remaining_s <= self.write_reserve_s:
            return f"Stopped {phase} at the {self.wall_clock_s:.0f}s time budget."
        if self.llm_calls >= self.depth.max_llm_calls - WRITE_RESERVE_CALLS:
            return f"Stopped {phase} at the {self.depth.max_llm_calls}-call budget."
        return None

    def summary(self) -> dict[str, float | int]:
        return {
            "elapsed_s": round(self.elapsed_s, 1),
            "llm_calls": self.llm_calls,
            "searches": self.searches,
            "pages_fetched": self.pages_fetched,
        }
