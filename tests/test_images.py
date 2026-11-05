"""Pictures attached to a chat: stored on their own, shown to a model that
sees, and never turning the chat into one that answers only from its files."""

from __future__ import annotations

import struct
import zlib

import pytest
import test_library
from test_agent import FakeLLM, collect, request

from app import runs
from app.agent import orchestrator, prompts
from app.store import attachments as attachment_store
from app.store import chats, images
from app.store.db import DEFAULT_USER
from app.store.stream import RunStream

client = test_library.client
offline_embed = test_library.offline_embed


def png(seed: int = 0) -> bytes:
    """A real 1x1 PNG, different per seed so the hashes differ."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    raw = b"\x00" + bytes([seed % 256, 0, 0])
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def test_pictures_are_known_by_their_bytes():
    assert images.mime_of(png()) == "image/png"
    assert images.mime_of(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert images.mime_of(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert images.mime_of(b"GIF89a...") == "image/gif"
    assert images.mime_of(b"%PDF-1.7") is None


async def test_a_picture_is_stored_shown_and_removed_over_http(client, db):
    uploaded = await client.post(
        "/api/chats/newchat2/files", content=png(), headers={"X-Filename": "Foto%201.png"}
    )
    assert uploaded.status_code == 200, uploaded.text
    file = uploaded.json()["file"]
    assert file["kind"] == "image" and file["name"] == "Foto 1.png"
    assert file["url"] == f"/api/chats/newchat2/images/{file['id']}"

    shown = await client.get(file["url"])
    assert shown.status_code == 200 and shown.content == png()
    assert shown.headers["content-type"] == "image/png"

    # A picture is not a document: the chat does not become one that answers
    # only from its files.
    assert not await attachment_store.has_files(db, "newchat2", DEFAULT_USER)
    listed = (await client.get("/api/chats/newchat2/files")).json()["files"]
    assert [f["id"] for f in listed] == [file["id"]]

    assert (await client.delete(f"/api/chats/newchat2/files/{file['id']}")).status_code == 200
    assert (await client.get(file["url"])).status_code == 404


async def test_another_profiles_picture_cannot_be_seen(client, db):
    theirs, _ = await images.add(db, "pending2", "someone-else", "a.png", png())
    assert (await client.get(f"/api/chats/pending2/images/{theirs['id']}")).status_code == 404
    # Nor can anything be attached under their chat id.
    refused = await client.post(
        "/api/chats/pending2/files", content=png(1), headers={"X-Filename": "b.png"}
    )
    assert refused.status_code == 404


async def test_a_question_claims_the_pictures_sent_with_it(db):
    await chats.ensure_chat(db, "c1", "q", None, user=DEFAULT_USER)
    first, _ = await images.add(db, "c1", DEFAULT_USER, "a.png", png(1))
    parts = await images.claim(db, "c1", DEFAULT_USER, "m1")
    assert len(parts) == 1
    assert parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    # Claimed once: the next question does not send it again as its own.
    assert await images.claim(db, "c1", DEFAULT_USER, "m2") == []
    assert (await images.get(db, "c1", first["id"], DEFAULT_USER))["message_id"] == "m1"


async def test_a_follow_up_is_shown_the_last_pictures_again(db):
    await chats.ensure_chat(db, "c2", "q", None, user=DEFAULT_USER)
    message_id = await chats.start_message(
        db, chat_id="c2", run_id="r1", query="what is this", depth="balanced", model="m"
    )
    await images.add(db, "c2", DEFAULT_USER, "a.png", png(2))
    await images.claim(db, "c2", DEFAULT_USER, message_id)
    # Not answered yet: nothing to come back to.
    assert await images.latest_sent(db, "c2", DEFAULT_USER) == []
    await chats.finish_message(db, "r1", blocks=[], answer="A cat.", status="completed")
    assert len(await images.latest_sent(db, "c2", DEFAULT_USER)) == 1


async def test_a_model_that_cannot_see_is_given_no_pictures_and_says_so(db, monkeypatch):
    await chats.ensure_chat(db, "c3", "q", None, user=DEFAULT_USER)
    await images.add(db, "c3", DEFAULT_USER, "a.png", png(3))

    async def blind(redis, provider, model):
        return False

    monkeypatch.setattr(runs.catalog, "sees", blind)
    warnings: list[str] = []
    pictures, earlier = await runs._pictures_of(
        db, None, "c3", "m1", "openrouter", "text/only", DEFAULT_USER, warnings
    )
    assert pictures == [] and not earlier
    assert warnings and "cannot see images" in warnings[0]


async def test_an_unknown_model_is_given_the_pictures(db, monkeypatch):
    await chats.ensure_chat(db, "c4", "q", None, user=DEFAULT_USER)
    await images.add(db, "c4", DEFAULT_USER, "a.png", png(4))

    async def unknown(redis, provider, model):
        return None

    monkeypatch.setattr(runs.catalog, "sees", unknown)
    warnings: list[str] = []
    pictures, _ = await runs._pictures_of(
        db, None, "c4", "m1", "groq", "some/model", DEFAULT_USER, warnings
    )
    assert len(pictures) == 1 and warnings == []


def test_pictures_go_on_the_last_user_message_only():
    part = images.as_part(png(), "image/png")
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "what is this"},
    ]
    out = prompts.with_images(messages, [part])
    assert out[1]["content"] == "earlier"
    assert out[3]["content"] == [{"type": "text", "text": "what is this"}, part]
    assert messages[3]["content"] == "what is this", "the prompt it was given is untouched"
    assert prompts.with_images(messages, []) is messages


class SeeingLLM(FakeLLM):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.seen: list[list] = []

    async def structured(self, messages, schema, **kwargs):
        self.seen.append(messages)
        return await super().structured(messages, schema, **kwargs)

    async def stream(self, messages, **kwargs):
        self.seen.append(messages)
        async for delta in super().stream(messages, **kwargs):
            yield delta


@pytest.fixture
def seeing(monkeypatch):
    llm = SeeingLLM(classification={"needs_search": False, "search_queries": []})
    monkeypatch.setattr(orchestrator, "LLM", lambda *a, **k: llm)
    return llm


async def test_the_classifier_and_the_writer_see_the_picture(redis, seeing):
    part = images.as_part(png(), "image/png")
    stream = RunStream(redis, "img-run", ttl=60)
    await orchestrator.run(stream, request(query="was ist das?", images=[part]))
    _events, _blocks, done = await collect(stream)
    assert done["status"] == "completed"
    assert "stream" in seeing.calls
    # Small talk is not decided on the words alone with a picture attached:
    # the classifier ran, and both calls carried the picture.
    assert "Classification" in seeing.calls
    for messages in seeing.seen:
        last = messages[-1] if messages[-1]["role"] == "user" else messages[-2]
        assert isinstance(last["content"], list) and part in last["content"]
