"""The citation check.

What it is for: an answer's most dangerous failure is not "no answer" (the run
reports that), but a confident sentence carrying [3] that source 3 does
not say. These pin the two properties that make the check worth having: the
claims are enumerated by code rather than chosen by the model, and a verdict
never invents a failure the model did not find.
"""

from __future__ import annotations

import re

from app.agent import verify
from app.embed.rerank import Passage
from app.llm import LLMError
from app.search.base import SearchResult


def claims(answer: str, known=(1, 2, 3)):
    return verify.claims_of(answer, set(known))


def passage(index: int, text: str = "What the page says about the limiter.") -> Passage:
    return Passage(text=text, source_index=index, title=f"Source {index}", url=f"https://s{index}")


def source(index: int, **fields) -> tuple[int, SearchResult]:
    base = {"title": f"Source {index}", "url": f"https://s{index}", "snippet": "A snippet."}
    return index, SearchResult(**{**base, **fields})


# ------------------------------------------------------------------ extraction


def test_every_cited_sentence_becomes_a_claim():
    found = claims(
        "Redis reclaims idle entries with XAUTOCLAIM [1]. "
        "The command replaced a two-step loop [2]. "
        "This paragraph cites nothing at all."
    )
    assert len(found) == 2
    assert found[0].sources == [1]
    assert found[1].sources == [2]
    assert "XAUTOCLAIM" in found[0].text


def test_a_claim_leaning_on_several_sources_keeps_all_of_them():
    found = claims("Both the docs and the release notes describe it that way [1][3].")
    assert found[0].sources == [1, 3]


def test_bullets_are_claims_even_without_a_full_stop():
    found = claims("- The limiter needs Valkey [1]\n- Backing off is what avoids the ban [2]")
    assert len(found) == 2


def test_a_citation_inside_a_code_block_is_not_a_claim():
    """`[1]` in a code sample is a list index, and judging it produces a
    confident verdict about nothing."""
    found = claims("Use it like this:\n\n```python\nvalues = items[1] + items[2]\n```\n")
    assert found == []


def test_a_heading_carrying_a_number_is_not_a_claim():
    """It routinely carries a citation and asserts nothing on its own."""
    assert claims("## What the sources say about this [1]") == []


def test_a_table_row_is_a_claim_and_is_handed_over_as_a_sentence():
    """The largest hole this check had. Asked for a timeline, the writer puts
    every fact in a table and cites each row, and rows were skipped, so the
    block reported on the prose underneath and called an eight-row answer "all 2
    cited statements supported"."""
    found = claims("| Oktober 2025 | Vergleich vor der Gerichtsverhandlung [2] |")
    assert len(found) == 1
    assert found[0].sources == [2]
    assert found[0].text == "Oktober 2025: Vergleich vor der Gerichtsverhandlung"


def test_the_rule_under_a_table_header_is_not_a_claim():
    assert claims(
        "| Datum | Ereignis [1] |\n|---|---|\n| 2017 | Sie kauften das Grundstück [2] |"
    ) == (claims("| 2017 | Sie kauften das Grundstück [2] |"))


def test_every_row_of_a_cited_table_is_checked():
    answer = (
        "| Datum | Ereignis |\n"
        "|-------|----------|\n"
        "| 2017 | Das Grundstück wurde gekauft [1] |\n"
        "| 2024 | Die Klage wurde eingereicht [2] |\n"
        "| 2025 | Der Vergleich kam zustande [3] |\n"
    )
    found = claims(answer)
    assert [claim.sources for claim in found] == [[1], [2], [3]]


def test_a_short_factual_sentence_is_still_a_claim():
    """The floor must not quietly stop checking the sentences a wrong citation
    hurts most."""
    assert len(claims("Der Preis liegt bei 302 USD [1].")) == 1


