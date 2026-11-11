"""Model discovery, depth presets and persisted UI settings."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.agent import depth as depth_module
from app.api.deps import DbDep, RedisDep, UserDep
from app.providers import catalog
from app.search import lenses as lens_module
from app.store import settings_store

router = APIRouter()


@router.get("/api/models")
async def list_models(
    redis: RedisDep,
    kind: str = Query(default=catalog.CHAT, pattern="^(chat|embedding)$"),
    refresh: bool = False,
) -> dict:
    """Models grouped as free / local / all, for the picker.

    `problems` is part of the success response: a provider being
    unreachable is information the UI should show, not a reason to fail the
    request and leave the user with an empty dropdown.
    """
    return await catalog.list_models(redis, kind, refresh=refresh)


@router.get("/api/depths")
async def list_depths() -> dict:
    """The depth presets, served from the same definitions the agent runs on so
    the UI cannot describe a setting that no longer exists."""
    return {"depths": depth_module.catalog(), "default": depth_module.DEFAULT_DEPTH}


@router.get("/api/lenses")
async def list_lenses() -> dict:
    """The source corpora a run can search, from the definitions the searches use.

    Served like the depth presets so the selector cannot offer a corpus that no
    longer exists, or hide one that was added.
    """
    return {"lenses": lens_module.catalog(), "default": list(lens_module.DEFAULT_LENSES)}


@router.get("/api/settings", response_model=settings_store.UISettings)
async def get_settings(db: DbDep, user: UserDep) -> settings_store.UISettings:
    return await settings_store.load(db, user)


@router.put("/api/settings", response_model=settings_store.UISettings)
async def put_settings(
    body: settings_store.UISettings, db: DbDep, user: UserDep
) -> settings_store.UISettings:
    # Groq hosts no embedding models, so it is only a valid chat provider. The
    # optional models' providers may additionally be empty, which is how a model
    # that is switched off is saved, and means "where the chat model runs".
    chat_providers = (catalog.OPENROUTER, catalog.GROQ, catalog.OLLAMA, catalog.LMSTUDIO)
    allowed = {
        "chat_provider": chat_providers,
        "embedding_provider": (catalog.OPENROUTER, catalog.OLLAMA, catalog.LMSTUDIO),
        "router_provider": (*chat_providers, ""),
        "title_provider": (*chat_providers, ""),
        "redact_provider": (catalog.OLLAMA, catalog.LMSTUDIO, ""),
        "literature_provider": (*chat_providers, ""),
        "judge_provider": (*chat_providers, ""),
    }
    for field, known in allowed.items():
        provider = getattr(body, field)
        if provider not in known:
            raise HTTPException(status_code=422, detail=f"Unknown provider {provider!r}")
    return await settings_store.save(db, body, user)


@router.get("/api/settings/favorites", response_model=settings_store.Favorites)
async def get_favorites(db: DbDep, user: UserDep) -> settings_store.Favorites:
    """The models this profile starred in a picker, in the order starred."""
    return await settings_store.load_favorites(db, user)


@router.put("/api/settings/favorites", response_model=settings_store.Favorites)
async def put_favorites(
    body: settings_store.Favorites, db: DbDep, user: UserDep
) -> settings_store.Favorites:
    # No provider check, unlike `put_settings`: a favourite is a bookmark, not
    # a model anything runs on, and one whose provider went away is simply not
    # listed by the picker until it comes back.
    return await settings_store.save_favorites(db, body, user)
