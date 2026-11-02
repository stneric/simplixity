"""Phase 2 acceptance: the stream must be replayable by offset, exactly once.

If these fail, deep research does not work, and it is better to find out on day
two than on day thirty.
"""

from __future__ import annotations

import pytest
from fakeredis import aioredis

from app.agent import blocks
from app.store.stream import RunStream, apply_patch


@pytest.fixture
async def redis():
    client = aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def stream(redis):
    return RunStream(redis, "run_test", ttl=60)


async def drain(stream: RunStream, last_id: str = "0") -> list:
    """Read everything currently in the stream, stopping at the first timeout."""
    events = []
    async for event in stream.read(last_id=last_id, block_ms=1):
        if event is None:
            break
        events.append(event)
    return events


async def test_replay_from_zero_returns_every_event(stream):
    await stream.emit("phase", {"phase": "researching"})
    await stream.emit_block(blocks.text_block())
    await stream.emit_done("completed")

    events = await drain(stream)

    assert [e.event for e in events] == ["phase", "block", "done"]


async def test_resume_from_last_event_id_has_no_gap_and_no_duplicate(stream):
    """The Last-Event-ID contract: resume yields the tail, and only the tail."""
    for index in range(6):
        await stream.emit("block", {"n": index})

    first_half = await drain(stream)
    cut = first_half[2]  # pretend the client died right after receiving this

    resumed = await drain(stream, last_id=cut.id)

    assert [e.data["n"] for e in first_half] == [0, 1, 2, 3, 4, 5]
    assert [e.data["n"] for e in resumed] == [3, 4, 5], "resume must be exclusive of the cursor"
    assert cut.id not in [e.id for e in resumed]


async def test_ids_are_monotonic(stream):
    for index in range(5):
        await stream.emit("block", {"n": index})
    events = await drain(stream)
    ids = [tuple(int(part) for part in e.id.split("-")) for e in events]
    assert ids == sorted(ids)


async def test_snapshot_tracks_patches_for_late_joiners(stream):
    block = blocks.text_block()
    await stream.emit_block(block)
    await stream.emit_patch(block["id"], [blocks.append_text("Hello ")])
    await stream.emit_patch(block["id"], [blocks.append_text("world")])

    snapshot = await stream.snapshot()

    assert len(snapshot) == 1
    assert snapshot[0]["data"]["content"] == "Hello world"


async def test_cancel_flag_round_trips(stream):
    await stream.set_state(status="running", cancel="0")
    assert await stream.is_cancelled() is False
    await stream.cancel()
    assert await stream.is_cancelled() is True


async def test_events_expire(stream, redis):
    await stream.emit("phase", {"phase": "researching"})
    assert 0 < await redis.ttl(stream.events_key) <= 60


# ------------------------------------------------------------------ patch ops


def test_append_op_concatenates_strings():
    doc = {"data": {"content": "ab"}}
    apply_patch(doc, [{"op": "append", "path": "/data/content", "value": "cd"}])
    assert doc["data"]["content"] == "abcd"


def test_append_op_on_missing_key_starts_empty():
    doc = {"data": {}}
    apply_patch(doc, [{"op": "append", "path": "/data/content", "value": "x"}])
    assert doc["data"]["content"] == "x"


def test_add_to_array_tail():
    doc = {"data": {"sub_steps": []}}
    apply_patch(doc, [blocks.add_sub_step("Searching")])
    assert doc["data"]["sub_steps"] == [{"text": "Searching", "status": "active"}]


def test_replace_nested_array_member():
    doc = {"data": {"sub_steps": [{"text": "a", "status": "active"}]}}
    apply_patch(doc, [blocks.set_sub_step_status(0, "done")])
    assert doc["data"]["sub_steps"][0]["status"] == "done"


def test_remove_op():
    doc = {"data": {"sources": [{"url": "a"}, {"url": "b"}]}}
    apply_patch(doc, [{"op": "remove", "path": "/data/sources/0"}])
    assert doc["data"]["sources"] == [{"url": "b"}]


async def test_concurrent_patches_to_one_block_all_reach_the_snapshot(redis):
    """A patch is read-modify-write. The sub-questions of a deep run register
    sources into one sidebar at the same time, and with any latency between
    the read and the write each saw the same old copy: the stored transcript
    kept one source of five, and a reopened chat cited sources it did not have."""
    import asyncio

    hget = redis.hget

    async def slow_hget(*args, **kwargs):
        value = await hget(*args, **kwargs)
        await asyncio.sleep(0.001)  # the network a real Redis sits behind
        return value

    redis.hget = slow_hget
    stream = RunStream(redis, "race", ttl=60)
    block = {"id": "s", "type": "sources", "data": {"sources": []}}

    await asyncio.gather(
        stream.emit_block(block),
        *(
            stream.emit_patch("s", [{"op": "add", "path": "/data/sources/-", "value": i}])
            for i in range(5)
        ),
    )

    snapshot = await stream.snapshot()
    assert sorted(snapshot[0]["data"]["sources"]) == [0, 1, 2, 3, 4]
    # And the log never names a block before it exists.
    kinds = [event.event for event in await drain(stream)]
    assert kinds[0] == "block"


async def test_a_patch_from_another_writer_still_reads_the_stored_block(redis):
    """The in-memory copy is only this writer's; a block written by some other
    instance is still patched from Redis."""
    text = blocks.text_block()
    await RunStream(redis, "shared", ttl=60).emit_block(text)
    other = RunStream(redis, "shared", ttl=60)

    await other.emit_patch(text["id"], [blocks.append_text("hello")])

    assert (await other.snapshot())[0]["data"]["content"] == "hello"


async def test_the_snapshot_keeps_the_order_blocks_were_emitted_in(stream, redis):
    """A Redis hash has no order once its values pass 64 bytes, which every
    block does: the polling fallback rendered the answer above its sources."""
    first, second = blocks.sources_block(), blocks.text_block("answer")
    await stream.emit_block(first)
    await stream.emit_block(second)
    # A replacement keeps its place in line rather than moving to the end.
    await stream.emit_block({**first, "data": {"sources": [{"index": 1}]}})

    # Read by another instance, the way the API and the finalizer see it.
    fresh = RunStream(redis, "run_test", ttl=60)
    assert [b["id"] for b in await fresh.snapshot()] == [first["id"], second["id"]]
    assert (await fresh.snapshot())[0]["data"]["sources"] == [{"index": 1}]


async def test_a_block_the_order_list_does_not_name_is_still_shown(stream, redis):
    await redis.hset(stream.blocks_key, "legacy", '{"id":"legacy","type":"text","data":{}}')
    await stream.emit_block(blocks.text_block("new"))
    assert [b["id"] for b in await stream.snapshot()][-1] == "legacy"


async def test_events_carry_their_payload_as_stored(stream):
    await stream.emit_patch("missing", [{"op": "append", "path": "/data/content", "value": "x"}])
    [event] = await drain(stream)
    assert event.raw == (
        '{"id":"missing","ops":[{"op":"append","path":"/data/content","value":"x"}]}'
    )
    assert event.data["id"] == "missing"