def test_a_soft_wrapped_sentence_is_one_claim_and_not_its_second_half():
    """The bug this pins: splitting on newlines cut every claim wherever the
    writer's margin fell, and the fragment keeping the citation was the tail of
    it, so the model was asked to rule on a sentence nobody wrote."""
    found = claims(
        "Startpage throws a CAPTCHA as soon as a self-hosted\n"
        "instance scrapes from one address [2]. Mojeek answers\n"
        '"access denied" [3].'
    )
    assert len(found) == 2
    assert found[0].text == (
        "Startpage throws a CAPTCHA as soon as a self-hosted instance scrapes from one address."
    )
    assert found[1].text.startswith("Mojeek answers")


def test_a_wrapped_list_item_stays_its_own_claim():
    found = claims(
        "1. Keep the limiter on [1]\n"
        "2. Back off on errors: the docs suggest the stats page\n"
        "   before disabling anything [2]"
    )
    assert len(found) == 2
    assert "stats page before disabling" in found[1].text


def test_an_abbreviation_is_not_a_sentence_end():
    """Observed in a stored run: "Lt. Gen. Igor Kirillov" and "am 19. Dezember"
    are five full stops between them and not one sentence end, and the fragment
    that kept the citation was the tail: a claim nobody wrote."""
    found = claims(
        "Der BBC-Bericht bezeichnet die Ermordung von Lt. Gen. Igor Kirillov "
        "als den ranghöchsten Offizier [1]. Die Crew kehrte am 19. Dezember "
        "1972 nach zwölf Tagen zur Erde zurück [2]."
    )
    assert len(found) == 2
    assert found[0].text.startswith("Der BBC-Bericht")
    assert "Lt. Gen. Igor Kirillov" in found[0].text
    assert found[1].text.startswith("Die Crew kehrte am 19. Dezember 1972")


def test_an_initial_does_not_end_a_sentence_either():
    found = claims("The edition was prepared by J. R. R. Tolkien himself [1].")
    assert len(found) == 1
    assert "J. R. R. Tolkien" in found[0].text


def test_the_claim_is_shown_as_prose_and_not_as_markdown():
    """The block renders text, never markup, so a surviving `**` arrives as
    literal asterisks in the one place meant to be quotable."""
    found = claims("- Die **Telegraph**-Artikel [1] nennen `keine` Zahlen zum *Treffer*.")
    assert found[0].text == "Die Telegraph-Artikel nennen keine Zahlen zum Treffer."


def test_the_citation_marker_leaves_the_text_but_not_the_claim():
    """It is stated separately and far more clearly than an inline marker the
    model would have to parse back out of the prose."""
    found = claims("Both the docs and the notes describe it that way [1][3].")
    assert found[0].sources == [1, 3]
    assert "[1]" not in found[0].text
    assert found[0].text.endswith("that way.")


def test_extraction_stops_at_the_limit_rather_than_growing_without_bound():
    answer = " ".join(f"Sentence number {i} about the subject [1]." for i in range(40))
    assert len(verify.claims_of(answer, {1}, limit=5)) == 5


def test_a_long_answer_is_enumerated_in_full_even_though_one_call_cannot_rule_on_it():
    """The extraction limit and the per-call limit are different numbers. While
    they were the same one, the claims past it were not merely unchecked. They
    were never counted, so the block reported a clean answer on the strength of
    its first fourteen sentences."""
    answer = " ".join(f"Sentence number {i} about the subject [1]." for i in range(40))
    found = verify.claims_of(answer, {1})
    assert len(found) == 40
    assert len(verify._judgeable(found)[: verify.JUDGED_LIMIT]) == verify.JUDGED_LIMIT
    assert verify.summary_of(found)["unchecked"] == 40


# -------------------------------------------------------------------- verdicts


def test_a_citation_the_run_never_retrieved_is_unsupported_without_asking_anyone():
    """Not a matter of opinion, so no model is consulted: source 9 does not
    exist, and a claim resting on it rests on nothing."""
    found = claims("The instance bans the address after a day [9].", known=(1, 2))
    assert found[0].unknown == [9]
    assert verify._judgeable(found) == []

    verify._finalise(found)
    assert found[0].verdict == verify.UNSUPPORTED
    assert "[9]" in found[0].note


