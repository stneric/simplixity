"""The LLM client's job is to make provider misbehaviour survivable.

These cover the three things that actually break in production: models that
wrap JSON in prose, providers that return an error body instead of a completion,
and strict-mode schemas that have to be shaped a particular way.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from pydantic import BaseModel

from app.llm import client as llm


class Schema(BaseModel):
    needs_search: bool
    queries: list[str]


# ------------------------------------------------------------------ JSON


@pytest.mark.parametrize(
    "raw",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        '```\n{"a": 1}\n```',
        'Sure! Here you go:\n{"a": 1}\nLet me know if you need more.',
        '{"a": "a string with } a brace"}',
        '{"a": "escaped \\" quote and } brace"}',
    ],
)
def test_extract_json_survives_the_ways_models_wrap_it(raw):
    assert json.loads(llm._extract_json(raw))["a"] in (
        1,
        "a string with } a brace",
        'escaped " quote and } brace',
    )


def test_extract_json_takes_the_first_complete_object_not_the_first_brace():
    raw = 'prefix {"outer": {"inner": 2}} trailing {"other": 3}'
    assert json.loads(llm._extract_json(raw)) == {"outer": {"inner": 2}}


@pytest.mark.parametrize("raw", ["no json here", '{"unterminated": '])
def test_extract_json_reports_failure_rather_than_returning_junk(raw):
    with pytest.raises(ValueError):
        llm._extract_json(raw)


# ---------------------------------------------------------------- schema


def test_harden_closes_every_object_for_strict_mode():
    schema = llm._harden(Schema.model_json_schema())
    assert schema["additionalProperties"] is False
    # Strict mode requires *all* properties listed, including optional ones.
    assert set(schema["required"]) == {"needs_search", "queries"}
    assert "title" not in schema


def test_harden_recurses_into_arrays_of_objects():
    schema = llm._harden(
        {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "object", "properties": {"x": {}}}}
            },
        }
    )
    assert schema["properties"]["items"]["items"]["additionalProperties"] is False


def test_harden_closes_the_nested_models_under_defs():
    """Every row, step and side is a nested model, and pydantic files those under
    `$defs`. Hardening only the top level left them open and optional, which a
    strict provider rejects outright."""

    class Row(BaseModel):
        """Design notes a model filling the schema should never read."""

        label: str = ""

    class Card(BaseModel):
        rows: list[Row] = []

    schema = llm._harden(Card.model_json_schema())
    row = schema["$defs"]["Row"]
    assert row["additionalProperties"] is False
    assert row["required"] == ["label"]
    # The docstring is commentary for this codebase, not an instruction.
    assert "description" not in row
    assert "default" not in row["properties"]["label"]


async def test_a_reply_cut_off_in_schema_mode_is_not_held_against_the_model(monkeypatch):
    """Truncation is the token ceiling talking. Marking the model as lacking
    json_schema mode after one long reply moved every later call in the process
    onto the slower prompt path."""
    llm._JSON_SCHEMA_MODE.clear()
    seen: list[bool] = []

    async def complete(self, messages, *, temperature=0.2, max_tokens=None, response_format=None):
        seen.append(response_format is not None)
        if response_format is not None and len(seen) == 1:
            return '{"needs_search": true, "queries": ["a"'
        return '{"needs_search": true, "queries": []}'

    monkeypatch.setattr(llm.LLM, "complete", complete)
    client = llm.LLM("good/model", "openrouter")

    await client.structured([], Schema)
    await client.structured([], Schema)
    # Cut off, recovered in prompt mode, and the next call tries the schema again.
    assert seen == [True, False, True]
    llm._JSON_SCHEMA_MODE.clear()


# ----------------------------------------------------------------- errors


def _response(status: int, body: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        json=body if body is not None else {"error": {"message": "boom"}},
        request=httpx.Request("POST", "https://example.test/v1/chat/completions"),
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, llm.LLMAuthError),
        (403, llm.LLMAuthError),
        (402, llm.LLMAuthError),  # out of credit is not something to retry
        (429, llm.LLMRateLimitError),
        (500, llm.LLMTimeoutError),  # transient: the retry path handles these
        (503, llm.LLMTimeoutError),
        (404, llm.LLMError),
        (418, llm.LLMError),
    ],
)
def test_provider_errors_map_to_actionable_exception_types(status, expected):
    error = llm._from_response(_response(status), "some/model")
    assert isinstance(error, expected)
    assert "some/model" in str(error)


def test_error_message_survives_a_non_json_body():
    response = httpx.Response(
        500, text="<html>gateway exploded</html>", request=httpx.Request("POST", "https://x.test")
    )
    assert "gateway exploded" in str(llm._from_response(response, "m"))


def test_backoff_grows_and_stays_bounded():
    assert all(0 < llm._backoff(attempt) <= 8.0 for attempt in range(6))
    assert llm._backoff(0) < llm._backoff(5) * 2  # jitter, but the trend holds


# ----------------------------------------------------------------- stream


class _FakeStream:
    """Just enough httpx.Response to drive the SSE parser."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


