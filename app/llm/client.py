"""One OpenAI-wire client for every provider.

OpenRouter, Groq, Ollama and LM Studio all speak `/v1/chat/completions`, so they
need one implementation with a different `base_url`, not one SDK each.

Three non-negotiables:

* **Explicit timeouts.** Inheriting an SDK's 10-minute × 2-retry default means a
  stalled provider hangs a run for half an hour.
* **Bounded concurrency.** A module-level semaphore, so a fan-out over search
  results cannot open fifty simultaneous completions.
* **Errors that reach the user.** Every failure raises an `LLMError` whose
  message is safe and specific enough to render in the UI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app import net
from app.config import settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

OPENROUTER = "openrouter"
OLLAMA = "ollama"
LMSTUDIO = "lmstudio"
GROQ = "groq"

# Shared across every run in the process. Deep research fans out over
# sub-questions, and without this one query can saturate the provider.
_gate = asyncio.Semaphore(settings.llm_concurrency)

_RETRY_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})

# Which models honour `response_format: json_schema`, learned by trying.
#
# The free models this app is meant to serve mostly do not, and every structured
# call on one of them costs two round-trips: the schema attempt, the failure,
# then the prompt-mode retry. Remembering the answer makes that a once-per-model
# cost instead of a once-per-call one, which on a classifier plus a widget is the
# difference between four provider calls and two.
#
# Process-local and deliberately not persisted. A restart re-learns it in one
# call, and a model that gains the capability at the provider is picked up by
# the same restart, which is the right trade for a single-replica app.
_JSON_SCHEMA_MODE: dict[tuple[str, str], bool] = {}


class LLMError(RuntimeError):
    """Base for anything the user should be told about in plain language."""


class LLMConfigError(LLMError):
    pass


class LLMAuthError(LLMError):
    pass


class LLMRateLimitError(LLMError):
    pass


class LLMTimeoutError(LLMError):
    pass


@dataclass(slots=True)
class Delta:
    """One streamed fragment. `reasoning` is OpenRouter's separate channel for
    thinking models, kept apart from the answer so the UI can show it as a
    trace rather than splicing it into the prose."""

    content: str = ""
    reasoning: str = ""


@dataclass(slots=True)
class StreamTrace:
    """What a finished stream actually did.

    A stream that ends without a single content token is the one failure the
    wire format has no way of reporting: HTTP 200, well-formed chunks, no
    prose. Whether that was a thinking model burning its whole ceiling on
    reasoning or a provider that hung up after the first chunk is invisible
    from the outside, and it is exactly the difference between "raise
    max_tokens" and "retry". This records enough to tell them apart.
    """

    content_chars: int = 0
    reasoning_chars: int = 0
    finish_reason: str = ""
    # What the provider actually ran. Under `openrouter/auto` this is a
    # different model from one request to the next, which is the whole reason a
    # failure can look like it belongs to the deployment rather than the model:
    # the same install answers all morning and then routes one question to a
    # model that thinks instead.
    model: str = ""


@dataclass
class Usage:
    """What the provider says a run cost, accumulated across its calls.

    Reported, never computed. A token count derived from a local tokenizer and
    a price table would be a guess dressed as an invoice, and it would go
    quietly wrong on exactly the models where it matters, the ones that bill
    reasoning tokens the response never shows. `priced` says whether anyone
    quoted a price at all: a local runtime charges nothing and knows nothing,
    which is not the same as costing zero.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    priced: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, raw: dict[str, Any] | None) -> None:
        if not isinstance(raw, dict):
            return
        self.prompt_tokens += int(raw.get("prompt_tokens") or 0)
        self.completion_tokens += int(raw.get("completion_tokens") or 0)
        details = raw.get("completion_tokens_details")
        if isinstance(details, dict):
            self.reasoning_tokens += int(details.get("reasoning_tokens") or 0)
        cost = raw.get("cost")
        if isinstance(cost, (int, float)):
            self.cost_usd += float(cost)
            self.priced = True

    def merge(self, other: Usage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.cost_usd += other.cost_usd
        self.priced = self.priced or other.priced

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            # Absent rather than zero when nobody quoted a price: a footer
            # reading "$0.00" for a local model would be a claim, not a number.
            "cost_usd": round(self.cost_usd, 6) if self.priced else None,
        }


def is_local(provider: str) -> bool:
    return provider in (OLLAMA, LMSTUDIO)