def test_a_claim_the_model_skipped_is_unchecked_and_not_supported():
    """A truncated response is a gap in the check. It is not evidence against the
    answer. Inventing findings out of it would make the block untrustworthy in the
    one direction that matters, but it is not evidence *for* it either, which is
    what a "supported" default would silently claim."""
    found = claims("The limiter needs Valkey [1]. Backing off avoids the ban [2].")
    verify._apply(found, verify.Support(verdicts=[verify.Verdict(claim=1, verdict="unsupported")]))

    assert found[0].verdict == verify.UNSUPPORTED
    assert found[1].verdict == verify.UNCHECKED


def test_an_unknown_verdict_string_does_not_become_a_verdict():
    """And in particular does not become a pass. A model answering "dubious" has
    said something the check cannot read, which is exactly a claim nobody ruled
    on."""
    found = claims("The limiter needs Valkey [1].")
    verify._apply(found, verify.Support(verdicts=[verify.Verdict(claim=1, verdict="dubious")]))
    assert found[0].verdict == verify.UNCHECKED


def test_a_note_is_kept_on_a_finding_and_dropped_on_a_pass():
    found = claims("The limiter needs Valkey [1]. Backing off avoids the ban [2].")
    verify._apply(
        found,
        verify.Support(
            verdicts=[
                verify.Verdict(claim=1, verdict="supported", note="looks fine to me"),
                verify.Verdict(claim=2, verdict="partial", note="the source gives no date"),
            ]
        ),
    )
    assert found[0].note == ""
    assert found[1].note == "the source gives no date"


def test_the_summary_counts_what_the_footer_line_states():
    found = claims(
        "The limiter needs Valkey [1]. Backing off avoids the ban [2]. "
        "The instance was blocked within a day [3]."
    )
    verify._apply(
        found,
        verify.Support(
            verdicts=[
                verify.Verdict(claim=1, verdict="supported"),
                verify.Verdict(claim=2, verdict="partial"),
                verify.Verdict(claim=3, verdict="unsupported"),
            ]
        ),
    )
    summary = verify.summary_of(found)
    assert summary["total"] == 3
    assert summary["checked"] == 3
    assert summary["supported"] == 1
    assert summary["partial"] == 1
    assert summary["unsupported"] == 1
    assert summary["unchecked"] == 0
    assert len(summary["claims"]) == 3


def test_only_the_cited_sources_are_put_in_front_of_the_judge():
    """A model shown the whole evidence pile finds claims supported by sources
    the answer never named, which is the opposite of the question asked."""
    found = claims("The limiter needs Valkey [1].")
    evidence = verify._evidence_for(found, [passage(1), passage(2)], dict([source(1), source(2)]))
    assert [p.source_index for p in evidence] == [1]


def test_a_cited_source_with_no_ranked_passage_is_read_from_what_the_run_retrieved():
    """The deep path re-ranks before writing, so a source can hold a citation and
    own no passage in the final set. Judged against an empty evidence list, every
    sentence resting on it comes back unsupported."""
    found = claims("Backing off avoids the ban [2].")
    registry = dict([source(1), source(2, content="The page states that backing off avoids it.")])

    evidence = verify._evidence_for(found, [passage(1)], registry)

    assert [p.source_index for p in evidence] == [2]
    assert "backing off avoids it" in evidence[0].text
    # It was read in full, and the provenance line the judge sees has to say so.
    assert evidence[0].read is True


def test_a_source_in_the_sidebar_is_never_reported_as_one_the_run_never_retrieved():
    """`known` is every registered source, not only those holding a passage. Saying
    otherwise would accuse the run of citing something it had, in fact,
    retrieved, and the reader could click it."""
    found = verify.claims_of("Backing off avoids the ban [2].", {1, 2})
    verify._finalise(found)
    assert found[0].unknown == []
    assert found[0].verdict == verify.UNCHECKED


