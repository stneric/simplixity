"""The redact tool: excerpts that fit a small window, findings that are really
in the text, and a hard refusal of anything that is not a local model."""

from __future__ import annotations

from itertools import pairwise
from typing import ClassVar

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.llm.client import LLMError
from app.main import app
from app.store.db import DEFAULT_USER
from app.tools import redact


@pytest.fixture
async def client(redis, db):
    app.state.redis = redis
    app.state.db = db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        signed_in = await http.post("/api/profiles", json={"name": DEFAULT_USER})
        assert signed_in.status_code == 200, signed_in.text
        yield http


class FakeLLM:
    """Replies with whatever the test hands it, and records what it was asked."""

    reply: redact._Findings = redact._Findings(items=[])
    error: Exception | None = None
    calls: ClassVar[list[tuple[str, str]]] = []

    def __init__(self, model, provider):
        self.model, self.provider = model, provider

    async def structured(self, messages, schema, **kwargs):
        FakeLLM.calls.append((self.provider, messages[-1]["content"]))
        if FakeLLM.error:
            raise FakeLLM.error
        return FakeLLM.reply


@pytest.fixture
def fake_llm(monkeypatch):
    FakeLLM.reply = redact._Findings(items=[])
    FakeLLM.error = None
    FakeLLM.calls = []
    monkeypatch.setattr(redact, "LLM", FakeLLM)
    return FakeLLM


def findings(*pairs):
    return redact._Findings(items=[redact._Item(text=t, kind=k) for t, k in pairs])


# ------------------------------------------------------------------- chunks


def test_excerpts_cover_the_text_and_respect_the_size():
    text = ("Der Mieter Max Mustermann zahlt monatlich. " * 40 + "\n\n") * 6
    spans = redact.chunks(text, 1000)
    assert spans[0][0] == 0 and spans[-1][1] == len(text)
    for (start, end), (next_start, _) in pairwise(spans):
        assert end - start <= 1000
        # Overlapping, so a name across a cut is whole in one of them, and
        # never stalled: each excerpt starts after the previous one did.
        assert start < next_start < end


def test_excerpts_end_at_a_natural_break():
    text = "Erster Absatz endet hier.\n\n" + "x" * 500 + " Zweiter Satz. " + "y" * 600
    start, end = redact.chunks(text, 700)[0]
    assert text[start:end].endswith(("\n\n", ". ", " "))


def test_short_text_is_one_excerpt():
    assert redact.chunks("Anna Schmidt", 1000) == [[0, 12]]


def test_excerpt_size_follows_the_local_window(monkeypatch):
    monkeypatch.setattr(settings, "reasoning_max_tokens", 1024)
    small = redact.chunk_chars(4096)
    assert redact.MIN_CHUNK_CHARS <= small <= redact.MAX_CHUNK_CHARS
    # A window too small for the prompt still gets a usable excerpt, and a huge
    # one is capped for recall's sake.
    assert redact.chunk_chars(1024) == redact.MIN_CHUNK_CHARS
    assert redact.chunk_chars(128_000) == redact.MAX_CHUNK_CHARS


# ----------------------------------------------------------------- findings


def test_only_spans_in_the_excerpt_survive():
    excerpt = "Vertrag mit Anna  Schmidt,\nwohnhaft in Berlin. IBAN folgt."
    kept = redact.keep(
        excerpt,
        findings(
            ("Anna Schmidt", "person"),  # whitespace differs from the excerpt
            ("Peter Invented", "person"),  # not there at all
            ("berlin", "location"),  # case differs, alias kind
            ("Anna Schmidt", "person"),  # duplicate
            ("Vertrag", "weird-kind"),
        ),
    )
    assert kept == [
        {"text": "Anna  Schmidt", "kind": "person"},
        {"text": "Berlin", "kind": "address"},
        {"text": "Vertrag", "kind": "other"},
    ]


def test_an_echoed_excerpt_is_not_a_span():
    excerpt = "Hiermit kündige ich den Vertrag fristgerecht zum Monatsende."
    assert redact.keep(excerpt, findings((excerpt, "other"))) == []