def provider_base_url(provider: str) -> str:
    """Where this provider's OpenAI-compatible API lives."""
    if provider == OLLAMA:
        return f"{settings.ollama_base_url.rstrip('/')}/v1"
    if provider == LMSTUDIO:
        return f"{settings.lmstudio_base_url.rstrip('/')}/v1"
    if provider == GROQ:
        # Groq's base URL already includes /v1; only the local runtimes need it
        # appended to a bare host:port.
        return settings.groq_base_url.rstrip("/")
    return settings.openrouter_base_url.rstrip("/")


def provider_headers(provider: str) -> dict[str, str]:
    """Auth for this provider. Local runtimes take none; remote providers must
    have a key, and saying so early beats a 401 halfway through a run."""
    if is_local(provider):
        return {"Content-Type": "application/json"}
    if provider == GROQ:
        if not settings.groq_api_key:
            raise LLMConfigError("No Groq API key is configured. Set GROQ_API_KEY and restart.")
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.groq_api_key}",
        }
    if not settings.openrouter_api_key:
        raise LLMConfigError(
            "No OpenRouter API key is configured. Set OPENROUTER_API_KEY and restart."
        )
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        # Optional attribution; OpenRouter uses these for its dashboard.
        "HTTP-Referer": settings.app_url,
        "X-Title": settings.app_title,
    }


