"""Chat storage: what a follow-up question actually sees, and schema upgrades."""

from __future__ import annotations

import aiosqlite

from app.store import chats, projects
from app.store.db import connect


async def _turn(db, chat_id: str, query: str, answer: str, status: str = "completed") -> None:
    await chats.ensure_chat(db, chat_id, query)
    run_id = f"run-{query}"
    await chats.start_message(
        db, chat_id=chat_id, run_id=run_id, query=query, depth="quick", model="m"
    )
    await chats.finish_message(db, run_id, blocks=[], answer=answer, status=status)


async def test_history_returns_alternating_turns_oldest_first(db):
    await _turn(db, "c1", "first question", "first answer")
    await _turn(db, "c1", "second question", "second answer")

    history = await chats.history(db, "c1")
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
    assert history[0]["content"] == "first question"
    assert history[-1]["content"] == "second answer"


async def test_history_excludes_failed_turns(db):
    """Feeding a failed run's empty answer back in teaches the model that empty
    answers are acceptable."""
    await _turn(db, "c2", "good", "an answer")
    await _turn(db, "c2", "bad", "", status="error")

    history = await chats.history(db, "c2")
    assert [m["content"] for m in history] == ["good", "an answer"]


async def test_history_truncates_very_long_answers(db):
    await _turn(db, "c3", "q", "x" * (chats.HISTORY_ANSWER_CHARS * 2))
    assert len((await chats.history(db, "c3"))[1]["content"]) == chats.HISTORY_ANSWER_CHARS


async def test_chats_are_listed_most_recently_updated_first(db):
    await _turn(db, "older", "one", "a")
    await _turn(db, "newer", "two", "b")
    listed = await chats.list_chats(db)
    assert listed[0]["id"] == "newer"


async def test_deleting_a_chat_removes_its_messages(db):
    await _turn(db, "doomed", "q", "a")
    assert await chats.delete_chat(db, "doomed") is True

    async with db.execute("SELECT COUNT(*) AS n FROM messages WHERE chat_id = 'doomed'") as cursor:
        assert (await cursor.fetchone())["n"] == 0


def test_fallback_title_is_short_and_never_empty():
    assert chats.fallback_title("") == "New chat"
    assert len(chats.fallback_title("word " * 100)) <= chats.TITLE_LIMIT


async def test_titles_are_stripped_of_quotes_models_like_to_add(db):
    await chats.ensure_chat(db, "c4", "q")
    await chats.set_title(db, "c4", "  “A Quoted Title”  ")
    assert (await chats.get_chat(db, "c4"))["title"] == "A Quoted Title"


async def test_migrations_add_columns_to_a_database_from_an_older_release(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` skips an existing table, so an upgraded
    install would keep the old shape and fail on the first write."""
    path = str(tmp_path / "old.db")
    async with aiosqlite.connect(path) as legacy:
        await legacy.execute(
            """CREATE TABLE chats (id TEXT PRIMARY KEY, title TEXT NOT NULL,
               created_at TEXT NOT NULL, sources TEXT)"""
        )
        await legacy.execute(
            """CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
               message_id TEXT NOT NULL, run_id TEXT, query TEXT, blocks TEXT, status TEXT,
               created_at TEXT NOT NULL)"""
        )
        await legacy.commit()

    conn = await connect(path)
    try:
        await _turn(conn, "c5", "q", "a")
        assert (await chats.history(conn, "c5"))[1]["content"] == "a"
        # Projects arrived later still: an upgraded install has to gain the
        # column, the table and the index, and a chat written before any of it
        # existed has to come back as simply unfiled.
        assert (await chats.get_chat(conn, "c5"))["project_id"] is None
        project = await projects.create(conn, "Added after the upgrade")
        await projects.assign(conn, "c5", project["id"])
        assert [c["id"] for c in await chats.list_chats(conn, project_id=project["id"])] == ["c5"]
    finally:
        await conn.close()


async def test_a_message_records_when_its_answer_was_written(db):
    await chats.ensure_chat(db, "c7", "q")
    await chats.start_message(db, chat_id="c7", run_id="r7", query="q", depth="quick", model="m")
    assert (await chats.get_chat(db, "c7"))["messages"][0]["finished_at"] is None

    await chats.finish_message(db, "r7", blocks=[], answer="a", status="completed")
    message = (await chats.get_chat(db, "c7"))["messages"][0]
    assert message["finished_at"] >= message["created_at"]


async def test_startup_marks_runs_a_previous_process_left_behind(db):
    await chats.ensure_chat(db, "c6", "q")
    await chats.start_message(db, chat_id="c6", run_id="r", query="q", depth="quick", model="m")

    assert await chats.fail_stale_messages(db) == 1

    chat = await chats.get_chat(db, "c6")
    assert chat["messages"][0]["status"] == "error"
    assert await chats.fail_stale_messages(db) == 0  # idempotent