def test_the_summary_separates_what_was_checked_from_what_the_answer_cited():
    """The failure this pins: an answer with more claims than one call rules on
    was summarised as though the whole of it had come through clean."""
    found = claims(
        "The limiter needs Valkey [1]. Backing off avoids the ban [2]. "
        "The instance was blocked within a day [3]."
    )
    verify._apply(found, verify.Support(verdicts=[verify.Verdict(claim=1, verdict="supported")]))

    summary = verify.summary_of(found)
    assert summary["total"] == 3
    assert summary["checked"] == 1
    assert summary["supported"] == 1
    assert summary["unchecked"] == 2


# -------------------------------------------------------------------- batching


class BatchLLM:
    """Rules every claim it is handed `supported`, and records the batch sizes.

    Failing on a chosen call is what the "a later batch fails" case needs, and
    counting the calls is the only way to tell a check that covered the answer
    from one that covered its first fourteen sentences.
    """

    model = "fake/model"
    is_local = False

    def __init__(self, fail_on: int | None = None):
        self.batch_sizes: list[int] = []
        self.fail_on = fail_on

    async def structured(self, messages, schema, **kwargs):
        prompt = messages[-1]["content"]
        listed = prompt.partition("## Claims to check")[2]
        size = len(re.findall(r"^\d+\. ", listed, re.MULTILINE))
        self.batch_sizes.append(size)
        if self.fail_on is not None and len(self.batch_sizes) == self.fail_on:
            raise LLMError("provider fell over")
        return verify.Support(
            verdicts=[verify.Verdict(claim=n, verdict="supported") for n in range(1, size + 1)]
        )


def answer_of(count: int) -> str:
    """`count` cited sentences, each one long enough to be a claim."""
    return " ".join(f"Finding number {n} rests on the retrieved passage [1]." for n in range(count))


async def verified(redis, llm, *, count: int, depth: str = "deep", answer: str | None = None):
    from app.agent import depth as depth_module
    from app.agent.budget import Budget
    from app.agent.context import RunContext, SourceRegistry
    from app.store.stream import RunStream

    preset = depth_module.get(depth)
    stream = RunStream(redis, "v1", ttl=60)
    registry = SourceRegistry(stream, preset.max_sources)
    await registry.add([SearchResult(title="Source 1", url="https://s1.test/p")])
    ctx = RunContext(
        stream=stream,
        llm=llm,
        router=None,
        depth=preset,
        budget=Budget(preset),
        sources=registry,
        query="q",
    )
    data = await verify.check(
        ctx, "q", answer if answer is not None else answer_of(count), [passage(1)]
    )
    return ctx, data


async def test_a_long_answer_is_checked_past_the_first_call(redis):
    """The bug this closes: one call ruled on fourteen claims and the rest were
    reported unchecked, so a deep report with twenty-three cited sentences was
    summarised as "14 of 23" with the nine it ends on never looked at."""
    llm = BatchLLM()
    _ctx, data = await verified(redis, llm, count=23)

    assert data["total"] == 23
    assert data["checked"] == 23
    assert data["unchecked"] == 0
    assert llm.batch_sizes == [14, 9]


async def test_the_check_cannot_outgrow_the_answer_it_checks(redis):
    """A pathological answer must not turn the verification into the most
    expensive thing in the run; what the ceiling does not reach is reported."""
    llm = BatchLLM()
    _ctx, data = await verified(redis, llm, count=60)

    assert len(llm.batch_sizes) == verify.MAX_JUDGE_CALLS
    assert data["checked"] == verify.JUDGED_LIMIT * verify.MAX_JUDGE_CALLS
    assert data["unchecked"] == 60 - data["checked"]


