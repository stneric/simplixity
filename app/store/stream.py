"""Redis-Streams-backed event log for a single run.

The load-bearing idea: the Redis Stream entry ID *is* the SSE event id. The
browser replays it back in `Last-Event-ID` on reconnect, which is exactly the
cursor `XREAD` wants. That removes the reconnect endpoint, the client resume
state machine, and the in-memory replay buffer all at once.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

log = logging.getLogger(__name__)

TERMINAL_EVENT = "done"
# Consecutive Redis read failures tolerated before a reader gives up and lets
# the client reconnect from its last event id.
_MAX_READ_FAILURES = 3
# A run in any of these states is over; nothing may emit after one lands.
TERMINAL_STATUSES = frozenset({"completed", "error", "cancelled"})


@dataclass(slots=True)
class StreamEvent:
    id: str
    event: str
    # The payload exactly as it was stored. The SSE layer forwards it as it is:
    # decoding every token delta only to encode it again was the bulk of the
    # work a live connection did.
    raw: str

    @property
    def data(self) -> dict[str, Any]:
        return json.loads(self.raw)


class RunStream:
    def __init__(self, redis: Redis, run_id: str, ttl: int = 86400) -> None:
        self.redis = redis
        self.run_id = run_id
        self.ttl = ttl
        # Block writes are read-modify-write: the patched block is the stored
        # one plus the ops. Concurrent writers, the sub-questions of a deep run
        # all registering sources into one sidebar, each read the same old copy
        # and the last write won, so sources vanished from the transcript that
        # is stored once the run ends. The lock makes each write whole, and
        # keeps a patch from reaching the log before the block it applies to.
        self._block_lock = asyncio.Lock()
        # This process's copy of every block it wrote. Runs are in-process, so
        # the writer always holds the latest version, and patching from it
        # saves a Redis round trip on every flush of a streamed answer.
        self._blocks: dict[str, dict[str, Any]] = {}

    @property
    def events_key(self) -> str:
        return f"run:{self.run_id}:events"

    @property
    def state_key(self) -> str:
        return f"run:{self.run_id}:state"

    @property
    def blocks_key(self) -> str:
        return f"run:{self.run_id}:blocks"

    @property
    def order_key(self) -> str:
        """Block ids in emission order. The blocks hash has no order of its own
        (past 64 bytes a value turns the listpack into a hashtable), and a late
        joiner or the polling fallback rendered the answer above its sources."""
        return f"run:{self.run_id}:order"

    # ------------------------------------------------------------------ write

    async def emit(self, event: str, data: dict[str, Any]) -> str:
        """Append one event. Returns the stream entry id (== the SSE event id)."""
        payload = json.dumps(data, separators=(",", ":"))
        async with self.redis.pipeline(transaction=False) as pipe:
            pipe.xadd(self.events_key, {"event": event, "data": payload})
            pipe.expire(self.events_key, self.ttl)
            result = await pipe.execute()
        return result[0]

    async def emit_block(self, block: dict[str, Any]) -> str:
        """Emit a new block and snapshot it so late joiners can skip the replay."""
        async with self._block_lock:
            payload = json.dumps(block, separators=(",", ":"))
            # A block id seen before is a replacement, not a new place in line.
            first = block["id"] not in self._blocks
            self._blocks[block["id"]] = copy.deepcopy(block)
            # One round trip for the snapshot, the order and the event: this
            # runs for every block, and the lock is held across it.
            async with self.redis.pipeline(transaction=False) as pipe:
                pipe.hset(self.blocks_key, block["id"], payload)
                pipe.expire(self.blocks_key, self.ttl)
                if first:
                    pipe.rpush(self.order_key, block["id"])
                    pipe.expire(self.order_key, self.ttl)
                pipe.xadd(self.events_key, {"event": "block", "data": payload})
                pipe.expire(self.events_key, self.ttl)
                result = await pipe.execute()
            return result[-2]

    async def emit_patch(self, block_id: str, ops: list[dict[str, Any]]) -> str:
        """Emit RFC-6902 ops against an existing block, keeping the snapshot current."""
        async with self._block_lock:
            block = self._blocks.get(block_id)
            if block is None:
                raw = await self.redis.hget(self.blocks_key, block_id)
                block = json.loads(raw) if raw is not None else None
            payload = json.dumps({"id": block_id, "ops": ops}, separators=(",", ":"))
            # Snapshot and event in one round trip: this is the path every
            # flush of a streamed answer takes, several times a second.
            async with self.redis.pipeline(transaction=False) as pipe:
                if block is not None:
                    # A copy, so a caller that later mutates a value it patched in
                    # cannot reach into the cached block behind the log's back.
                    apply_patch(block, copy.deepcopy(ops))
                    self._blocks[block_id] = block
                    pipe.hset(self.blocks_key, block_id, json.dumps(block, separators=(",", ":")))
                pipe.xadd(self.events_key, {"event": "patch", "data": payload})
                pipe.expire(self.events_key, self.ttl)
                result = await pipe.execute()
            return result[-2]

    async def emit_phase(self, phase: str) -> str:
        await self.set_state(phase=phase)
        return await self.emit("phase", {"phase": phase})

    async def emit_done(
        self,
        status: str,
        message: str | None = None,
        stats: dict[str, Any] | None = None,
    ) -> str:
        # The message goes into the state hash as well as the event. Events
        # expire with the run and are not part of the snapshot the transcript is
        # stored from, so without this the only record of *why* a run failed
        # lived in a client that was open at the time. Reopen the chat and a
        # provider outage, a cancelled run and an OOM kill all read "This run
        # did not finish."
        await self.set_state(status=status, message=message or "")
        data: dict[str, Any] = {"status": status}
        if message:
            data["message"] = message
        if stats:
            # What the run cost, for the footer under the answer.
            data["stats"] = stats
        return await self.emit(TERMINAL_EVENT, data)

    # ------------------------------------------------------------------ state

    async def set_state(self, **fields: Any) -> None:
        mapping = {k: ("" if v is None else str(v)) for k, v in fields.items()}
        async with self.redis.pipeline(transaction=False) as pipe:
            pipe.hset(self.state_key, mapping=mapping)
            pipe.expire(self.state_key, self.ttl)
            await pipe.execute()

    async def get_state(self) -> dict[str, str]:
        return await self.redis.hgetall(self.state_key)

    async def exists(self) -> bool:
        return bool(await self.redis.exists(self.state_key))

    async def snapshot(self) -> list[dict[str, Any]]:
        """Every block as it stands now, in the order the run emitted them."""
        async with self.redis.pipeline(transaction=False) as pipe:
            pipe.lrange(self.order_key, 0, -1)
            pipe.hgetall(self.blocks_key)
            order, raw = await pipe.execute()
        ordered = [json.loads(raw.pop(block_id)) for block_id in order if block_id in raw]
        # Whatever the list does not name (a run stored before it existed) still
        # gets shown, after the rest.
        return ordered + [json.loads(value) for value in raw.values()]

    async def cancel(self) -> None:
        await self.set_state(cancel="1")

    async def is_cancelled(self) -> bool:
        return await self.redis.hget(self.state_key, "cancel") == "1"

    # ------------------------------------------------------------------- read

    async def read(
        self, last_id: str = "0", block_ms: int = 15000
    ) -> AsyncIterator[StreamEvent | None]:
        """Yield events from `last_id` onward, then follow the stream live.

        Yields None when nothing arrived, so the caller can send an SSE
        heartbeat and notice client disconnects.

        An idle `XREAD BLOCK` reaches its deadline on both sides at once, and
        redis-py reports the client half as `TimeoutError`. That is not an
        error, it means "nothing happened yet", and treating it as one used to
        kill every SSE connection after one idle interval, which is exactly the
        long deep-research run this app exists to support.
        """
        cursor = last_id or "0"
        failures = 0
        while True:
            try:
                resp = await self.redis.xread({self.events_key: cursor}, count=200, block=block_ms)
                failures = 0
            except RedisTimeoutError:
                yield None
                continue
            except RedisConnectionError:
                # A dropped connection is worth one or two silent retries
                # (redis-py reconnects on the next command), but not an endless
                # hot loop if Redis is genuinely gone.
                failures += 1
                if failures > _MAX_READ_FAILURES:
                    raise
                log.warning("redis read failed (%d/%d), retrying", failures, _MAX_READ_FAILURES)
                await asyncio.sleep(min(2.0 * failures, 5.0))
                yield None
                continue

            if not resp:
                yield None
                continue
            for _stream, entries in resp:
                for entry_id, fields in entries:
                    cursor = entry_id
                    yield StreamEvent(id=entry_id, event=fields["event"], raw=fields["data"])


def apply_patch(doc: Any, ops: list[dict[str, Any]]) -> Any:
    """Minimal RFC-6902 subset: add / replace / remove, plus one extension.

    `append` concatenates a string onto the value at `path`. RFC-6902 has no way
    to express a string delta, and resending a whole streamed answer on every
    token is the wrong shape for this app.
    """
    for op in ops:
        kind = op.get("op")
        path = op.get("path", "")
        tokens = _tokens(path)
        if not tokens:
            continue
        parent = _resolve(doc, tokens[:-1])
        key = tokens[-1]
        if kind in ("add", "replace"):
            _set(parent, key, op.get("value"))
        elif kind == "append":
            current = _get(parent, key) or ""
            _set(parent, key, current + str(op.get("value", "")))
        elif kind == "remove":
            _remove(parent, key)
    return doc


def _tokens(path: str) -> list[str]:
    if not path or path == "/":
        return []
    return [t.replace("~1", "/").replace("~0", "~") for t in path.lstrip("/").split("/")]


def _resolve(doc: Any, tokens: list[str]) -> Any:
    node = doc
    for token in tokens:
        node = node[int(token)] if isinstance(node, list) else node[token]
    return node


def _get(parent: Any, key: str) -> Any:
    if isinstance(parent, list):
        return None if key == "-" else parent[int(key)]
    return parent.get(key)


def _set(parent: Any, key: str, value: Any) -> None:
    if isinstance(parent, list):
        if key == "-":
            parent.append(value)
        else:
            index = int(key)
            if index == len(parent):
                parent.append(value)
            else:
                parent[index] = value
    else:
        parent[key] = value


def _remove(parent: Any, key: str) -> None:
    if isinstance(parent, list):
        parent.pop(int(key))
    else:
        parent.pop(key, None)
