"""Profiles: people sharing one install see only their own history.

Two layers, pinned separately. Separation: another profile's chat, project or
settings read as missing, never as someone else's. Sessions: nothing is served
without one, a password is asked for when a profile has one, and a session
cannot be forged or outlive the password it was issued under.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from app import runs
from app.agent import blocks
from app.api.deps import SESSION_COOKIE
from app.main import app
from app.store import chats, profiles, settings_store
from app.store.db import DEFAULT_USER


@pytest.fixture
async def browser(redis, db, monkeypatch):
    """A factory for independent browsers, each with its own cookie jar."""
    app.state.redis = redis
    app.state.db = db

    async def fake_run(stream, _request):
        await stream.emit_block(blocks.text_block("done"))
        await stream.emit_done("completed")

    monkeypatch.setattr(runs, "orchestrate", fake_run)
    opened: list[AsyncClient] = []

    def make() -> AsyncClient:
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        opened.append(client)
        return client

    yield make
    for client in opened:
        await client.aclose()


@pytest.fixture
async def as_user(browser):
    """A browser signed into a fresh, open profile of the given name."""

    async def make(name: str, password: str = "") -> AsyncClient:
        client = browser()
        response = await client.post("/api/profiles", json={"name": name, "password": password})
        assert response.status_code == 200, response.text
        return client

    return make


async def ask(client: AsyncClient, **body) -> dict:
    response = await client.post("/api/chat", json={"query": "hello world", **body})
    assert response.status_code == 200, response.text
    await asyncio.sleep(0.05)
    return response.json()


# --------------------------------------------------------------- separation


async def test_each_profile_lists_only_its_own_chats(as_user):
    anna, ben = await as_user("anna"), await as_user("ben")
    mine = await ask(anna)

    assert [c["id"] for c in (await anna.get("/api/chats")).json()["chats"]] == [mine["chat_id"]]
    assert (await ben.get("/api/chats")).json()["chats"] == []


async def test_another_profiles_chat_reads_as_missing(as_user):
    anna, ben = await as_user("anna"), await as_user("ben")
    chat_id = (await ask(anna))["chat_id"]

    assert (await ben.get(f"/api/chats/{chat_id}")).status_code == 404
    assert (await ben.patch(f"/api/chats/{chat_id}", json={"title": "x"})).status_code == 404
    assert (await ben.delete(f"/api/chats/{chat_id}")).status_code == 404
    # And the refused delete took nothing with it.
    assert (await anna.get(f"/api/chats/{chat_id}")).status_code == 200


async def test_a_run_cannot_append_to_another_profiles_chat(as_user, db):
    """Otherwise the run would read her history into its prompt."""
    anna, ben = await as_user("anna"), await as_user("ben")
    chat_id = (await ask(anna))["chat_id"]

    response = await ben.post("/api/chat", json={"query": "hi", "chat_id": chat_id})
    assert response.status_code == 404
    assert len((await chats.get_chat(db, chat_id))["messages"]) == 1


async def test_another_profiles_run_reads_as_missing(as_user, browser):
    """A run id alone is not enough: without the session check, whoever held
    one could read the question and answer, or stop the run."""
    anna, ben = await as_user("anna"), await as_user("ben")
    run_id = (await ask(anna))["run_id"]

    assert (await anna.get(f"/api/runs/{run_id}")).status_code == 200
    assert (await ben.get(f"/api/runs/{run_id}")).status_code == 404
    assert (await ben.get(f"/api/runs/{run_id}/events")).status_code == 404
    assert (await ben.post(f"/api/runs/{run_id}/cancel")).status_code == 404
    assert (await browser().get(f"/api/runs/{run_id}")).status_code == 401


async def test_projects_are_per_profile(as_user):
    anna, ben = await as_user("anna"), await as_user("ben")
    project = (await anna.post("/api/projects", json={"name": "Thesis"})).json()

    assert (await ben.get("/api/projects")).json()["projects"] == []
    assert (await ben.get(f"/api/projects/{project['id']}/chats")).status_code == 404
    assert (await ben.delete(f"/api/projects/{project['id']}")).status_code == 404
    # Her corpus must not become his sources.
    refused = await ben.post("/api/chat", json={"query": "hi", "project_id": project["id"]})
    assert refused.status_code == 404


async def test_settings_are_per_profile_and_start_from_the_default(as_user, db):
    base = await settings_store.load(db)
    await settings_store.save(db, base.model_copy(update={"home_location": "Berlin"}))
    anna = await as_user("anna")

    # A new profile inherits what the install was set up with...
    assert (await anna.get("/api/settings")).json()["home_location"] == "Berlin"
    changed = {**(await anna.get("/api/settings")).json(), "home_location": "Wien"}
    assert (await anna.put("/api/settings", json=changed)).status_code == 200
    # ...and changing it leaves everyone else's alone.
    assert (await settings_store.load(db)).home_location == "Berlin"
    assert (await anna.get("/api/settings")).json()["home_location"] == "Wien"


async def test_favourite_models_are_per_profile_and_survive_a_settings_save(as_user):
    anna, ben = await as_user("anna"), await as_user("ben")
    starred = {"models": [{"provider": "openrouter", "id": "a/b"}] * 2}
    saved = await anna.put("/api/settings/favorites", json=starred)
    # Starred twice from two tabs is still one favourite.
    assert saved.json()["models"] == [{"provider": "openrouter", "id": "a/b"}]

    # The settings modal PUTs everything it loaded; that must not touch them.
    await anna.put("/api/settings", json=(await anna.get("/api/settings")).json())
    assert len((await anna.get("/api/settings/favorites")).json()["models"]) == 1
    assert (await ben.get("/api/settings/favorites")).json()["models"] == []


# ----------------------------------------------------------------- sessions


async def test_nothing_personal_is_served_without_a_session(browser):
    """A 401 is the frontend's cue to show the picker."""
    anonymous = browser()
    for path in ("/api/chats", "/api/projects", "/api/settings"):
        assert (await anonymous.get(path)).status_code == 401, path
    assert (await anonymous.post("/api/chat", json={"query": "hi"})).status_code == 401