class LLM:
    """A chat model at a provider. Cheap to construct; state lives in `net`."""

    def __init__(self, model: str, provider: str = OPENROUTER) -> None:
        self.model = model
        self.provider = provider
        # How the last stream ended. The caller cannot see it otherwise (an
        # async generator has no return value), and "the model stopped because
        # it ran out of room" is the difference between an answer and an answer
        # that ends mid-word.
        self.last_finish_reason = ""
        # One client per run, so this is the run's bill.
        self.usage = Usage()

    # ------------------------------------------------------------- transport

    @property
    def base_url(self) -> str:
        return provider_base_url(self.provider)

    @property
    def is_local(self) -> bool:
        return is_local(self.provider)

    def _headers(self) -> dict[str, str]:
        return provider_headers(self.provider)

    def _payload(
        self,
        messages: list[dict[str, Any]],
        *,
        stream: bool,
        temperature: float,
        max_tokens: int | None,
        response_format: dict[str, Any] | None = None,
        reasoning_tokens: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": stream,
        }
        if max_tokens:
            # `max_tokens` at the provider is the ceiling on *everything the
            # model emits*, reasoning included, while every call site here means
            # it as the length of the answer. On a thinking model the two
            # readings collide and reasoning wins: it is generated first, and a
            # 400-token findings call or a 2000-token report can hit the ceiling
            # having written nothing at all. Observed exactly that, against
            # `openrouter/auto`, which picks a different model per request, so
            # the same install answers normally all morning and then does not.
            #
            # So the thinking gets its own room on top, and is capped there.
            budget = _reasoning_budget() if reasoning_tokens is None else max(reasoning_tokens, 0)
            payload["max_tokens"] = max_tokens + budget
            if budget and self.provider == OPENROUTER:
                # Ignored by models that do not reason, converted to an effort
                # level by those that only understand one. Only OpenRouter is
                # documented to take it; Groq and the local runtimes get the
                # headroom without the cap.
                payload["reasoning"] = {"max_tokens": budget}
            elif reasoning_tokens == 0 and self.provider == OPENROUTER:
                # A caller that asked for none gets a refusal rather than
                # silence. Left to itself a thinking model under a 24-token
                # ceiling spends the ceiling thinking and returns nothing, and
                # the deployment-wide off switch (`reasoning_max_tokens = 0`)
                # deliberately sends no field at all, so this cannot ride on it.
                payload["reasoning"] = {"enabled": False}
        if response_format:
            payload["response_format"] = response_format
        if stream and self.provider != LMSTUDIO:
            # Ask for a final usage chunk. Ollama serves one as of 0.24 (checked
            # against a running instance), and its `prompt_tokens` is the only
            # honest way to learn that a local runtime silently truncated the
            # prompt to its window: what was sent and what was read differ, and
            # nothing else in the response says so. LM Studio is left out
            # because nothing here has verified that it accepts the option.
            payload["stream_options"] = {"include_usage": True}
        return payload

    # ------------------------------------------------------------- non-stream

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        reasoning_tokens: int | None = None,
    ) -> str:
        """`reasoning_tokens=0` denies a thinking model its scratchpad, for the
        calls where thinking is not worth what it costs. None, the default,
        leaves the deployment-wide allowance in place."""
        payload = self._payload(
            messages,
            stream=False,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            reasoning_tokens=reasoning_tokens,
        )
        data = await self._post_with_retry(payload)
        self.usage.add(data.get("usage"))
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"{self.model} returned no choices")
        message = choices[0].get("message") or {}
        return (message.get("content") or "").strip()

    async def _post_with_retry(self, payload: dict[str, Any]) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(settings.llm_max_retries + 1):
            try:
                async with _gate:
                    response = await net.client().post(
                        f"{self.base_url}/chat/completions",
                        json=payload,
                        headers=self._headers(),
                        timeout=settings.llm_timeout_s,
                    )
                if response.status_code >= 400:
                    raise _from_response(response, self.model)
                return response.json()
            except (LLMRateLimitError, LLMTimeoutError) as exc:
                last = exc
            except LLMError:
                raise  # auth and config errors will not improve with a retry
            except httpx.TimeoutException as exc:
                last = LLMTimeoutError(
                    f"{self.model} timed out after {settings.llm_timeout_s:.0f}s"
                )
                log.warning("llm timeout (%s attempt %d)", self.model, attempt + 1, exc_info=exc)
            except httpx.HTTPError as exc:
                last = LLMError(f"Could not reach {self.provider}: {type(exc).__name__}")
            if attempt < settings.llm_max_retries:
                await asyncio.sleep(_backoff(attempt))
        raise last or LLMError(f"{self.model} failed for an unknown reason")

    # ----------------------------------------------------------------- stream

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> AsyncIterator[Delta]:
        """Yield deltas as they arrive.

        Retries only until the first byte of content. Once text has reached the
        UI a retry would duplicate it, so from that point a failure is a failure.

        A stream that ends having yielded no content at all is treated as a
        failed attempt, not a successful empty answer. It used to `return`
        cleanly, which is how a provider that dropped the connection after one
        chunk reached the reader as "The model returned an empty answer", with
        no retry, no log line and nothing to act on.

        A model that thought until it ran out of room is retried *once with more
        room*, because "raise the limit" is advice this can act on itself. Not
        every model honours a reasoning cap: the field is documented for
        Gemini, Anthropic and Qwen, and a DeepSeek asked for 400 tokens of
        findings will happily think through 600. So the cap alone is not
        enough to keep a sub-question from failing.
        """
        allowance = _reasoning_budget()
        last: Exception | None = None
        widened = False

        for attempt in range(settings.llm_max_retries + 1):
            produced = False
            trace = StreamTrace()
            payload = self._payload(
                messages,
                stream=True,
                temperature=temperature,
                max_tokens=max_tokens,
                reasoning_tokens=allowance,
            )
            try:
                async with (
                    _gate,
                    net.client().stream(
                        "POST",
                        f"{self.base_url}/chat/completions",
                        json=payload,
                        headers=self._headers(),
                        timeout=httpx.Timeout(settings.llm_stream_timeout_s, connect=10.0),
                    ) as response,
                ):
                    if response.status_code >= 400:
                        await response.aread()
                        raise _from_response(response, self.model)
                    async for delta in _parse_sse(response, self.usage, trace):
                        produced = produced or bool(delta.content)
                        yield delta
            except (LLMAuthError, LLMConfigError):
                raise
            except (httpx.HTTPError, LLMError) as exc:
                if produced:
                    raise LLMError(f"The answer stream broke: {_brief(exc)}") from exc
                last = exc if isinstance(exc, LLMError) else LLMError(_brief(exc))
                log.warning(
                    "stream attempt %d failed for %s: %s", attempt + 1, self.model, _brief(exc)
                )
            else:
                self.last_finish_reason = trace.finish_reason
                if produced:
                    return
                # The ceiling that actually went out, not the answer budget the
                # caller asked for: "spent its whole 400-token budget" reads as
                # a bug in the caller when the request said 1424.
                empty = _empty_stream_error(
                    self.model, trace, (max_tokens + allowance) if max_tokens else None
                )
                log.warning(
                    "stream attempt %d for %s produced nothing: %s (reasoning=%d chars)",
                    attempt + 1,
                    self.model,
                    _brief(empty),
                    trace.reasoning_chars,
                )
                if trace.reasoning_chars:
                    if widened or not max_tokens or allowance <= 0:
                        # Already given the bigger ceiling, or there is no
                        # ceiling to widen. Raised rather than retried: the
                        # outcome is deterministic and another attempt would
                        # spend the same tokens to reach the same place.
                        raise empty
                    widened = True
                    allowance *= _WIDENED_REASONING
                    # Kept in case this was the last attempt: `continue` would
                    # otherwise fall out of the loop with nothing to report.
                    last = empty
                    log.info("retrying %s with %d tokens of reasoning room", self.model, allowance)
                    continue  # straight away: nothing is overloaded, it is thinking
                last = empty
            if attempt < settings.llm_max_retries:
                await asyncio.sleep(_backoff(attempt))
        raise last or LLMError(f"{self.model} produced no output")

    # ------------------------------------------------------------- structured

    async def structured(
        self,
        messages: list[dict[str, Any]],
        schema: type[T],
        *,
        max_tokens: int | None = None,
    ) -> T:
        """Return a validated pydantic object.

        Native JSON-schema mode first; a model or runtime that rejects it falls
        back to asking for JSON in the prompt. Local runtimes are the usual
        reason the first path fails.

        `max_tokens` is worth passing. Without it OpenRouter reserves the
        model's entire output window against the account's balance (64k tokens
        for a call that returns a few hundred) and refuses the request outright
        when the balance is lower than that reservation, whatever the call would
        actually have cost.
        """
        json_schema = _harden(schema.model_json_schema())

        if _honours_json_schema(self.provider, self.model):
            response_format = {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "strict": True, "schema": json_schema},
            }
            try:
                raw = await self.complete(
                    messages,
                    temperature=0.0,
                    max_tokens=max_tokens,
                    response_format=response_format,
                )
                return schema.model_validate_json(_extract_json(raw))
            except (LLMAuthError, LLMConfigError, LLMRateLimitError):
                raise
            except LLMTimeoutError as exc:
                # A timeout or a 502 says nothing about the model's capabilities.
                # Retrying in prompt mode is still worth it; remembering the
                # provider's bad minute as a permanent limitation is not.
                log.info("structured call fell back after a transport error: %s", _brief(exc))
            except TruncatedJSON:
                # Cut off by the token ceiling, which is a fact about this call's
                # budget, not about the model. Remembering it as "no json_schema
                # mode" moved Gemini Flash, which honours the mode, to the slower
                # and less reliable prompt path for the rest of the process after
                # one long widget reply. Prompt mode below doubles the ceiling on
                # its retry, so this call still has its way out.
                log.info("structured reply was cut off in schema mode (%s)", self.model)
            except (LLMError, ValidationError, ValueError) as exc:
                _mark_prompt_mode(self.provider, self.model)
                log.info(
                    "structured call fell back to prompt mode (%s): %s", self.model, _brief(exc)
                )

        nudged = [
            *messages,
            {
                "role": "user",
                "content": (
                    "Reply with a single JSON object and nothing else: no prose, no code "
                    f"fence. It must match this JSON schema:\n{json.dumps(json_schema)}"
                ),
            },
        ]
        # Two attempts, because the two ways prompt mode fails are both worth one
        # more call: a reply that opened with a sentence, and a JSON object cut
        # off by the token ceiling. The retry shows the model its own bad reply:
        # asking again from scratch at temperature 0 mostly reproduces it, and
        # doubles the ceiling, so a truncation cannot fail the same way twice.
        budget = max_tokens
        for attempt in (1, 2):
            raw = await self.complete(nudged, temperature=0.0, max_tokens=budget)
            try:
                return schema.model_validate_json(_extract_json(raw))
            except (ValidationError, ValueError) as exc:
                log.info(
                    "structured reply %d/2 was not usable (%s): %s",
                    attempt,
                    _brief(exc),
                    _snippet(raw),
                )
                if attempt == 2:
                    raise LLMError(f"{self.model} did not return usable JSON") from exc
                nudged = [
                    *nudged,
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": (
                            "That reply did not parse as an object matching the schema. "
                            "Send the corrected JSON object only. It must start with `{` "
                            "and end with `}`, with no prose and no code fence."
                        ),
                    },
                ]
                budget = budget * 2 if budget else None
        raise LLMError(f"{self.model} did not return usable JSON")  # unreachable


