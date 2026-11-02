"""Persisted UI settings.

Server-side rather than localStorage: the backend needs these as defaults for
any request that omits a model, and a single-user install should not lose its
configuration to a cleared browser cache.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import aiosqlite
from pydantic import BaseModel, Field, field_validator

from app.agent import depth as depth_module
from app.config import settings as env
from app.search import lenses as lens_module
from app.store.db import DEFAULT_USER

SETTINGS_KEY = "ui"


class UISettings(BaseModel):
    """Everything the picker remembers. Extra fields are rejected so a typo in a
    PUT surfaces immediately instead of being silently persisted."""

    model_config = {"extra": "forbid"}

    chat_provider: str = Field(default_factory=lambda: env.default_provider)
    chat_model: str = Field(default_factory=lambda: env.default_model)
    embedding_provider: str = Field(default_factory=lambda: env.embedding_provider)
    embedding_model: str = Field(default_factory=lambda: env.embedding_model)
    embedding_enabled: bool = Field(default_factory=lambda: env.embedding_enabled)
    # A small, fast model for triage only. Empty means "use the chat model",
    # which is the default. An install with one model configured must not have
    # to configure a second one to work.
    router_model: str = Field(default_factory=lambda: env.router_model or "")
    router_provider: str = Field(default_factory=lambda: env.router_provider)
    # Whether a new chat gets a model-written name. Off by default: the call runs
    # after the footer is written and therefore cannot be included in the price
    # the run reports, and the truncated question already names the chat.
    title_enabled: bool = Field(default_factory=lambda: env.title_enabled)
    # Empty means "whatever triages", the router if there is one and the chat
    # model otherwise. See `runs._title_spec`.
    title_model: str = Field(default_factory=lambda: env.title_model or "")
    title_provider: str = Field(default_factory=lambda: env.title_provider)
    depth: str = Field(default_factory=lambda: depth_module.get(env.default_depth).id)
    # Which corpora searches may draw on. A list because these combine: a
    # question can want the general web *and* the paper databases.
    lenses: list[str] = Field(default_factory=lambda: list(env.default_lenses))
    # Where a question without a place means. Not derivable from the browser:
    # the CSP and the permissions policy exclude geolocation.
    home_location: str = Field(default_factory=lambda: env.home_location, max_length=80)
    # The local model the redact tool runs on. Empty means "the first local
    # model discovery finds". Only Ollama and LM Studio are accepted, see
    # `api.models.put_settings`: the tool exists so a document stays on this
    # machine, and a remote model here would quietly undo that.
    redact_provider: str = ""
    redact_model: str = ""
    # The model the literature page talks, plans queries and screens with.
    # Empty means the chat model. Its own setting because an install whose chat
    # model is a small local one, fine for a quick answer, still wants a capable
    # model to judge forty abstracts against a set of criteria.
    literature_provider: str = ""
    literature_model: str = ""
    # The model that rules on the citations once an answer is written
    # (`verify._judge`). Empty keeps the old rule: the router on the web, the
    # writer's own model in a literature project. A model of another family is
    # the point of setting one: a writer judging itself shares its own blind
    # spots, and waves through the sentence it was sure enough to write.
    judge_provider: str = ""
    judge_model: str = ""

    @field_validator("home_location")
    @classmethod
    def _tidy_place(cls, value: str) -> str:
        """A bare place name. The weather engines hand the whole string to a
        geocoder, so stray whitespace is the difference between a hit and a
        crash; see app/search/weather.py."""
        return " ".join(str(value or "").split())[:80]

    @field_validator(
        "router_model", "title_model", "redact_model", "literature_model", "judge_model"
    )
    @classmethod
    def _tidy_model(cls, value: str) -> str:
        """No whitelist: models are discovered dynamically, exactly as the chat
        model is, and an id this build has never heard of may still be valid."""
        return str(value or "").strip()

    @field_validator("depth")
    @classmethod
    def _known_depth(cls, value: str) -> str:
        if value not in depth_module.DEPTHS:
            raise ValueError(f"unknown depth {value!r}")
        return value

    @field_validator("lenses")
    @classmethod
    def _known_lenses(cls, value: list[str]) -> list[str]:
        """Unknown ids are an error here, unlike at search time, where an old
        client's stale id must degrade rather than fail. A PUT is a decision."""
        # "auto" is not a corpus and is not in `LENSES` (nothing may
        # search it), but it is a valid thing to have chosen.
        if lens_module.wants_auto(value):
            return [lens_module.AUTO]
        unknown = [i for i in value if i not in lens_module.LENSES]
        if unknown:
            raise ValueError(f"unknown source lens {', '.join(unknown)}")
        # Empty means "search nothing", which is never what anyone wants.
        return value or list(lens_module.DEFAULT_LENSES)


