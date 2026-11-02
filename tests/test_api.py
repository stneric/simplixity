"""HTTP surface: contracts the frontend depends on, and the guards in front of them."""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from app import runs
from app.agent import blocks
from app.config import settings
from app.main import app
from app.store import chats, settings_store
from app.store.db import DEFAULT_USER


@pytest.fixture
async def client(redis, db, monkeypatch):
    # No lifespan: the fixtures already provide the resources it would set up.
    app.state.redis = redis
    app.state.db = db

    async def fake_run(stream, _request):
        await stream.emit_block(blocks.text_block("done"))
        await stream.emit_done("completed")

    async def fake_title(db_conn, chat_id, _query, _spec):
        await chats.set_title(db_conn, chat_id, "Generated title")

    monkeypatch.setattr(runs, "orchestrate", fake_run)
    # Naming a new chat is a real completion, and off by default. Patched anyway
    # so that a test which switches it on cannot reach the network or spend
    # anything to prove an HTTP contract.
    monkeypatch.setattr(runs, "_title", fake_title)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        # Signed in as the profile every pre-profile row belongs to, so these
        # contracts read exactly as they did before profiles existed.
        signed_in = await http.post("/api/profiles", json={"name": DEFAULT_USER})
        assert signed_in.status_code == 200, signed_in.text
        yield http


async def start_run(client, **body):
    response = await client.post("/api/chat", json={"query": "hello world", **body})
    assert response.status_code == 200, response.text
    # The run is a background task; let it finish so history assertions are stable.
    await asyncio.sleep(0.05)
    return response.json()


# ------------------------------------------------------------------- health


async def test_health_reports_each_dependency(client):
    body = (await client.get("/api/health")).json()
    assert body["status"] == "ok"
    assert body["redis"] is True and body["database"] is True


async def test_depths_are_served_from_the_agent_definitions(client):
    body = (await client.get("/api/depths")).json()
    assert [d["id"] for d in body["depths"]] == ["auto", "quick", "balanced", "deep"]
    assert body["default"] in {d["id"] for d in body["depths"]}
    # "simple" is a routing decision, not a setting. Offering it in the selector
    # would make "Auto" mean two things.
    assert "simple" not in {d["id"] for d in body["depths"]}


# --------------------------------------------------------------------- runs


async def test_starting_a_run_creates_the_chat_and_a_pending_message(client, db):
    body = await start_run(client)
    assert body["depth"] and body["model"]

    chat = await chats.get_chat(db, body["chat_id"])
    assert chat is not None
    assert chat["messages"][0]["query"] == "hello world"
    assert chat["messages"][0]["status"] == "completed"


async def test_a_failed_run_stores_why_it_failed(client, db, monkeypatch):
    """Terminal events are not blocks and expire with the run, so a reopened
    chat would read "This run did not finish." for a provider outage, a
    cancellation and an OOM kill alike, losing the one fact worth having."""

    async def fails(stream, _request):
        await stream.emit_block(blocks.text_block("half an answer"))
        await stream.emit_done("error", "some/model returned an empty stream")

    monkeypatch.setattr(runs, "orchestrate", fails)
    body = await start_run(client)

    chat = await chats.get_chat(db, body["chat_id"])
    message = chat["messages"][0]
    assert message["status"] == "error"
    reason = next(b for b in message["blocks"] if b["type"] == "notice")
    assert reason["data"]["level"] == "error"
    assert "empty stream" in reason["data"]["text"]


async def test_a_completed_run_stores_no_such_notice(client, db):
    body = await start_run(client)
    stored = (await chats.get_chat(db, body["chat_id"]))["messages"][0]
    assert not [b for b in stored["blocks"] if b["type"] == "notice"]


async def test_a_second_turn_stays_in_the_same_chat(client):
    first = await start_run(client)
    second = await start_run(client, chat_id=first["chat_id"])
    assert second["chat_id"] == first["chat_id"]


# ----------------------------------------------------------------- projects


async def test_a_project_holds_the_chats_started_inside_it(client, db):
    project = (await client.post("/api/projects", json={"name": "Kafka"})).json()

    body = await start_run(client, project_id=project["id"])

    filed = (await client.get(f"/api/chats?project_id={project['id']}")).json()["chats"]
    assert [c["id"] for c in filed] == [body["chat_id"]]
    listed = (await client.get("/api/projects")).json()["projects"]
    assert listed[0]["chats"] == 1