# --------------------------------------------------------------------- helpers


def _honours_json_schema(provider: str, model: str) -> bool:
    """Optimistic until proven otherwise: a model is tried once."""
    return _JSON_SCHEMA_MODE.get((provider, model), True)


def _mark_prompt_mode(provider: str, model: str) -> None:
    if _JSON_SCHEMA_MODE.get((provider, model)) is not False:
        _JSON_SCHEMA_MODE[(provider, model)] = False
        log.info(
            "%s/%s does not honour json_schema; using prompt mode for the rest of this process",
            provider,
            model,
        )


async def _parse_sse(
    response: httpx.Response,
    usage: Usage | None = None,
    trace: StreamTrace | None = None,
) -> AsyncIterator[Delta]:
    """Decode an OpenAI-style `text/event-stream` body into deltas.

    The last chunk of an OpenRouter stream carries `usage` and no choices,
    it is the only place the answer's token count and price exist. Groq puts
    the same numbers under `x_groq` instead, so a Groq answer used to arrive
    billed at zero tokens.

    **A chunk may carry an error instead of a choice.** An upstream provider
    that fails after the response has begun cannot change the status code, so
    OpenRouter reports it in the body: HTTP 200, then `{"error": {...}}`.
    Reading only `choices` turned that into a stream that ended politely with
    nothing in it, and the run blamed the model for an empty answer.
    """
    async for line in response.aiter_lines():
        if not line or line.startswith(":"):
            # ": OPENROUTER PROCESSING" keep-alive comments land here.
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        if usage is not None:
            usage.add(chunk.get("usage") or _groq_usage(chunk))
        error = _error_in(chunk)
        if error is not None:
            raise error
        if trace is not None and chunk.get("model"):
            trace.model = str(chunk["model"])
        for choice in chunk.get("choices") or []:
            if trace is not None and choice.get("finish_reason"):
                trace.finish_reason = str(choice["finish_reason"])
            delta = choice.get("delta") or {}
            content = delta.get("content") or ""
            reasoning = delta.get("reasoning") or ""
            if trace is not None:
                trace.content_chars += len(content)
                trace.reasoning_chars += len(reasoning)
            if content or reasoning:
                yield Delta(content=content, reasoning=reasoning)