def _key(user: str) -> str:
    """One settings row per profile. The default profile keeps the bare key, so
    an upgraded install's saved choices are still its own."""
    return SETTINGS_KEY if user == DEFAULT_USER else f"{SETTINGS_KEY}:{user}"


async def load(conn: aiosqlite.Connection, user: str = DEFAULT_USER) -> UISettings:
    async with conn.execute("SELECT value FROM settings WHERE key = ?", (_key(user),)) as cursor:
        row = await cursor.fetchone()
    if row is None:
        # A new profile starts from the default profile's choices rather than
        # the environment's: whoever set the install up already picked models
        # that work here, and a second person should not have to find them again.
        return UISettings() if user == DEFAULT_USER else await load(conn, DEFAULT_USER)
    try:
        return UISettings.model_validate_json(row["value"])
    except Exception:
        # A stored blob from an older schema must not brick the app.
        return UISettings()


async def save(
    conn: aiosqlite.Connection, value: UISettings, user: str = DEFAULT_USER
) -> UISettings:
    await conn.execute(
        """
        INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (
            _key(user),
            json.dumps(value.model_dump()),
            datetime.now(UTC).isoformat(),
        ),
    )
    await conn.commit()
    return value


# ── favourite models ───────────────────────────────────────────────────
#
# Kept in a row of their own rather than as a field of `UISettings`: every
# picker on every page stars and unstars, and most of them have no `Settings`
# object to go through. The settings modal PUTs the whole `UISettings` it
# loaded, so a list stored inside it would be overwritten with the stale copy
# the moment anything else in the modal changed.

FAVORITES_KEY = "favorites"
FAVORITES_LIMIT = 200


class FavoriteModel(BaseModel):
    model_config = {"extra": "forbid"}

    provider: str = Field(min_length=1, max_length=40)
    id: str = Field(min_length=1, max_length=300)


class Favorites(BaseModel):
    model_config = {"extra": "forbid"}

    models: list[FavoriteModel] = Field(default_factory=list, max_length=FAVORITES_LIMIT)

    @field_validator("models")
    @classmethod
    def _once_each(cls, value: list[FavoriteModel]) -> list[FavoriteModel]:
        """A model starred twice, from two open tabs, is still one favourite."""
        seen: set[tuple[str, str]] = set()
        unique = []
        for model in value:
            if (model.provider, model.id) not in seen:
                seen.add((model.provider, model.id))
                unique.append(model)
        return unique


def _favorites_key(user: str) -> str:
    return f"{FAVORITES_KEY}:{user}"


async def load_favorites(conn: aiosqlite.Connection, user: str = DEFAULT_USER) -> Favorites:
    async with conn.execute(
        "SELECT value FROM settings WHERE key = ?", (_favorites_key(user),)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return Favorites()
    try:
        return Favorites.model_validate_json(row["value"])
    except Exception:
        # Same courtesy as `load`: an unreadable row is an empty list, not a 500.
        return Favorites()


async def save_favorites(
    conn: aiosqlite.Connection, value: Favorites, user: str = DEFAULT_USER
) -> Favorites:
    await conn.execute(
        """
        INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (_favorites_key(user), value.model_dump_json(), datetime.now(UTC).isoformat()),
    )
    await conn.commit()
    return value