async def test_a_follow_up_never_moves_the_chat_it_belongs_to(client):
    """The composer sends the selected project on every send. Acting on it for
    an existing chat would drag a chat out of its project the moment someone
    asked a follow-up from another project's view."""
    home = (await client.post("/api/projects", json={"name": "Home"})).json()
    other = (await client.post("/api/projects", json={"name": "Other"})).json()
    first = await start_run(client, project_id=home["id"])

    await start_run(client, chat_id=first["chat_id"], project_id=other["id"])

    chat = (await client.get(f"/api/chats/{first['chat_id']}")).json()
    assert chat["project_id"] == home["id"]


async def test_a_chat_can_be_filed_and_unfiled_over_http(client):
    project = (await client.post("/api/projects", json={"name": "Kafka"})).json()
    body = await start_run(client)

    await client.patch(f"/api/chats/{body['chat_id']}", json={"project_id": project["id"]})
    assert (await client.get(f"/api/chats/{body['chat_id']}")).json()["project_id"] == project["id"]

    # The empty string is "no project": a nullable field cannot say that on
    # its own, because None already means "not stated".
    await client.patch(f"/api/chats/{body['chat_id']}", json={"project_id": ""})
    assert (await client.get(f"/api/chats/{body['chat_id']}")).json()["project_id"] is None


async def test_renaming_and_filing_are_independent(client):
    project = (await client.post("/api/projects", json={"name": "Kafka"})).json()
    body = await start_run(client)

    await client.patch(f"/api/chats/{body['chat_id']}", json={"title": "A better name"})
    chat = (await client.get(f"/api/chats/{body['chat_id']}")).json()
    assert chat["title"] == "A better name" and chat["project_id"] is None

    await client.patch(f"/api/chats/{body['chat_id']}", json={"project_id": project["id"]})
    chat = (await client.get(f"/api/chats/{body['chat_id']}")).json()
    assert chat["title"] == "A better name" and chat["project_id"] == project["id"]


async def test_deleting_a_project_over_http_keeps_the_chats(client):
    project = (await client.post("/api/projects", json={"name": "Temporary"})).json()
    body = await start_run(client, project_id=project["id"])

    assert (await client.delete(f"/api/projects/{project['id']}")).status_code == 200

    assert (await client.get(f"/api/chats/{body['chat_id']}")).status_code == 200
    assert (await client.get(f"/api/projects/{project['id']}/chats")).status_code == 404


async def test_filing_into_a_project_that_does_not_exist_is_refused(client):
    body = await start_run(client)
    response = await client.patch(f"/api/chats/{body['chat_id']}", json={"project_id": "deadbeef"})
    assert response.status_code == 404


async def test_unknown_ids_are_404_not_500(client):
    assert (await client.get("/api/runs/deadbeef")).status_code == 404
    assert (await client.post("/api/runs/deadbeef/cancel")).status_code == 404
    assert (await client.get("/api/chats/deadbeef")).status_code == 404


async def test_malformed_ids_are_rejected_before_touching_storage(client):
    assert (await client.get("/api/runs/../../etc/passwd")).status_code == 404
    assert (
        await client.post("/api/chat", json={"query": "x", "chat_id": "a b/c"})
    ).status_code == 422


async def test_an_empty_query_is_rejected_with_a_readable_message(client):
    response = await client.post("/api/chat", json={"query": "   "})
    # Whitespace passes min_length but is stripped before the run starts, so the
    # guard that matters is that it never 500s.
    assert response.status_code in (200, 422)
    assert (await client.post("/api/chat", json={"query": ""})).status_code == 422
    assert "query" in (await client.post("/api/chat", json={"query": ""})).json()["detail"]


async def test_capacity_is_refused_with_503_and_a_retry_hint(client, monkeypatch):
    monkeypatch.setattr("app.agent.runner.at_capacity", lambda: True)
    response = await client.post("/api/chat", json={"query": "hello"})
    assert response.status_code == 503
    assert response.headers["retry-after"]


