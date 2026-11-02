"""Shared fixtures. Everything here is in-memory: the suite must not need
Redis, SQLite on disk, an API key or a network."""

from __future__ import annotations

import pytest
from fakeredis import aioredis

from app.config import Settings, settings
from app.store import db as db_module

# `settings` reads the developer's .env at import. The suite runs on the
# defaults instead, so a local run behaves like CI and no test passes only
# because a real API key happens to be configured. Tests that need a value
# set it with monkeypatch.
_defaults = Settings(_env_file=None)
for _name in Settings.model_fields:
    setattr(settings, _name, getattr(_defaults, _name))


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
