"""Every exit path owes the client a terminal event.

The fork never emits an error event anywhere, which is why it spins forever.
These tests exist so that property cannot regress silently.
"""

from __future__ import annotations

import asyncio

import pytest
from fakeredis import aioredis

from app.agent import runner
from app.store.stream import RunStream


@pytest.fixture
async def redis():
    client = aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def stream(redis):
    return RunStream(redis, "run_test", ttl=60)


async def terminal_events(stream: RunStream) -> list[dict]:
    events = []
    async for event in stream.read(last_id="0", block_ms=1):
        if event is None:
            break
        if event.event == "done":
            events.append(event.data)
    return events


async def test_raising_run_emits_error_not_silence(stream):
    async def boom(_s):
        raise RuntimeError("provider exploded")

    await asyncio.wait_for(runner.spawn(stream, boom), timeout=5)

    done = await terminal_events(stream)
    assert len(done) == 1
    assert done[0]["status"] == "error"
    assert "provider exploded" in done[0]["message"]


async def test_run_that_forgets_to_finish_still_terminates(stream):
    async def silent(_s):
        return None

    await asyncio.wait_for(runner.spawn(stream, silent), timeout=5)

    assert await terminal_events(stream) == [{"status": "completed"}]


async def test_run_that_emits_its_own_done_is_not_double_terminated(stream):
    async def polite(s):
        await s.emit_done("completed")

    await asyncio.wait_for(runner.spawn(stream, polite), timeout=5)

    assert len(await terminal_events(stream)) == 1


async def test_a_run_stuck_inside_an_await_is_ended_by_the_hard_limit(stream):
    """The budget is only checked between steps, so a run blocked inside one
    await, a stalled provider, a slot that never frees, never reached a check
    and kept its spinner and its capacity slot forever."""

    async def stuck(_s):
        await asyncio.Event().wait()

    await asyncio.wait_for(runner.spawn(stream, stuck, limit_s=0.05), timeout=5)

    done = await terminal_events(stream)
    assert len(done) == 1
    assert done[0]["status"] == "error"
    assert "without finishing" in done[0]["message"]


async def test_stop_ends_a_run_that_no_longer_checks_the_flag(stream, monkeypatch):
    """The stop button is a flag the run reads between steps. One stuck inside
    an await never reads it, so after a grace period it is stopped from outside,
    and reported as stopped rather than as a failure."""
    monkeypatch.setattr(runner, "CANCEL_POLL_S", 0.01)
    monkeypatch.setattr(runner, "COOPERATIVE_STOP_S", 0.01)
    finalized = []

    async def stuck(_s):
        await asyncio.Event().wait()

    async def finalize(_s, status):
        finalized.append(status)

    task = runner.spawn(stream, stuck, finalize)
    await stream.cancel()
    await asyncio.wait_for(task, timeout=5)

    assert await terminal_events(stream) == [
        {"status": "cancelled", "message": "Stopped at your request"}
    ]
    assert finalized == ["cancelled"]


async def test_a_timeout_raised_by_the_run_is_an_ordinary_failure(stream):
    """Only the supervisor's own deadline means "stuck"; a library that raises
    TimeoutError is a failed run like any other."""

    async def times_out(_s):
        raise TimeoutError

    await asyncio.wait_for(runner.spawn(stream, times_out), timeout=5)

    done = await terminal_events(stream)
    assert done[0]["status"] == "error"
    assert "without finishing" not in done[0]["message"]


async def test_reaper_terminates_runs_orphaned_by_a_hard_kill(redis, stream):
    # A SIGKILL leaves exactly this: state says running, no terminal event.
    await stream.set_state(status="running", phase="researching")
    await stream.emit("block", {"id": "b1", "type": "text", "data": {}})

    assert await runner.reap_orphans(redis, ttl=60) == 1

    done = await terminal_events(stream)
    assert done[0]["status"] == "error"
    assert "restart" in done[0]["message"]


async def test_reaper_leaves_finished_runs_alone(redis, stream):
    await stream.set_state(status="completed")
    assert await runner.reap_orphans(redis, ttl=60) == 0
    assert await terminal_events(stream) == []