async def test_scan_refuses_a_remote_provider(fake_llm):
    with pytest.raises(redact.RedactError):
        await redact.scan("openrouter", "openai/gpt-4o-mini", "Anna Schmidt")
    assert fake_llm.calls == []


# ---------------------------------------------------------------------- api


async def test_plan_returns_excerpt_offsets(client):
    body = (await client.post("/api/tools/redact/plan", json={"text": "a b c " * 2000})).json()
    assert body["chunks"][0][0] == 0
    assert body["chunks"][-1][1] == len("a b c " * 2000)
    assert body["chunk_chars"] == redact.chunk_chars()


async def test_scan_runs_on_a_local_model(client, fake_llm):
    fake_llm.reply = findings(("Anna Schmidt", "person"), ("Nobody", "person"))
    res = await client.post(
        "/api/tools/redact/scan",
        json={
            "provider": "ollama",
            "model": "qwen3:4b",
            "text": "Frau Anna Schmidt unterschreibt.",
        },
    )
    assert res.status_code == 200, res.text
    # "Nobody" was proposed and is not in the excerpt: dropped, but counted, so
    # the progress log can say the model guessed.
    assert res.json() == {"findings": [{"text": "Anna Schmidt", "kind": "person"}], "proposed": 2}
    assert fake_llm.calls[0][0] == "ollama"
    assert "Frau Anna Schmidt" in fake_llm.calls[0][1]


async def test_scan_rejects_a_remote_model(client, fake_llm):
    res = await client.post(
        "/api/tools/redact/scan",
        json={"provider": "openrouter", "model": "openai/gpt-4o-mini", "text": "Anna Schmidt"},
    )
    assert res.status_code == 422
    assert "local" in res.json()["detail"]
    assert fake_llm.calls == []


async def test_a_failing_model_is_a_502_with_its_reason(client, fake_llm):
    fake_llm.error = LLMError("qwen3:4b did not return usable JSON")
    res = await client.post(
        "/api/tools/redact/scan",
        json={"provider": "lmstudio", "model": "qwen3:4b", "text": "Anna Schmidt"},
    )
    assert res.status_code == 502
    assert "usable JSON" in res.json()["detail"]


async def test_tools_need_a_profile(redis, db):
    app.state.redis, app.state.db = redis, db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        res = await http.post("/api/tools/redact/plan", json={"text": "x"})
    assert res.status_code == 401


async def test_pdf_upload_must_be_a_pdf(client):
    res = await client.post(
        "/api/tools/redact/pdf", content=b"hello", headers={"Content-Type": "application/pdf"}
    )
    assert res.status_code == 415


async def test_pdf_text_layer_is_extracted(client):
    res = await client.post(
        "/api/tools/redact/pdf",
        content=_pdf("Anna Schmidt"),
        headers={"Content-Type": "application/pdf"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["pages"] == 1
    assert "Anna Schmidt" in res.json()["text"]


async def test_a_pdf_without_text_says_so(client):
    from io import BytesIO

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = BytesIO()
    writer.write(buffer)
    res = await client.post(
        "/api/tools/redact/pdf",
        content=buffer.getvalue(),
        headers={"Content-Type": "application/pdf"},
    )
    assert res.status_code == 422
    assert "no text layer" in res.json()["detail"]


async def test_redact_model_must_be_local(client):
    current = (await client.get("/api/settings")).json()
    assert current["redact_model"] == "" and current["redact_provider"] == ""
    rejected = await client.put(
        "/api/settings", json={**current, "redact_provider": "openrouter", "redact_model": "x"}
    )
    assert rejected.status_code == 422
    saved = await client.put(
        "/api/settings", json={**current, "redact_provider": "ollama", "redact_model": " qwen3:4b "}
    )
    assert saved.status_code == 200
    assert saved.json()["redact_model"] == "qwen3:4b"


def _pdf(text: str) -> bytes:
    """A one-page PDF with a real text layer, written by hand: pypdf can read
    text but not lay it out, and a fixture file would be one more thing to keep."""
    stream = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)