async def test_sse_parser_splits_answer_from_reasoning_and_ignores_noise():
    lines = [
        ": OPENROUTER PROCESSING",
        "",
        'data: {"choices":[{"delta":{"reasoning":"thinking…"}}]}',
        'data: {"choices":[{"delta":{"content":"Hello"}}]}',
        "data: not json at all",
        'data: {"choices":[{"delta":{"content":" world"}}]}',
        "data: [DONE]",
        'data: {"choices":[{"delta":{"content":"never reached"}}]}',
    ]
    deltas = [d async for d in llm._parse_sse(_FakeStream(lines))]

    assert "".join(d.content for d in deltas) == "Hello world"
    assert "".join(d.reasoning for d in deltas) == "thinking…"


@pytest.mark.parametrize(
    "chunk, expected",
    [
        ('{"error":{"code":429,"message":"upstream is busy"}}', llm.LLMRateLimitError),
        ('{"error":{"code":502,"message":"provider hung up"}}', llm.LLMTimeoutError),
        ('{"error":{"code":401,"message":"bad key"}}', llm.LLMAuthError),
        ('{"error":{"message":"no code at all"}}', llm.LLMError),
        ('{"choices":[{"error":{"code":429,"message":"per-choice"}}]}', llm.LLMRateLimitError),
    ],
)
async def test_an_error_carried_by_a_200_response_is_raised_not_ignored(chunk, expected):
    """An upstream provider that dies mid-stream cannot change the status code,
    so OpenRouter reports it in the body. Reading only `choices` turned that
    into a stream that ended politely with nothing in it, and the run blamed
    the model for an empty answer."""
    lines = ['data: {"choices":[{"delta":{"content":"partial"}}]}', f"data: {chunk}"]

    with pytest.raises(expected) as caught:
        [d async for d in llm._parse_sse(_FakeStream(lines))]
    assert "message" not in str(caught.value)  # the text, not the JSON


async def test_the_final_chunk_of_a_groq_stream_is_read_too():
    """Groq puts its token counts under `x_groq`, not at the top level. Read
    only the OpenAI shape and a Groq answer is billed at zero tokens."""
    usage = llm.Usage()
    lines = [
        'data: {"choices":[{"delta":{"content":"Hi"}}]}',
        'data: {"choices":[],"x_groq":{"usage":{"prompt_tokens":30,"completion_tokens":7}}}',
    ]

    [d async for d in llm._parse_sse(_FakeStream(lines), usage)]

    assert usage.total_tokens == 37
    # Groq quotes no price, so none is claimed.
    assert usage.as_dict()["cost_usd"] is None


def test_the_two_ways_a_stream_can_be_empty_are_told_apart():
    thinking = llm._empty_stream_error(
        "some/model", llm.StreamTrace(reasoning_chars=4000, finish_reason="length"), 400
    )
    assert "reasoning" in str(thinking) and "400-token" in str(thinking)

    silent = llm._empty_stream_error("some/model", llm.StreamTrace(), 400)
    assert "empty stream" in str(silent) and "reasoning" not in str(silent)


def test_a_failure_names_the_model_that_actually_ran():
    """`openrouter/auto` is a different model per request. Blaming the alias
    points the reader at their deployment instead of at the model."""
    routed = llm._empty_stream_error(
        "openrouter/auto",
        llm.StreamTrace(reasoning_chars=900, model="some/thinking-model"),
        2000,
    )
    assert "openrouter/auto → some/thinking-model" in str(routed)

    # Nothing to add when the provider ran what it was asked for.
    plain = llm._empty_stream_error("some/model", llm.StreamTrace(model="some/model"), 2000)
    assert "→" not in str(plain)


# ------------------------------------------------------- reasoning budget


