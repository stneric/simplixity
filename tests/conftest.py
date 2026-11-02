"""Shared fixtures. Everything here is in-memory: the suite must not need
Redis, SQLite on disk, an API key or a network."""

from __future__ import annotations

import pytest
from fakeredis import aioredis

from app.store import db as db_module


@pytest.fixture
async def redis():
    client = aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
async def db(tmp_path):
    conn = await db_module.connect(str(tmp_path / "test.db"))
    yield conn
    await conn.close()
