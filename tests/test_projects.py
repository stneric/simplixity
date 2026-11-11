"""Projects: the folder, and the corpus made out of it.

The corpus half is what makes projects more than filing, so most of this is
about what a past answer is allowed to become: a numbered source, ranked
against the web, that a run can never confuse with the thing it was written
about.
"""

from __future__ import annotations

from app.search import quality
from app.search.project import search as project_search
from app.store import chats, projects


async def _turn(db, chat_id: str, query: str, answer: str, status: str = "completed") -> None:
    run_id = f"run-{chat_id}-{query[:12]}"
    await chats.start_message(
        db, chat_id=chat_id, run_id=run_id, query=query, depth="quick", model="m"
    )
    await chats.finish_message(db, run_id, blocks=[], answer=answer, status=status)


# ------------------------------------------------------------------ the folder


async def test_a_project_counts_the_chats_filed_under_it(db):
    project = await projects.create(db, "Log anomalies")
    await chats.ensure_chat(db, "c1", "first", project["id"])
    await chats.ensure_chat(db, "c2", "second", project["id"])
    await chats.ensure_chat(db, "c3", "unfiled")

    listed = await projects.list_projects(db)
    assert [(p["name"], p["chats"]) for p in listed] == [("Log anomalies", 2)]
    assert [c["id"] for c in await chats.list_chats(db, project_id=project["id"])] == ["c2", "c1"]


async def test_a_project_is_returned_under_the_name_it_was_stored_with(db):
    """A name of only spaces is stored as untitled, and used to come back empty
    from the call that made it, so the sidebar showed a blank row until reload."""
    project = await projects.create(db, "   ")
    assert project["name"] == projects.UNTITLED
    assert [p["name"] for p in await projects.list_projects(db)] == [projects.UNTITLED]


async def test_deleting_a_project_keeps_its_chats(db):
    """A project is a view onto chats, not their owner. Deleting one because the
    grouping stopped being useful must not be a way to lose a month of work."""
    project = await projects.create(db, "Temporary")
    await chats.ensure_chat(db, "c1", "a question", project["id"])

    assert await projects.delete(db, project["id"])

    chat = await chats.get_chat(db, "c1")
    assert chat is not None
    assert chat["project_id"] is None
    assert [c["id"] for c in await chats.list_chats(db)] == ["c1"]


async def test_an_existing_chat_can_be_filed_and_unfiled(db):
    project = await projects.create(db, "Kafka")
    await chats.ensure_chat(db, "c1", "a question")

    await projects.assign(db, "c1", project["id"])
    assert (await chats.get_chat(db, "c1"))["project_id"] == project["id"]

    await projects.assign(db, "c1", None)
    assert (await chats.get_chat(db, "c1"))["project_id"] is None


async def test_a_run_in_a_project_does_not_move_a_chat_that_lives_elsewhere(db):
    """`ensure_chat` files new chats only. Asking a follow-up from inside a
    project view must not drag an existing chat out of where it was."""
    home = await projects.create(db, "Home")
    other = await projects.create(db, "Other")
    await chats.ensure_chat(db, "c1", "first", home["id"])

    await chats.ensure_chat(db, "c1", "second", other["id"])

    assert (await chats.get_chat(db, "c1"))["project_id"] == home["id"]


# ------------------------------------------------------------------ the corpus


async def test_the_corpus_is_the_finished_answers_of_the_other_chats(db):
    project = await projects.create(db, "Logs")
    await chats.ensure_chat(db, "c1", "how are kafka logs parsed", project["id"])
    await _turn(
        db, "c1", "how are kafka logs parsed", "Fluent Bit reads them and Kafka ships them."
    )
    await chats.ensure_chat(db, "c2", "asking now", project["id"])

    documents = await projects.corpus(db, project["id"], exclude_chat_id="c2")
    assert [d["chat_id"] for d in documents] == ["c1"]

    # And the chat doing the asking is never its own source.
    assert await projects.corpus(db, project["id"], exclude_chat_id="c1") == []


async def test_an_unfinished_answer_is_not_evidence(db):
    project = await projects.create(db, "Logs")
    await chats.ensure_chat(db, "c1", "a question", project["id"])
    await _turn(db, "c1", "a question", "", status="error")

    assert await projects.corpus(db, project["id"]) == []


async def test_a_past_answer_comes_back_as_a_source_the_reader_can_open(db):
    project = await projects.create(db, "Logs")
    await chats.ensure_chat(db, "c1", "kafka log parsing", project["id"])
    await _turn(db, "c1", "kafka log parsing", "Fluent Bit parses the Kafka log lines.")

    found = await project_search(db, project["id"], "how is kafka log parsing done")
    assert len(found) == 1
    result = found[0]
    assert result.tier == quality.PROJECT
    assert result.url == "/#chat=c1"
    # The answer arrives as content, not as a snippet to be fetched later.
    assert "Fluent Bit" in result.content
    assert result.tier_label == "This project"


async def test_a_past_answer_is_offered_without_its_own_citation_numbers(db):
    """Its `[2]` was that run's second source, and here it would read as this
    run's. Every form a citation takes goes, the direct quotation included."""
    project = await projects.create(db, "Logs")
    await chats.ensure_chat(db, "c1", "kafka log parsing", project["id"])
    await _turn(
        db,
        "c1",
        "kafka log parsing",
        'Fluent Bit parses the Kafka log lines [2]. They [1 quote: "arrive as JSON"] '
        'and are indexed [3: "indexed by day"].',
    )

    [result] = await project_search(db, project["id"], "how is kafka log parsing done")
    assert result.content == ("Fluent Bit parses the Kafka log lines. They and are indexed.")


async def test_a_past_answer_about_something_else_is_left_out(db):
    """The same relevance floor the web results face. A project is not a reason
    to put last week's unrelated answer in front of the model."""
    project = await projects.create(db, "Mixed")
    await chats.ensure_chat(db, "c1", "kafka", project["id"])
    await _turn(db, "c1", "kafka log parsing", "Fluent Bit parses the Kafka log lines.")
    await chats.ensure_chat(db, "c2", "cake", project["id"])
    await _turn(db, "c2", "sourdough", "Long fermentation improves the crumb.")

    found = await project_search(db, project["id"], "kafka log parsing")
    assert [r.url for r in found] == ["/#chat=c1"]


async def test_a_past_answer_never_outranks_a_paper(db):
    """Trust 0.55: above an unknown page, far below anything primary. A run's
    own earlier conclusion must not become the next run's premise."""
    assert quality.TIER_TRUST[quality.PROJECT] > quality.TIER_TRUST[quality.WEB]
    assert quality.TIER_TRUST[quality.PROJECT] < quality.TIER_TRUST[quality.NEWS]
    assert quality.TIER_TRUST[quality.PROJECT] < quality.TIER_TRUST[quality.ACADEMIC]


async def test_a_chat_outside_the_project_is_not_in_its_corpus(db):
    project = await projects.create(db, "Logs")
    await chats.ensure_chat(db, "c1", "kafka", project["id"])
    await _turn(db, "c1", "kafka log parsing", "Fluent Bit parses the Kafka log lines.")
    await chats.ensure_chat(db, "c2", "kafka elsewhere")
    await _turn(db, "c2", "kafka log parsing", "Something else entirely about Kafka logs.")

    found = await project_search(db, project["id"], "kafka log parsing")
    assert [r.url for r in found] == ["/#chat=c1"]