def _groq_usage(chunk: dict[str, Any]) -> dict[str, Any] | None:
    """Groq's token counts, which live under `x_groq` in the final chunk rather
    than at the top level. No cost is ever quoted there, so a Groq run still
    reports tokens and no price, which is the truth."""
    extra = chunk.get("x_groq")
    return extra.get("usage") if isinstance(extra, dict) else None


def _error_in(chunk: dict[str, Any]) -> LLMError | None:
    """The error a 200 response can still be carrying, at either level."""
    error = chunk.get("error")
    if error is None:
        for choice in chunk.get("choices") or []:
            if isinstance(choice, dict) and choice.get("error"):
                error = choice["error"]
                break
    if error is None:
        return None
    if not isinstance(error, dict):
        return LLMError(f"the provider aborted the stream: {str(error)[:200]}")

    message = str(error.get("message") or error.get("type") or "no reason given")[:200]
    code = error.get("code")
    status = code if isinstance(code, int) else 0
    if status in (401, 403):
        return LLMAuthError(f"the provider rejected the API key mid-stream: {message}")
    if status == 402:
        return LLMAuthError(f"out of credit at the provider: {message}")
    if status == 429:
        return LLMRateLimitError(f"rate limited mid-stream: {message}")
    if status in _RETRY_STATUS:
        return LLMTimeoutError(f"the provider aborted the stream (HTTP {status}): {message}")
    return LLMError(f"the provider aborted the stream: {message}")


def _from_response(response: httpx.Response, model: str) -> LLMError:
    """Turn a provider error body into something worth showing a human."""
    detail = ""
    try:
        body = response.json()
        error = body.get("error")
        detail = (error or {}).get("message", "") if isinstance(error, dict) else str(error or "")
    except Exception:
        detail = (response.text or "")[:200]

    status = response.status_code
    if status in (401, 403):
        return LLMAuthError(f"{model}: the provider rejected the API key ({detail or status})")
    if status == 402:
        return LLMAuthError(f"{model}: out of credit at the provider ({detail or status})")
    if status == 404:
        return LLMError(f"{model} is not available at this provider. Pick another model.")
    if status == 429:
        return LLMRateLimitError(f"{model} is rate limited{f': {detail}' if detail else ''}")
    if status in _RETRY_STATUS:
        return LLMTimeoutError(
            f"{model}: provider returned HTTP {status}{f': {detail}' if detail else ''}"
        )
    return LLMError(f"{model}: HTTP {status}{f': {detail}' if detail else ''}")