async def test_simultaneous_starts_cannot_share_the_last_slot(client, monkeypatch):
    """Setting a run up awaits the database between deciding there is room and
    spawning, and without a claim every request arriving in that gap sees the
    same free slot."""
    from app.agent import runner

    monkeypatch.setattr(settings, "max_concurrent_runs", 1)
    monkeypatch.setattr(settings, "rate_limit_runs_per_minute", 0)
    hold = asyncio.Event()

    async def slow_run(stream, _request):
        await hold.wait()
        await stream.emit_done("completed")

    monkeypatch.setattr(runs, "orchestrate", slow_run)

    responses = await asyncio.gather(
        *(client.post("/api/chat", json={"query": "hi"}) for _ in range(4))
    )
    assert sorted(r.status_code for r in responses) == [200, 503, 503, 503]
    hold.set()
    await asyncio.sleep(0.05)
    # Refused setups gave their claim back, and the finished run its slot.
    assert not runner.at_capacity()


async def test_rate_limiting_kicks_in_and_says_when_to_retry(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_runs_per_minute", 2)
    statuses = [
        (await client.post("/api/chat", json={"query": "hi"})).status_code for _ in range(4)
    ]
    assert statuses[:2] == [200, 200]
    assert 429 in statuses[2:]


# ------------------------------------------------------------------- history


async def test_chats_list_reopen_rename_and_delete(client, db):
    body = await start_run(client)
    chat_id = body["chat_id"]

    listed = (await client.get("/api/chats")).json()["chats"]
    assert any(c["id"] == chat_id for c in listed)

    assert (
        await client.patch(f"/api/chats/{chat_id}", json={"title": "Renamed"})
    ).status_code == 200
    assert (await client.get(f"/api/chats/{chat_id}")).json()["title"] == "Renamed"

    assert (await client.delete(f"/api/chats/{chat_id}")).status_code == 200
    assert (await client.get(f"/api/chats/{chat_id}")).status_code == 404
    assert await chats.list_chats(db) == []


async def test_search_matches_message_text_not_only_titles(client):
    await start_run(client, query="a question about hydrothermal vents")
    hits = (await client.get("/api/chats", params={"search": "hydrothermal"})).json()["chats"]
    assert len(hits) == 1


async def test_search_takes_wildcards_literally(client):
    await start_run(client, query="what does 50% mean here")
    await start_run(client, query="what does 5000 mean here")
    hits = (await client.get("/api/chats", params={"search": "50%"})).json()["chats"]
    assert len(hits) == 1
    assert not (await client.get("/api/chats", params={"search": "snake_case"})).json()["chats"]


async def test_deleting_a_chat_stops_the_run_still_answering_in_it(client, redis, monkeypatch):
    """Closing a tab never cancels a run, by design, so a deleted chat went on
    spending the budget and holding a slot to answer into rows that were gone."""
    from app.store.stream import RunStream

    async def waits_for_stop(stream, _request):
        # Polled like the real run checks it between steps: the flag lives in
        # Redis, there is no event to wait on.
        while not await stream.is_cancelled():  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        await stream.emit_done("cancelled", "Stopped")

    monkeypatch.setattr(runs, "orchestrate", waits_for_stop)
    response = await client.post("/api/chat", json={"query": "a long one"})
    body = response.json()
    await asyncio.sleep(0.02)

    assert (await client.delete(f"/api/chats/{body['chat_id']}")).status_code == 200
    assert await RunStream(redis, body["run_id"]).is_cancelled()


async def test_a_project_that_cannot_be_read_says_so_in_the_answer(client, db, monkeypatch):
    """Answered as an ordinary chat is the only way left to answer at all, and
    it must not look like an answer from the papers."""
    from app.store import library as library_store

    async def broken(*_args, **_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(library_store, "project_has_library", broken)
    project = (await client.post("/api/projects", json={"name": "Thesis"})).json()
    body = await start_run(client, project_id=project["id"])

    stored = (await chats.get_chat(db, body["chat_id"]))["messages"][0]
    notices = [b["data"]["text"] for b in stored["blocks"] if b["type"] == "notice"]
    assert any("not limited to your papers" in text for text in notices)


# ------------------------------------------------------------------ settings


async def test_settings_round_trip(client):
    current = (await client.get("/api/settings")).json()
    current["depth"] = "deep"
    saved = await client.put("/api/settings", json=current)
    assert saved.status_code == 200
    assert (await client.get("/api/settings")).json()["depth"] == "deep"


async def test_the_router_model_and_home_location_round_trip(client):
    current = (await client.get("/api/settings")).json()
    saved = await client.put(
        "/api/settings",
        json={
            **current,
            "router_model": "google/gemini-2.0-flash-lite:free",
            "router_provider": "openrouter",
            "home_location": "  Reutlingen  ",
        },
    )
    assert saved.status_code == 200, saved.text

    body = (await client.get("/api/settings")).json()
    assert body["router_model"] == "google/gemini-2.0-flash-lite:free"
    # The weather engines hand the whole string to a geocoder, so it is trimmed
    # before it is stored rather than at the point of use.
    assert body["home_location"] == "Reutlingen"


async def test_an_empty_router_means_the_chat_model_triages_too(client):
    """The default, and the state a single-model install must stay valid in."""
    current = (await client.get("/api/settings")).json()
    saved = await client.put(
        "/api/settings", json={**current, "router_model": "", "router_provider": ""}
    )
    assert saved.status_code == 200
    assert saved.json()["router_model"] == ""


async def test_new_chats_keep_their_question_as_a_name_by_default(client):
    """The title call happens after the run's footer is written, so nothing it
    spends can appear in the price the run reported. Off is therefore the
    default, and a chat named after its first line is the whole feature."""
    body = await start_run(client)
    chat = (await client.get(f"/api/chats/{body['chat_id']}")).json()
    assert chat["title"] == "hello world"


async def test_titles_are_written_when_the_setting_asks_for_them(client):
    current = (await client.get("/api/settings")).json()
    saved = await client.put("/api/settings", json={**current, "title_enabled": True})
    assert saved.status_code == 200, saved.text

    body = await start_run(client)
    chat = (await client.get(f"/api/chats/{body['chat_id']}")).json()
    assert chat["title"] == "Generated title"


async def test_the_title_model_round_trips(client):
    current = (await client.get("/api/settings")).json()
    saved = await client.put(
        "/api/settings",
        json={
            **current,
            "title_enabled": True,
            "title_model": "  google/gemini-2.0-flash-lite:free  ",
            "title_provider": "openrouter",
        },
    )
    assert saved.status_code == 200, saved.text

    body = (await client.get("/api/settings")).json()
    assert body["title_model"] == "google/gemini-2.0-flash-lite:free"
    assert body["title_enabled"] is True
    assert (
        await client.put("/api/settings", json={**body, "title_provider": "hal9000"})
    ).status_code == 422


def test_switching_titles_on_never_demands_a_second_decision():
    """Whatever was chosen for titles, else the router, else the chat model.
    The picker in the settings modal resolves it the same way, so what it shows
    is what will run."""
    saved = settings_store.UISettings(
        chat_model="chat/model",
        chat_provider="openrouter",
        router_model="router/model",
        router_provider="groq",
    )

    assert runs._title_spec(saved) is None  # off by default

    on = saved.model_copy(update={"title_enabled": True})
    assert runs._title_spec(on) == runs.TitleSpec("router/model", "groq")

    no_router = on.model_copy(update={"router_model": "", "router_provider": ""})
    assert runs._title_spec(no_router) == runs.TitleSpec("chat/model", "openrouter")

    chosen = on.model_copy(update={"title_model": "tiny/model", "title_provider": "ollama"})
    assert runs._title_spec(chosen) == runs.TitleSpec("tiny/model", "ollama")


async def test_auto_is_a_depth_the_settings_accept(client):
    current = (await client.get("/api/settings")).json()
    assert (await client.put("/api/settings", json={**current, "depth": "auto"})).status_code == 200
    assert (await client.get("/api/settings")).json()["depth"] == "auto"
    # `simple` is a routing decision, never a saved setting.
    assert (
        await client.put("/api/settings", json={**current, "depth": "simple"})
    ).status_code == 422


async def test_settings_reject_unknown_providers_and_depths(client):
    current = (await client.get("/api/settings")).json()
    assert (
        await client.put("/api/settings", json={**current, "chat_provider": "hal9000"})
    ).status_code == 422
    assert (
        await client.put("/api/settings", json={**current, "depth": "turbo"})
    ).status_code == 422
    # extra="forbid": a typo must not be silently persisted
    assert (await client.put("/api/settings", json={**current, "typo": 1})).status_code == 422


async def test_groq_is_a_chat_provider_but_has_no_embeddings(client):
    current = (await client.get("/api/settings")).json()
    assert (
        await client.put("/api/settings", json={**current, "chat_provider": "groq"})
    ).status_code == 200
    assert (
        await client.put("/api/settings", json={**current, "embedding_provider": "groq"})
    ).status_code == 422


# ------------------------------------------------------------------ security


async def test_security_headers_are_present_on_every_response(client):
    headers = (await client.get("/api/health")).headers
    assert "default-src 'self'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