def test_thinking_gets_its_own_room_rather_than_eating_the_answer(monkeypatch):
    """The provider's `max_tokens` covers reasoning *and* prose; every call site
    here means the answer alone. Seen in the wild: `openrouter/auto` routed a
    report to a thinking model, which spent the whole 2000-token ceiling
    reasoning and returned finish_reason=length with no answer at all."""
    monkeypatch.setattr(llm.settings, "reasoning_max_tokens", 1024)
    payload = llm.LLM("some/model", "openrouter")._payload(
        [], stream=True, temperature=0.3, max_tokens=2000
    )

    assert payload["reasoning"] == {"max_tokens": 1024}
    assert payload["max_tokens"] == 3024  # the answer's own budget, kept whole


def test_the_allowance_does_not_shrink_with_the_answer(monkeypatch):
    """Scaling it with the answer budget was the first attempt and is exactly
    backwards: a model thinks about the problem, not about the length of the
    reply. The 400-token findings call (the smallest in the app) is the one
    that failed first, on a model given 200 tokens to think in."""
    monkeypatch.setattr(llm.settings, "reasoning_max_tokens", 1024)
    small = llm.LLM("some/model", "openrouter")._payload(
        [], stream=True, temperature=0.3, max_tokens=400
    )
    large = llm.LLM("some/model", "openrouter")._payload(
        [], stream=True, temperature=0.3, max_tokens=8000
    )

    assert small["reasoning"]["max_tokens"] == 1024
    assert large["reasoning"]["max_tokens"] == 1024
    assert small["max_tokens"] == 1424


@pytest.mark.parametrize("provider", ["groq", "ollama", "lmstudio"])
def test_only_openrouter_is_sent_the_reasoning_field(monkeypatch, provider):
    """Nobody else documents it. They still get the headroom, which is a plain
    integer no provider can choke on."""
    monkeypatch.setattr(llm.settings, "reasoning_max_tokens", 1024)
    monkeypatch.setattr(llm.settings, "groq_api_key", "gsk-test")
    payload = llm.LLM("some/model", provider)._payload(
        [], stream=False, temperature=0.2, max_tokens=1000
    )

    assert "reasoning" not in payload
    assert payload["max_tokens"] == 2024


def test_a_caller_asking_for_no_thinking_says_so_out_loud(monkeypatch):
    """`reasoning_tokens=0` is a refusal, not an omission. Left to itself under
    the 24-token ceiling of a chat title, a thinking model spends the ceiling
    thinking and returns nothing, and it was ~90% of what naming a chat cost."""
    monkeypatch.setattr(llm.settings, "reasoning_max_tokens", 1024)
    payload = llm.LLM("some/model", "openrouter")._payload(
        [], stream=False, temperature=0.0, max_tokens=24, reasoning_tokens=0
    )

    assert payload["reasoning"] == {"enabled": False}
    assert payload["max_tokens"] == 24  # no headroom bolted on top


def test_the_allowance_can_be_turned_off_entirely(monkeypatch):
    monkeypatch.setattr(llm.settings, "reasoning_max_tokens", 0)
    payload = llm.LLM("some/model", "openrouter")._payload(
        [], stream=True, temperature=0.3, max_tokens=2000
    )

    assert "reasoning" not in payload
    assert payload["max_tokens"] == 2000


# ------------------------------------------------- streams that say nothing


def _sse(body: str, monkeypatch) -> list[int]:
    """Serve `body` as an event stream; returns the per-request call counter."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(
        llm.net, "client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(llm, "_backoff", lambda attempt: 0.0)
    return calls


async def test_a_stream_that_produced_nothing_is_retried_and_then_reported(monkeypatch):
    """Returning cleanly here would mean no retry, no log line, and an "empty
    answer" the reader can do nothing with."""
    calls = _sse('data: {"choices":[{"delta":{}}]}\ndata: [DONE]\n', monkeypatch)
    monkeypatch.setattr(llm.settings, "llm_max_retries", 2)

    with pytest.raises(llm.LLMError, match="empty stream"):
        [d async for d in llm.LLM("some/model", "openrouter").stream([])]
    assert len(calls) == 3  # the attempt and both retries