def _reasoning_budget() -> int:
    """How much thinking a call may do *on top of* its answer budget.

    A flat allowance, not a share of the answer budget. Scaling it with the
    answer was the obvious first idea and is exactly wrong: a model thinks about
    the *problem*, not about the length of the reply, so the calls that ask for
    the least prose need the most headroom relative to it. A 400-token findings
    note given 200 tokens to think in failed on the first thinking model it met.

    Zero disables the mechanism, and with it the `reasoning` field: the ceilings
    then mean what they did before, which is the right escape hatch for anyone
    who finds this costing more than it saves.
    """
    return max(settings.reasoning_max_tokens, 0)


# What a ceiling becomes after a model has proved it cannot write inside it.
# Four times the allowance rather than a little more, because the failure says
# the model wanted more room than it was given and says nothing about how much
# more: a 20% bump would buy one more attempt at the same wall.
_WIDENED_REASONING = 4


def _empty_stream_error(model: str, trace: StreamTrace, max_tokens: int | None) -> LLMError:
    """Why a well-formed stream contained no answer.

    The two cases need different words because they need different fixes. A
    model that filled its ceiling with reasoning wants a bigger ceiling or a
    different model; a stream that carried nothing at all wants a retry.

    Named by what actually ran, not by what was asked for: under
    `openrouter/auto` those are different models, and "openrouter/auto failed"
    sends the reader looking at their deployment instead of at the model.
    """
    ran = f"{model} → {trace.model}" if trace.model and trace.model != model else model
    where = f" (finish_reason: {trace.finish_reason})" if trace.finish_reason else ""
    if trace.reasoning_chars:
        limit = f"{max_tokens}-token" if max_tokens else "output"
        return LLMError(
            f"{ran} spent its whole {limit} budget on reasoning and never began the "
            f"answer{where}. Raise the limit or pick a model that thinks less"
        )
    return LLMError(f"{ran} returned an empty stream{where}")


def _backoff(attempt: int) -> float:
    """Exponential with jitter: a synchronised retry storm helps nobody."""
    return min(8.0, 0.75 * (2**attempt)) * (0.5 + random.random() / 2)


def _brief(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or type(exc).__name__


def _snippet(raw: str, limit: int = 200) -> str:
    """Enough of a bad reply to tell truncation from prose from an empty answer.

    Without it "did not return usable JSON" is a log line that cannot be acted
    on: a reasoning model that spent its whole ceiling on thinking and a model
    that opened with "Sure!" look identical from the outside.
    """
    text = " ".join((raw or "").split())
    if not text:
        return "<empty reply>"
    return text if len(text) <= limit else f"{text[:limit]}…"


class TruncatedJSON(ValueError):
    """A reply that opened a JSON object and never closed it.

    Its own type because the remedy differs from every other parse failure: this
    one is the token ceiling talking, and the model was doing what it was asked.
    """


def _extract_json(raw: str) -> str:
    """Pull the JSON object out of a reply that may be fenced or chatty."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text.removeprefix("json").strip()
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in the reply")
    depth, in_string, escaped = 0, False, False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise TruncatedJSON("unterminated JSON object in the reply")


# Keys pydantic writes that are for people reading the schema, not for a model
# filling it. `description` is the one that mattered: it carries the class
# docstring, so every widget call shipped the design notes in this codebase,
# "the price of a panel composed from several blocks…", as instructions to the
# model, which competed with the real ones in `prompts.py`. `default` is not
# accepted by strict mode at several providers, and the prompts already say
# what an empty field means.
_SCHEMA_NOISE = ("title", "description", "default")


def _harden(schema: dict[str, Any]) -> dict[str, Any]:
    """Make a pydantic schema acceptable to OpenAI-style strict mode.

    Strict mode wants every object closed and every property required. Optional
    fields keep working because the prompts give the model an explicit empty
    value to use.

    Nested models live under `$defs`, and they are objects too. Hardening only
    the top level left every row, step and side unclosed and optional, which a
    lenient provider tolerates and a strict one rejects, costing the call.
    """
    for key in _SCHEMA_NOISE:
        schema.pop(key, None)
    for definition in (schema.get("$defs") or {}).values():
        if isinstance(definition, dict):
            _harden(definition)
    if schema.get("type") == "object":
        properties = schema.get("properties") or {}
        schema["additionalProperties"] = False
        schema["required"] = list(properties)
        for value in properties.values():
            _harden(value)
    items = schema.get("items")
    if isinstance(items, dict):
        _harden(items)
    for key in ("anyOf", "oneOf", "allOf"):
        for value in schema.get(key) or []:
            if isinstance(value, dict):
                _harden(value)
    return schema