async def test_the_picker_lists_profiles_and_who_is_protected(as_user, browser):
    await as_user("anna", "geheim")
    await as_user("ben")

    body = (await browser().get("/api/profiles")).json()
    assert body["current"] is None
    assert body["profiles"] == [
        {"name": "anna", "protected": True},
        {"name": "ben", "protected": False},
    ]
    assert "password_hash" not in str(body)


async def test_a_protected_profile_needs_its_password(as_user, browser):
    await as_user("anna", "geheim")
    guest = browser()

    wrong = await guest.post("/api/profiles/login", json={"name": "anna", "password": "nope"})
    assert wrong.status_code == 401
    assert (await guest.get("/api/chats")).status_code == 401

    right = await guest.post("/api/profiles/login", json={"name": "anna", "password": "geheim"})
    assert right.status_code == 200
    assert (await guest.get("/api/chats")).status_code == 200


async def test_an_open_profile_signs_in_with_a_click(as_user, browser):
    await as_user("ben")
    guest = browser()
    assert (await guest.post("/api/profiles/login", json={"name": "ben"})).status_code == 200
    assert (await guest.get("/api/profiles")).json()["current"] == "ben"


async def test_creating_cannot_take_over_an_existing_profile(as_user, browser):
    await as_user("anna", "geheim")
    again = await browser().post("/api/profiles", json={"name": "anna"})
    assert again.status_code == 409


async def test_a_forged_session_is_refused(as_user, browser):
    await as_user("anna", "geheim")
    forger = browser()
    forger.cookies.set(SESSION_COOKIE, "anna." + "0" * 64)
    assert (await forger.get("/api/chats")).status_code == 401
    forger.cookies.set(SESSION_COOKIE, "anna")
    assert (await forger.get("/api/chats")).status_code == 401


async def test_changing_the_password_ends_every_other_session(as_user, browser):
    anna = await as_user("anna", "alt")
    other = browser()
    await other.post("/api/profiles/login", json={"name": "anna", "password": "alt"})

    wrong = await anna.put("/api/profiles/password", json={"current": "x", "password": "neu"})
    assert wrong.status_code == 401
    changed = await anna.put("/api/profiles/password", json={"current": "alt", "password": "neu"})
    assert changed.status_code == 200

    # The browser that changed it keeps going; the other one is signed out.
    assert (await anna.get("/api/chats")).status_code == 200
    assert (await other.get("/api/chats")).status_code == 401


async def test_a_password_can_be_removed(as_user, browser):
    anna = await as_user("anna", "alt")
    await anna.put("/api/profiles/password", json={"current": "alt", "password": ""})
    assert (await browser().post("/api/profiles/login", json={"name": "anna"})).status_code == 200


async def test_logout_signs_the_browser_out(as_user):
    anna = await as_user("anna")
    await anna.post("/api/profiles/logout")
    assert (await anna.get("/api/chats")).status_code == 401


async def test_names_are_validated(browser):
    for bad in ("", "Anna", "a.b", "x" * 33, "robert'); drop table chats;--"):
        response = await browser().post("/api/profiles", json={"name": bad})
        assert response.status_code == 422, bad


# ----------------------------------------------------------------- upgrades


async def test_history_from_before_profiles_appears_as_the_default_profile(tmp_path):
    """An upgraded install's chats must be reachable from the picker."""
    from app.store import db as db_module

    path = str(tmp_path / "old.db")
    conn = await db_module.connect(path)
    await conn.execute(
        "INSERT INTO chats (id, title, created_at, updated_at) VALUES ('old', 't', '', '')"
    )
    await conn.execute("DELETE FROM profiles")
    await conn.commit()
    await conn.close()

    conn = await db_module.connect(path)
    try:
        assert await chats.owner(conn, "old") == DEFAULT_USER
        assert [p["name"] for p in await profiles.list_profiles(conn)] == [DEFAULT_USER]
    finally:
        await conn.close()


def test_passwords_are_hashed_and_checked():
    stored = profiles.hash_password("geheim")
    assert "geheim" not in stored
    assert profiles.check_password("geheim", stored)
    assert not profiles.check_password("Geheim", stored)
    assert not profiles.check_password("geheim", "garbage")
    # Two hashes of one password differ: the salt is per hash.
    assert profiles.hash_password("geheim") != stored


async def test_the_session_cookie_is_secure_over_tls_and_only_then(redis, db):
    """Behind TLS the cookie must never travel in the clear again; on a plain
    http://localhost install `Secure` would make the browser drop it at once."""
    app.state.redis = redis
    app.state.db = db
    for base, secure in (("https://test", True), ("http://test", False)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=base) as client:
            name = "tls" if secure else "plain"
            response = await client.post("/api/profiles", json={"name": name})
            cookie = response.headers["set-cookie"].lower()
            assert ("secure" in cookie) is secure, cookie


def test_the_rate_limit_ignores_a_forwarded_header_it_did_not_vouch_for():
    """uvicorn applies X-Forwarded-For for trusted proxies only. Reading it
    again here let any client pick a fresh address per request."""
    from types import SimpleNamespace

    from app.api.deps import client_ip

    request = SimpleNamespace(
        client=SimpleNamespace(host="203.0.113.7"),
        headers={"x-forwarded-for": "1.2.3.4"},
    )
    assert client_ip(request) == "203.0.113.7"