async def test_a_model_that_ran_out_of_room_to_think_is_given_more(monkeypatch):
    """Not every model honours a reasoning cap: the field is documented for
    Gemini, Anthropic and Qwen, and a DeepSeek asked for 400 tokens of findings
    thought straight through the ceiling and the sub-question failed. "Raise the
    limit" is advice this can take itself, once."""
    ceilings: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ceilings.append(json.loads(request.content)["max_tokens"])
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"reasoning":"still thinking…"},'
            '"finish_reason":"length"}]}\ndata: [DONE]\n',
            headers={"content-type": "text/event-stream"},
        )

    monkeypatch.setattr(
        llm.net, "client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(llm, "_backoff", lambda attempt: 0.0)
    monkeypatch.setattr(llm.settings, "reasoning_max_tokens", 1024)
    monkeypatch.setattr(llm.settings, "llm_max_retries", 2)

    with pytest.raises(llm.LLMError, match="reasoning"):
        [d async for d in llm.LLM("some/model", "openrouter").stream([], max_tokens=400)]

    # Once at the normal ceiling, once with four times the room, and then it
    # stops, because a third attempt would spend the same tokens again.
    assert ceilings == [1424, 400 + 4096]


async def test_a_model_that_only_ever_thinks_is_not_retried_forever(monkeypatch):
    """With no ceiling to widen there is nothing to try differently."""
    calls = _sse(
        'data: {"choices":[{"delta":{"reasoning":"still thinking…"}}]}\ndata: [DONE]\n',
        monkeypatch,
    )
    monkeypatch.setattr(llm.settings, "llm_max_retries", 2)

    with pytest.raises(llm.LLMError, match="reasoning"):
        [d async for d in llm.LLM("some/model", "openrouter").stream([])]
    assert len(calls) == 1


async def test_a_stream_that_broke_after_real_output_is_not_retried(monkeypatch):
    """Once text has reached the UI, a retry would duplicate it."""
    calls = _sse(
        'data: {"choices":[{"delta":{"content":"half an answ"}}]}\n'
        'data: {"error":{"code":502,"message":"upstream died"}}\n',
        monkeypatch,
    )
    monkeypatch.setattr(llm.settings, "llm_max_retries", 2)

    with pytest.raises(llm.LLMError, match="stream broke"):
        [d async for d in llm.LLM("some/model", "openrouter").stream([])]
    assert len(calls) == 1


# ------------------------------------------------------- structured output


@pytest.fixture
def attempts(monkeypatch):
    """Record whether each completion asked for native json_schema mode.

    The free models this app is meant to serve mostly reject it, and every
    structured call on one of them costs two round-trips until the answer is
    remembered.
    """
    llm._JSON_SCHEMA_MODE.clear()
    seen: list[bool] = []

    async def complete(self, messages, *, temperature=0.2, max_tokens=None, response_format=None):
        seen.append(response_format is not None)
        if response_format is not None:
            raise llm.LLMError("this model has no json_schema mode")
        return '{"needs_search": true, "queries": ["a"]}'

    monkeypatch.setattr(llm.LLM, "complete", complete)
    yield seen
    llm._JSON_SCHEMA_MODE.clear()


async def test_a_model_that_refused_json_schema_is_not_asked_again(attempts):
    client = llm.LLM("free/model", "openrouter")

    assert (await client.structured([], Schema)).needs_search is True
    assert (await client.structured([], Schema)).needs_search is True

    # First call: try, fail, fall back. Second call: straight to prompt mode.
    assert attempts == [True, False, False]


async def test_what_one_model_cannot_do_is_not_held_against_another(attempts):
    await llm.LLM("free/model", "openrouter").structured([], Schema)
    attempts.clear()

    await llm.LLM("free/model", "ollama").structured([], Schema)
    assert attempts[0] is True  # a different provider is its own question


async def test_a_transport_failure_is_not_remembered_as_a_missing_capability(monkeypatch):
    """A 502 says nothing about what the model can do. Remembering the
    provider's bad minute as a permanent limitation would cost every later call
    an extra round-trip for the rest of the process."""
    llm._JSON_SCHEMA_MODE.clear()
    seen: list[bool] = []

    async def complete(self, messages, *, temperature=0.2, max_tokens=None, response_format=None):
        seen.append(response_format is not None)
        if response_format is not None and len(seen) == 1:
            raise llm.LLMTimeoutError("provider returned HTTP 502")
        return '{"needs_search": true, "queries": []}'

    monkeypatch.setattr(llm.LLM, "complete", complete)
    client = llm.LLM("good/model", "openrouter")

    await client.structured([], Schema)
    await client.structured([], Schema)
    assert seen == [True, False, True]
    llm._JSON_SCHEMA_MODE.clear()


async def test_a_reply_that_is_not_json_is_asked_once_more(monkeypatch):
    """The failure this repairs is a widget that silently never appears: prompt
    mode is where reasoning models answer with a sentence first, and a single
    corrective turn recovers it."""
    llm._JSON_SCHEMA_MODE.clear()
    sent: list[list[dict]] = []
    ceilings: list[int | None] = []

    async def complete(self, messages, *, temperature=0.2, max_tokens=None, response_format=None):
        sent.append(messages)
        ceilings.append(max_tokens)
        if len(sent) == 1:
            return 'Sure! Here is the object: {"needs_search": tru'
        return '{"needs_search": true, "queries": []}'

    monkeypatch.setattr(llm.LLM, "complete", complete)
    llm._mark_prompt_mode("ollama", "local/model")

    result = await llm.LLM("local/model", "ollama").structured([], Schema, max_tokens=700)

    assert result.needs_search is True
    assert len(sent) == 2
    # The bad reply travels back, and the ceiling rises: a truncated object
    # retried under the same ceiling truncates in the same place.
    assert sent[1][-2]["role"] == "assistant"
    assert ceilings == [700, 1400]
    llm._JSON_SCHEMA_MODE.clear()


async def test_a_model_that_never_returns_json_fails_with_what_it_sent(monkeypatch, caplog):
    llm._JSON_SCHEMA_MODE.clear()

    async def complete(self, messages, *, temperature=0.2, max_tokens=None, response_format=None):
        return "I cannot help with that."

    monkeypatch.setattr(llm.LLM, "complete", complete)
    llm._mark_prompt_mode("ollama", "local/model")

    with (
        caplog.at_level(logging.INFO, logger="app.llm.client"),
        pytest.raises(llm.LLMError, match="usable JSON"),
    ):
        await llm.LLM("local/model", "ollama").structured([], Schema)

    # Two attempts, and the log says what came back rather than only that
    # something did not parse.
    assert "I cannot help with that." in caplog.text
    assert "2/2" in caplog.text
    llm._JSON_SCHEMA_MODE.clear()


# ------------------------------------------------------------------- usage


async def test_a_streamed_answer_records_what_the_provider_charged(monkeypatch):
    """The token count and the price exist in exactly one place: the final
    chunk of the stream, which carries usage and no choices."""
    body = (
        'data: {"choices":[{"delta":{"content":"Hi"}}]}\n'
        'data: {"choices":[],"usage":{"prompt_tokens":1874,"completion_tokens":332,'
        '"completion_tokens_details":{"reasoning_tokens":204},"cost":0.0003866}}\n'
        "data: [DONE]\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(
        llm.net, "client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    client = llm.LLM("some/model", "openrouter")

    assert [d.content async for d in client.stream([])] == ["Hi"]
    assert client.usage.total_tokens == 2206
    assert client.usage.reasoning_tokens == 204
    assert client.usage.as_dict()["cost_usd"] == 0.000387  # six decimals of a dollar


def test_a_run_that_nobody_quoted_a_price_for_reports_none_rather_than_zero():
    """A local runtime charges nothing and knows nothing. "$0.00" would be a
    claim about the bill; absence is the truth."""
    usage = llm.Usage()
    usage.add({"prompt_tokens": 100, "completion_tokens": 20})

    assert usage.total_tokens == 120
    assert usage.as_dict()["cost_usd"] is None

    priced = llm.Usage()
    priced.add({"prompt_tokens": 5, "cost": 0.0})
    assert priced.as_dict()["cost_usd"] == 0.0  # free is a price


def test_the_bill_covers_every_model_a_run_used():
    """The router is a second client and often a second model."""
    answer, router = llm.Usage(), llm.Usage()
    answer.add({"prompt_tokens": 900, "completion_tokens": 300, "cost": 0.002})
    router.add({"prompt_tokens": 400, "completion_tokens": 50, "cost": 0.0001})
    answer.merge(router)

    assert answer.total_tokens == 1650
    assert answer.as_dict()["cost_usd"] == 0.0021


def test_local_providers_need_no_api_key():
    assert llm.provider_headers("ollama") == {"Content-Type": "application/json"}
    assert llm.provider_base_url("ollama").endswith("/v1")


def test_groq_uses_its_own_key_and_base_url(monkeypatch):
    monkeypatch.setattr(llm.settings, "groq_api_key", "gsk-test")
    monkeypatch.setattr(llm.settings, "openrouter_api_key", "or-test")

    headers = llm.provider_headers("groq")
    assert headers["Authorization"] == "Bearer gsk-test"
    assert llm.provider_base_url("groq") == "https://api.groq.com/openai/v1"

    assert llm.provider_headers("openrouter")["Authorization"] == "Bearer or-test"


def test_groq_without_a_key_fails_loud_and_early(monkeypatch):
    monkeypatch.setattr(llm.settings, "groq_api_key", None)
    with pytest.raises(llm.LLMConfigError, match="GROQ_API_KEY"):
        llm.provider_headers("groq")