async def test_a_batch_that_runs_out_of_budget_leaves_the_rest_unchecked(redis):
    """Never unchecked-as-checked: the claims the budget did not pay for are
    counted, not quietly dropped from the total."""
    llm = BatchLLM()

    from app.agent import depth as depth_module
    from app.agent.budget import Budget
    from app.agent.context import RunContext, SourceRegistry
    from app.store.stream import RunStream

    preset = depth_module.get("deep")
    stream = RunStream(redis, "v2", ttl=60)
    registry = SourceRegistry(stream, preset.max_sources)
    await registry.add([SearchResult(title="Source 1", url="https://s1.test/p")])
    budget = Budget(preset)
    # One call left before the ceiling, so the second batch is unaffordable.
    budget.llm_calls = preset.max_llm_calls - 1
    ctx = RunContext(
        stream=stream,
        llm=llm,
        router=None,
        depth=preset,
        budget=budget,
        sources=registry,
        query="q",
    )

    data = await verify.check(ctx, "q", answer_of(23), [passage(1)])

    assert llm.batch_sizes == [14]
    assert data["checked"] == 14
    assert data["unchecked"] == 9
    assert data["total"] == 23


async def test_a_later_batch_failing_keeps_the_verdicts_already_in_hand(redis):
    """The first fourteen were ruled on by a model that answered. Throwing them
    away because the second call fell over reports less than the run knows."""
    llm = BatchLLM(fail_on=2)
    _ctx, data = await verified(redis, llm, count=23)

    assert data["checked"] == 14
    assert data["unchecked"] == 9
    assert data["status"] == "done"


async def test_the_first_call_failing_still_reports_a_failed_check(redis):
    """Nothing was ruled on, so the block says so rather than showing a verdict
    list with nothing in it."""
    llm = BatchLLM(fail_on=1)
    ctx, data = await verified(redis, llm, count=23)

    assert data is None
    blocks = [b for b in await ctx.stream.snapshot() if b["type"] == "verification"]
    assert blocks[-1]["data"]["status"] == "failed"


# --------------------------------------------------------------- no citations


async def test_an_answer_that_cites_nothing_is_a_finding_not_a_silence(redis):
    """The bug this closes: `check` returned None when no sentence carried a
    number, and no block is exactly what a clean check looks like. So the one
    answer that ignored the citation contract outright was also the only one the
    reader was told nothing about."""
    answer = (
        "**[Quelle impliziert durch den Kontext]**: Die Notwendigkeit, fehlerhafte Systeme "
        "zu analysieren und zu beheben, ist ein zentrales Thema der verteilten Systeme. "
        "Weitere Aspekte betreffen Logging, Monitoring und automatisierte Testverfahren."
    )
    ctx, data = await verified(redis, BatchLLM(), count=0, answer=answer)

    assert data is not None, "an uncited answer must not pass silently"
    assert data["status"] == "uncited"
    assert data["placeholders"] == ["Quelle impliziert durch den Kontext"]
    blocks = [b for b in await ctx.stream.snapshot() if b["type"] == "verification"]
    assert blocks[-1]["data"]["status"] == "uncited"


async def test_a_one_line_answer_without_citations_is_left_alone(redis):
    """Not every uncited sentence is a failed report: a short reply carries no
    claim worth a block, and flagging it would teach the reader to ignore the
    flag."""
    _ctx, data = await verified(redis, BatchLLM(), count=0, answer="Kurz: ja.")

    assert data is None


async def test_the_check_batches_smaller_for_a_local_window(redis, monkeypatch):
    """The bug this closes: the writer's prompt was fitted to a 4096-token
    runtime window and the check's was not, so a local run's verdict block read
    "0 of 4 checked": fourteen claims and their evidence truncated away, and
    nothing left for the model to rule on."""
    from app.agent import verify as verify_module

    monkeypatch.setattr(verify_module.settings, "local_context_tokens", 4096)
    llm = BatchLLM()
    llm.is_local = True

    _ctx, data = await verified(redis, llm, count=12)

    assert llm.batch_sizes, "the check must still run"
    assert max(llm.batch_sizes) < verify.JUDGED_LIMIT, "a 4k window cannot take fourteen claims"
    assert data["checked"] > 0, "claims that fit are actually ruled on"


