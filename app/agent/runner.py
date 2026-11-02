"""Background run supervision.

A run is keyed by `run_id` and owned by the process, never by an HTTP request.
Closing the tab does not cancel it; reopening replays from the stream.

The one rule this module exists to enforce: **every exit path emits `done`.**
A run that ends without one leaves the UI on an infinite spinner.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.config import settings
from app.store.stream import TERMINAL_STATUSES, RunStream

log = logging.getLogger(__name__)

# Strong refs so the event loop does not garbage-collect running tasks.
_tasks: set[asyncio.Task[Any]] = set()
# Slots claimed by runs that are still being set up. Starting a run awaits the
# database several times between deciding there is room and spawning, and
# requests arriving in that gap all saw the same free slot.
_claimed = 0

RunFn = Callable[[RunStream], Awaitable[None]]
# Called once a run reaches a terminal state, whatever that state is. It is the hook
# history persistence hangs off, so it must not be skipped on the error path.
FinalizeFn = Callable[[RunStream, str], Awaitable[None]]


# How long the stop button is left to the run itself. Its own `Cancelled` path
# names what was found so far and writes the footer, which beats being cut off,
# so the hard stop only comes for a run that is stuck inside an await and will
# never reach the next `guard()`.
COOPERATIVE_STOP_S = 5.0
# How often a running run's cancel flag is looked at from outside it.
CANCEL_POLL_S = 1.0


def hard_limit_s() -> float:
    """The ceiling no run outlives, whatever it is waiting on.

    The presets' wall clocks are budgets: they are checked between steps and
    deliberately leave the writer free to finish, so a run may honestly end a
    little after its own. This is the other kind of limit. A run blocked inside
    one await, a stalled provider, a lock that never comes, never reaches a
    check, and without this held its capacity slot and its spinner forever.
    Generous: the deepest preset plus one full answer stream.
    """
    return settings.max_wall_clock_s + settings.llm_stream_timeout_s


def spawn(
    stream: RunStream,
    fn: RunFn,
    finalize: FinalizeFn | None = None,
    *,
    limit_s: float | None = None,
    claimed: bool = False,
) -> asyncio.Task[Any]:
    """Start supervising a run. `claimed` hands over a slot from `claim()`."""
    if claimed:
        release()
    task = asyncio.create_task(
        _supervise(stream, fn, finalize, limit_s or hard_limit_s()), name=f"run:{stream.run_id}"
    )
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def at_capacity() -> bool:
    """Back-pressure. A single small VPS answers a handful of runs at once; the
    honest response to more is to say no, not to thrash."""
    return len(_tasks) + _claimed >= settings.max_concurrent_runs


def claim() -> bool:
    """Hold a slot for a run about to be set up. False when there is none.

    Checked and taken in one step, with no await in between, so two requests
    cannot both be granted the last slot. The slot passes to the run through
    `spawn(claimed=True)`, or back through `release()` if setup fails.
    """
    global _claimed
    if at_capacity():
        return False
    _claimed += 1
    return True


def release() -> None:
    """Return a slot from `claim()` that no run was spawned into."""
    global _claimed
    _claimed = max(0, _claimed - 1)


async def _supervise(
    stream: RunStream, fn: RunFn, finalize: FinalizeFn | None, limit_s: float
) -> None:
    status = "error"
    deadline = asyncio.timeout(limit_s)
    stopped = asyncio.Event()
    watcher = asyncio.create_task(_watch_cancel(stream, deadline, stopped))
    try:
        await stream.set_state(status="running")
        async with deadline:
            await fn(stream)
    except TimeoutError:
        if not deadline.expired():
            # Raised by the run itself, not by the deadline: an ordinary failure.
            log.exception("run %s failed", stream.run_id)
            status = "error"
            await _emit_quietly(stream, "error", "TimeoutError: the run timed out")
        elif stopped.is_set():
            status = "cancelled"
            await _emit_quietly(stream, "cancelled", "Stopped at your request")
        else:
            log.error("run %s exceeded the hard limit of %.0fs", stream.run_id, limit_s)
            status = "error"
            await _emit_quietly(
                stream,
                "error",
                f"The run was stopped after {limit_s / 60:.0f} minutes without finishing. "
                "A model or search service probably stopped responding.",
            )
    except asyncio.CancelledError:
        # Process shutdown, not the user's stop button: that one comes back
        # through the cancel flag, and when the run ignores it, through the
        # deadline above, as a TimeoutError.
        status = "error"
        await _emit_quietly(stream, "error", _interrupt_message(await _state_quietly(stream)))
        raise
    except Exception as exc:
        log.exception("run %s failed", stream.run_id)
        status = "error"
        await _emit_quietly(stream, "error", f"{type(exc).__name__}: {exc}")
    else:
        # A run that finished without saying so still owes the client a terminal event.
        state = await stream.get_state()
        status = state.get("status", "")
        if status not in TERMINAL_STATUSES:
            status = "completed"
            await _emit_quietly(stream, "completed")
    finally:
        watcher.cancel()
        if finalize is not None:
            try:
                await finalize(stream, status)
            except Exception:
                log.exception("finalizing run %s failed", stream.run_id)


async def _watch_cancel(
    stream: RunStream, deadline: asyncio.Timeout, stopped: asyncio.Event
) -> None:
    """Make the stop button work on a run that has stopped listening.

    The flag is cooperative and the run normally answers it at its next step.
    One that has not answered after `COOPERATIVE_STOP_S` is waiting on something
    that may never return, so its deadline is moved to now, which unwinds it the
    same way the hard limit does.
    """
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(CANCEL_POLL_S)
        try:
            if await stream.is_cancelled():
                break
        except Exception:  # a Redis blip must not end the run, only this check
            continue
    await asyncio.sleep(COOPERATIVE_STOP_S)
    stopped.set()
    # A run that finished in the meantime has nothing left to stop.
    with contextlib.suppress(RuntimeError):
        deadline.reschedule(loop.time())


async def _state_quietly(stream: RunStream) -> dict[str, str]:
    """The run's state, or nothing. Called on the shutdown path, where Redis may
    already be closing under us and a failure here would replace a useful
    message with a useless traceback."""
    try:
        return await stream.get_state()
    except Exception:
        return {}


async def _emit_quietly(stream: RunStream, status: str, message: str | None = None) -> None:
    """Emit a terminal event, tolerating a Redis that has already gone away.

    This runs on the shutdown path, where the connection may be closing under
    it. Failing here would replace a useful error with a useless one.
    """
    try:
        await stream.emit_done(status, message)
    except Exception:
        log.warning("could not emit terminal event for run %s", stream.run_id)


async def active_count() -> int:
    return len(_tasks)


async def reap_orphans(redis: Any, ttl: int) -> int:
    """Terminate runs that a previous process left behind.

    A hard kill (OOM, `docker kill`, node failure) skips the shutdown path, so
    the run's state stays `running` with no terminal event and any client that
    reconnects waits forever. Runs live in-process, so at startup this process
    owns none of them by definition: anything still marked running is orphaned.

    Assumes a single app replica, which matches the deployment. More than one
    would need an owner tag in the state hash instead of this blanket sweep.
    """
    reaped = 0
    async for key in redis.scan_iter(match="run:*:state", count=100):
        state = await redis.hgetall(key)
        if state.get("status") not in ("running", "queued"):
            continue
        run_id = key.split(":")[1]
        await RunStream(redis, run_id, ttl=ttl).emit_done("error", _interrupt_message(state))
        reaped += 1
    if reaped:
        log.warning("reaped %d orphaned run(s) from a previous process", reaped)
    return reaped


def _interrupt_message(state: dict[str, str]) -> str:
    """What to tell the client about a run this process killed.

    A run may leave its own sentence in the state hash, which says more than
    "the run failed" ever can; without one this is the fallback.
    """
    return state.get("interrupt_message") or "Run interrupted by a server restart"


async def shutdown(grace_s: float = 5.0) -> None:
    """Cancel in-flight runs on shutdown. Redis keeps what was already emitted,
    so a redeploy loses progress but never the transcript."""
    running = list(_tasks)
    if not running:
        return
    for task in running:
        task.cancel()
    await asyncio.wait(running, timeout=grace_s)
