"""Files attached to a chat: read, stored, searched, cited and opened like a
paper's full text, in any chat, with or without a project.

The Word documents are built here, by hand, as the PDFs are in test_library:
a .docx is a zip with one XML file that matters, and writing that file is the
smallest thing that still exercises the real reader.
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
import test_library
from test_agent import FakeLLM, collect, request
from test_library import FILLER, make_pdf

from app.agent import orchestrator
from app.scholar.fulltext import FulltextError
from app.search import library
from app.store import attachments as store
from app.store import chats, literature, settings_store
from app.store import library as library_store
from app.store.db import DEFAULT_USER
from app.store.stream import RunStream
from app.tools import attachments, documents

# Fixtures, found by name.
client = test_library.client
no_web = test_library.no_web
offline_embed = test_library.offline_embed

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def make_docx(body: str) -> bytes:
    """A .docx whose body is this XML (paragraphs, tables, content controls)."""
    xml = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}">'
        f"<w:body>{body}</w:body></w:document>"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", xml)
    return out.getvalue()


def p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


# ------------------------------------------------------------------- reading


def test_a_text_file_is_cut_into_parts_at_paragraphs():
    paragraphs = [f"Paragraph {i}. " + "word " * 120 for i in range(12)]
    doc = documents.read("notes.txt", "\n\n".join(paragraphs).encode())
    assert doc.kind == documents.TEXT
    assert len(doc.extracted.pages) > 1
    assert all(len(part) <= documents.PART_CHARS + 10 for part in doc.extracted.pages)
    # No paragraph is split between two parts: each starts a part or follows
    # another paragraph inside one.
    for part in doc.extracted.pages:
        assert part.startswith("Paragraph")


def test_one_huge_paragraph_is_still_cut():
    doc = documents.read("x.md", ("A sentence here. " * 1000).encode())
    assert doc.kind == documents.MARKDOWN
    assert all(len(part) <= documents.PART_CHARS for part in doc.extracted.pages)
    assert " ".join(doc.extracted.pages).count("A sentence here.") == 1000


def test_a_text_file_from_windows_is_read_not_refused():
    doc = documents.read("brief.txt", "Grüße aus Köln, für alle.".encode("cp1252"))
    assert doc.extracted.pages == ["Grüße aus Köln, für alle."]


def test_a_word_document_is_read_with_its_tables_and_content_controls():
    body = (
        p("Mietvertrag")
        + "<w:sdt><w:sdtContent>"
        + p("Deckblatt im Inhaltssteuerelement")
        + "</w:sdtContent></w:sdt>"
        + "<w:tbl><w:tr><w:tc>"
        + p("Kaltmiete")
        + "</w:tc><w:tc>"
        + p("850 EUR")
        + "</w:tc></w:tr></w:tbl>"
        + p("Die Kündigungsfrist beträgt drei Monate.")
    )
    doc = documents.read("vertrag.docx", make_docx(body))
    assert doc.kind == documents.DOCX
    [text] = doc.extracted.pages
    assert "Deckblatt im Inhaltssteuerelement" in text
    assert "Kaltmiete | 850 EUR" in text
    assert text.index("Mietvertrag") < text.index("Kündigungsfrist")


def test_a_word_document_declaring_entities_is_refused():
    xml = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><w:document/>'
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("word/document.xml", xml)
    with pytest.raises(FulltextError):
        documents.read("bomb.docx", out.getvalue())


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("photo.png", b"\x89PNG\r\n\x1a\n...."),
        ("sheet.xlsx", make_docx(p("x"))),
        ("binary.txt", b"abc\x00def"),
        ("fake.pdf", b"<html>login</html>"),
    ],
)
def test_what_cannot_be_read_is_refused_with_a_reason(name, data):
    with pytest.raises(FulltextError) as caught:
        documents.read(name, data)
    assert str(caught.value)


def test_a_pdf_is_a_pdf_whatever_it_is_called():
    doc = documents.read("scan.txt", make_pdf(["Hello on page one. " * 20]))
    assert doc.kind == documents.PDF


def test_an_empty_text_file_is_refused():
    with pytest.raises(FulltextError, match="no text"):
        documents.read("empty.txt", b"  \n\n ")


# ------------------------------------------------------------------- storing

AGGREGATION = "Secure aggregation hides each client update from the server. " * 8


async def test_an_attachment_is_stored_by_chat_and_indexed(db, offline_embed):
    result = await attachments.upload(
        db, "chat-a", "Paper.pdf", make_pdf([FILLER * 3, AGGREGATION]), user=DEFAULT_USER
    )
    file = result["file"]
    assert result["new"] and file["kind"] == "pdf" and file["pages"] == 2
    assert result["index"]["embedded"] == file["chunks"] > 0

    again = await attachments.upload(
        db, "chat-a", "Paper copy.pdf", make_pdf([FILLER * 3, AGGREGATION]), user=DEFAULT_USER
    )
    assert again["new"] is False and again["file"]["id"] == file["id"]
    assert [f["id"] for f in await attachments.listed(db, "chat-a", user=DEFAULT_USER)] == [
        file["id"]
    ]


async def test_a_chat_holds_a_bounded_number_of_files(db, monkeypatch):
    monkeypatch.setattr(store, "MAX_FILES", 2)
    for i in range(2):
        await attachments.upload(db, "c", f"{i}.txt", f"Text {i}".encode(), user=DEFAULT_USER)
    with pytest.raises(FulltextError, match="at most 2"):
        await attachments.upload(db, "c", "3.txt", b"Text 3", user=DEFAULT_USER)
    # The same file again is not a new one.
    await attachments.upload(db, "c", "0.txt", b"Text 0", user=DEFAULT_USER)


async def test_attachments_are_not_reading_lists(db, offline_embed):
    await attachments.upload(db, "chat-a", "a.txt", b"Some text.", user=DEFAULT_USER)
    assert await literature.list_collections(db, user=DEFAULT_USER) == []
    collection = await store.collection_of(db, "chat-a", DEFAULT_USER)
    assert await literature.get(db, collection["id"], user=DEFAULT_USER) is None


async def test_deleting_the_chat_deletes_its_files_and_their_bytes(db, offline_embed):
    await chats.ensure_chat(db, "chat-a", "q", None, user=DEFAULT_USER)
    result = await attachments.upload(db, "chat-a", "a.txt", b"Some text.", user=DEFAULT_USER)
    assert await chats.delete_chat(db, "chat-a", user=DEFAULT_USER)
    assert await library_store.original_of(db, result["file"]["id"]) is None
    async with db.execute("SELECT COUNT(*) FROM lit_blobs") as cursor:
        assert (await cursor.fetchone())[0] == 0


async def test_files_attached_to_a_question_never_sent_are_cleared_at_boot(db):
    await attachments.upload(db, "never-sent", "a.txt", b"Some text.", user=DEFAULT_USER)
    await attachments.upload(db, "sent", "b.txt", b"Other text.", user=DEFAULT_USER)
    await chats.ensure_chat(db, "sent", "q", None, user=DEFAULT_USER)
    old = (datetime.now(UTC) - store.ORPHAN_AFTER - timedelta(minutes=1)).isoformat()
    await db.execute("UPDATE lit_collections SET created_at = ?", (old,))
    assert await store.reap_orphans(db) == 1
    assert await store.has_files(db, "sent", DEFAULT_USER)
    assert not await store.has_files(db, "never-sent", DEFAULT_USER)


# ----------------------------------------------------------------- searching


async def test_an_attached_pdf_is_found_and_cited_by_page(db, offline_embed):
    await attachments.upload(
        db, "chat-a", "Paper.pdf", make_pdf([FILLER * 3, AGGREGATION]), user=DEFAULT_USER
    )
    lib = library.Library(db, None, chat_id="chat-a", user=DEFAULT_USER)
    [top, *_] = await lib.search(["secure aggregation server"], limit=5)
    assert top.title == "Paper.pdf (PDF p. 2)"
    assert top.url.startswith("/library/") and top.url.endswith("/2")
    assert top.tier == "primary" and "attached" in top.reasons[0]
    assert lib.label == "Your files"


async def test_a_text_file_is_cited_by_part_not_by_page(db, offline_embed):
    text = "\n\n".join([FILLER * 20, AGGREGATION * 3])
    await attachments.upload(db, "chat-a", "notes.md", text.encode(), user=DEFAULT_USER)
    lib = library.Library(db, None, chat_id="chat-a", user=DEFAULT_USER)
    found = await lib.search(["secure aggregation server"], limit=5)
    assert found and all("(part " in r.title for r in found)
    assert "PDF" not in found[0].title


async def test_another_profiles_files_are_not_searched(db, offline_embed):
    await attachments.upload(db, "chat-a", "a.pdf", make_pdf([AGGREGATION]), user="someone-else")
    lib = library.Library(db, None, chat_id="chat-a", user=DEFAULT_USER)
    assert await lib.search(["secure aggregation"], limit=5) == []


async def test_the_catalog_names_the_files_and_short_ones_are_read_whole(db, offline_embed):
    await attachments.upload(
        db, "chat-a", "Vertrag.pdf", make_pdf([FILLER, AGGREGATION]), user=DEFAULT_USER
    )
    lib = library.Library(db, None, chat_id="chat-a", user=DEFAULT_USER)
    assert "Vertrag.pdf (PDF, 2 pages)" in await lib.catalog()
    assert await lib.fits_whole()
    whole = await lib.overview(papers=False)
    assert [r.title for r in whole] == ["Vertrag.pdf (PDF p. 1)", "Vertrag.pdf (PDF p. 2)"]
    # In a literature project the files sit beside a library, and the search
    # decides what is read.
    assert not await library.Library(
        db, "project", chat_id="chat-a", user=DEFAULT_USER
    ).fits_whole()


async def test_a_long_file_is_summarised_from_its_start_and_spread_to_its_end(db, offline_embed):
    pages = [f"Page {i} says something distinct about topic {i}. " * 8 for i in range(1, 41)]
    await attachments.upload(db, "chat-a", "Book.pdf", make_pdf(pages), user=DEFAULT_USER)
    lib = library.Library(db, None, chat_id="chat-a", user=DEFAULT_USER)
    assert not await lib.fits_whole()
    numbers = [int(r.url.rsplit("/", 1)[1]) for r in await lib.overview(papers=False)]
    assert len(numbers) == library.OVERVIEW_PAGES
    assert numbers[: library.OVERVIEW_PAGES // 2] == list(range(1, library.OVERVIEW_PAGES // 2 + 1))
    assert numbers[-1] == 40 and numbers == sorted(set(numbers))


# ------------------------------------------------------------------- the run


async def test_a_chat_with_files_is_source_bound_and_keeps_its_chat_model(redis, db, monkeypatch):
    from app import runs

    seen = {}

    async def fake_run(stream, req):
        seen["request"] = req
        await stream.emit_done("completed")

    monkeypatch.setattr(runs, "orchestrate", fake_run)
    await settings_store.save(
        db,
        settings_store.UISettings(chat_model="chat/model", literature_model="lit/model"),
        DEFAULT_USER,
    )
    await attachments.upload(db, "chat-a", "a.txt", b"Some text.", user=DEFAULT_USER)

    await runs.start(redis=redis, db=db, query="q", chat_id="chat-a")
    await asyncio.sleep(0.02)
    bound = seen["request"]
    assert bound.library is not None and bound.library.chat_id == "chat-a"
    assert bound.library.project_id is None and bound.web is False
    assert bound.model == "chat/model"

    await runs.start(redis=redis, db=db, query="q", chat_id="chat-a", web=True)
    await asyncio.sleep(0.02)
    assert seen["request"].web is True

    await runs.start(redis=redis, db=db, query="q", chat_id="chat-b")
    await asyncio.sleep(0.02)
    assert seen["request"].library is None and seen["request"].web is True


async def test_summarise_this_is_written_from_the_whole_short_file(
    redis, db, offline_embed, no_web, monkeypatch
):
    """'Summarise this' matches no passage, and the classifier may not say it
    is about the file as a whole; a short file is read whole regardless."""
    await attachments.upload(
        db, "chat-a", "Brief.pdf", make_pdf([FILLER * 3, AGGREGATION]), user=DEFAULT_USER
    )
    llm = FakeLLM(
        answer="Der Brief behandelt föderiertes Lernen [1].",
        classification={
            "standalone_question": "Fasse das zusammen",
            "search_queries": ["volcano eruption"],
        },
    )
    monkeypatch.setattr(orchestrator, "LLM", lambda *a, **k: llm)
    stream = RunStream(redis, "att-whole", ttl=60)
    lib = library.Library(db, None, chat_id="chat-a", user=DEFAULT_USER)
    await orchestrator.run(stream, request(library=lib, lenses=[], web=False))

    _events, _blocks, done = await collect(stream)
    assert done["status"] == "completed"
    assert no_web == []
    assert "stream" in llm.calls, "nothing was written"
    sources = next(b for b in await stream.snapshot() if b["type"] == "sources")
    titles = [s["title"] for s in sources["data"]["sources"]]
    assert "Brief.pdf (PDF p. 1)" in titles and "Brief.pdf (PDF p. 2)" in titles
    provenance = next(b for b in await stream.snapshot() if b["type"] == "provenance")
    assert [e["name"] for e in provenance["data"]["search"]["engines"]] == ["Your files"]


async def test_nothing_in_a_long_file_means_no_answer_and_says_why(
    redis, db, offline_embed, no_web, monkeypatch
):
    pages = [f"Page {i} on grid forecasting. " * 10 for i in range(1, 15)]
    await attachments.upload(db, "chat-a", "Long.pdf", make_pdf(pages), user=DEFAULT_USER)
    llm = FakeLLM(
        answer="Unused.",
        classification={"standalone_question": "Eiffel", "search_queries": ["eiffel tower"]},
    )
    monkeypatch.setattr(orchestrator, "LLM", lambda *a, **k: llm)
    stream = RunStream(redis, "att-none", ttl=60)
    lib = library.Library(db, None, chat_id="chat-a", user=DEFAULT_USER)
    await orchestrator.run(stream, request(library=lib, lenses=[], web=False))

    notices = [b["data"]["text"] for b in await stream.snapshot() if b["type"] == "notice"]
    assert any("files attached to this chat" in n for n in notices)
    assert "stream" not in llm.calls


# ---------------------------------------------------------------------- API


async def test_attach_before_the_first_question_then_open_and_detach(client, db):
    word = make_docx(p("Die Kündigungsfrist beträgt drei Monate."))
    uploaded = await client.post(
        "/api/chats/newchat1/files",
        content=word,
        headers={"X-Filename": "Mietvertrag%20neu.docx"},
    )
    assert uploaded.status_code == 200, uploaded.text
    file = uploaded.json()["file"]
    assert file["name"] == "Mietvertrag neu.docx" and file["kind"] == "docx"

    listed = (await client.get("/api/chats/newchat1/files")).json()["files"]
    assert [f["id"] for f in listed] == [file["id"]]

    # The question names the chat id the files were attached under.
    await chats.ensure_chat(db, "newchat1", "q", None, user=DEFAULT_USER)
    opened = (await client.get("/api/chats/newchat1")).json()
    assert [f["id"] for f in opened["files"]] == [file["id"]]

    # The reader shows the text the citations were checked against, and the
    # original comes back as what it is.
    pages = (await client.get(f"/api/literature/files/{file['id']}/pages")).json()["pages"]
    assert pages == ["Die Kündigungsfrist beträgt drei Monate."]
    original = await client.get(f"/api/literature/files/{file['id']}/pdf")
    assert original.content == word
    assert original.headers["content-type"].startswith("application/vnd.openxmlformats")

    assert (await client.delete(f"/api/chats/newchat1/files/{file['id']}")).status_code == 200
    assert (await client.get("/api/chats/newchat1/files")).json()["files"] == []


async def test_another_profiles_chat_cannot_be_attached_to_or_read(client, db):
    await chats.ensure_chat(db, "theirs", "q", None, user="someone-else")
    refused = await client.post(
        "/api/chats/theirs/files", content=b"Text", headers={"X-Filename": "a.txt"}
    )
    assert refused.status_code == 404
    # Nor a chat id they attached files under before asking anything.
    theirs = await attachments.upload(db, "pending", "a.txt", b"Secret.", user="someone-else")
    assert (await client.get("/api/chats/pending/files")).status_code == 404
    assert (
        await client.get(f"/api/literature/files/{theirs['file']['id']}/pages")
    ).status_code == 404


async def test_an_unreadable_file_is_refused_over_http_with_its_reason(client):
    response = await client.post(
        "/api/chats/c1/files", content=b"\x89PNG....", headers={"X-Filename": "a.png"}
    )
    assert response.status_code == 422
    assert "PDF, Word" in response.json()["detail"]