async def test_a_remote_model_keeps_the_full_batch(redis):
    llm = BatchLLM()
    _ctx, _data = await verified(redis, llm, count=23)

    assert llm.batch_sizes == [14, 9]


# ------------------------------------------------------------------- figures


def _page(content: str) -> SearchResult:
    return SearchResult(title="Page", url="https://p.test", content=content)


def _figures(claim_text: str, content: str, verdict: str = verify.SUPPORTED) -> verify.Claim:
    claim = verify.Claim(text=claim_text, sources=[1], verdict=verdict)
    verify.check_numbers([claim], {1: _page(content)})
    return claim


def test_a_figure_the_source_does_not_contain_takes_the_claim_off_supported():
    claim = _figures(
        "Der Fehler sank um 23 % gegenüber dem Basismodell.", "The error fell by 32 %."
    )
    assert claim.figures == ["23"]
    assert claim.verdict == verify.PARTIAL
    assert "23" in claim.note


def test_both_decimal_conventions_and_rounding_count_as_the_same_figure():
    assert not _figures("Der Anteil stieg 2023 auf 1,5 %.", "In 2023 it rose to 1.5%.").figures
    assert not _figures("Das kostet 1.500 Euro.", "It costs 1,500 EUR.").figures
    assert not _figures("Rund 33 % der Befragten stimmten zu.", "33.3% agreed.").figures
    assert not _figures(
        "Es betrifft 2,5 Mio. Haushalte.", "It affects 2,500,000 households."
    ).figures
    assert not _figures(
        "Veröffentlicht am 17.12.2024 in Berlin.", "Published December 17, 2024."
    ).figures
    assert _figures("Rund 32 % der Befragten stimmten zu.", "33.3% agreed.").figures == ["32"]


def test_names_and_small_counts_are_not_looked_up():
    """GPT-4 and COVID-19 are names, and "3 Studien" is as often "three
    studies" on the page: looking them up would accuse correct sentences."""
    assert not _figures(
        "GPT-4 wurde in 3 Studien zu COVID-19 getestet.", "Nothing numeric."
    ).figures


def test_figures_are_not_judged_against_a_snippet():
    claim = verify.Claim(text="Der Umsatz stieg um 48 %.", sources=[1, 2], verdict=verify.SUPPORTED)
    read = _page("Revenue rose sharply.")
    snippet_only = SearchResult(title="S", url="https://s.test", snippet="Revenue")
    verify.check_numbers([claim], {1: read, 2: snippet_only})
    assert not claim.figures and claim.verdict == verify.SUPPORTED


def test_an_unchecked_claim_keeps_its_verdict_but_shows_the_missing_figure():
    claim = _figures(
        "Die Leistung lag bei 870 MW.", "Capacity was 780 MW.", verdict=verify.UNCHECKED
    )
    assert claim.figures == ["870"] and claim.verdict == verify.UNCHECKED
    assert verify.summary_of([claim])["figures"] == 1


def test_a_literature_check_may_make_more_calls_and_run_past_the_preset():
    from app.agent.budget import Budget
    from app.agent.depth import get

    groups = verify._batches([verify.Claim(text="x" * 20, sources=[1])] * 100, per_call=14)
    assert len(groups) == verify.MAX_JUDGE_CALLS
    groups = verify._batches(
        [verify.Claim(text="x" * 20, sources=[1])] * 100,
        per_call=14,
        calls=verify.LIBRARY_JUDGE_CALLS,
    )
    assert len(groups) == verify.LIBRARY_JUDGE_CALLS

    budget = Budget(get("quick"))
    budget.llm_calls = budget.depth.max_llm_calls
    assert not budget.can_verify()
    budget.allow_check(calls=4, seconds=60)
    assert budget.can_verify()
