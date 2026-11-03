"""Redact: find what identifies someone in a document, on a local model only.

The point of the tool is that the document never leaves the machine, so a
remote provider is refused here rather than merely not offered in the UI. A
setting that could quietly route a contract through OpenRouter would make the
tool worse than useless: it would look safe.

The work is split so that a small model with a 4k window can do it:

* `chunks` cuts the text into excerpts sized from `local_context_tokens`, with
  a little overlap so a name broken across a boundary is whole in one of them.
* `scan` asks the model for a *list* of spans per excerpt, see `prompts.REDACT`
  for why it never rewrites anything, and keeps only spans that really occur
  in the excerpt. A small model invents plausible names; one that is not in
  the text would be harmless to redact but would teach the reviewer to stop
  trusting the list.
* Placing the spans, numbering the placeholders and the regex detectors live in
  the browser (`web/js/tools/redact.js`), because that is where the person reviewing
  them toggles, adds and copies, and a round-trip per click would be absurd.

The server is stateless across calls. The browser drives the excerpts
one request at a time, so progress, cancelling and retrying one failed excerpt
need no job store, and nothing of the document is kept once a request ends.
"""

from __future__ import annotations

import io
import logging
import re

from pydantic import BaseModel

from app.agent import prompts
from app.config import settings
from app.llm.client import LLM, is_local
from app.scholar.fulltext import unsurrogate

log = logging.getLogger(__name__)

# The vocabulary the model is asked for, and the one the browser turns into
# placeholders ([PERSON_1], [IBAN_2]). Anything else a model improvises becomes
# "other" rather than being dropped: a span worth flagging under a wrong name is
# still worth flagging.
KINDS = (
    "person",
    "org",
    "address",
    "email",
    "phone",
    "url",
    "iban",
    "card",
    "vin",
    "id",
    "secret",
    "date",
    "other",
)

# The names small models actually use for the same things.
_ALIASES = {
    "name": "person",
    "people": "person",
    "organization": "org",
    "organisation": "org",
    "company": "org",
    "location": "address",
    "e-mail": "email",
    "mail": "email",
    "telephone": "phone",
    "fax": "phone",
    "website": "url",
    "domain": "url",
    "link": "url",
    "bank": "iban",
    "account": "iban",
    "credit_card": "card",
    "password": "secret",
    "pin": "secret",
    "token": "secret",
    "key": "secret",
    "username": "secret",
    "number": "id",
    "reference": "id",
    "plate": "id",
    "birthdate": "date",
    "dob": "date",
}

# Room the prompt and the reply take out of the window, in tokens. The prompt is
# ~450 tokens; prompt-mode JSON adds the schema again. The reply is a list of
# short strings, and 700 tokens is about forty of them, which no excerpt this
# size has needed.
PROMPT_TOKENS = 650
ANSWER_TOKENS = 700
# Conservative for German, whose long compounds cost more tokens per character
# than English. Overestimating the room is the failure that matters: Ollama
# truncates an overlong prompt silently (see `config.local_context_tokens`), and
# the part that falls away is the end of the excerpt, which then goes unread.
CHARS_PER_TOKEN = 3
# Smaller excerpts find more on a small model, which skims a long one. The
# ceiling holds even when a large window would allow more, because recall, not
# the number of calls, is what this tool is judged on.
MAX_CHUNK_CHARS = 3000
MIN_CHUNK_CHARS = 600
OVERLAP_CHARS = 150

MAX_TEXT_CHARS = 500_000
MAX_PDF_BYTES = 25 * 1024 * 1024
# Longer than any honest span. A model that returns a whole paragraph as one
# "item" would black out the clause around the name.
MAX_SPAN_CHARS = 160


class RedactError(RuntimeError):
    """A message for the person, not a stack trace."""


class _Item(BaseModel):
    text: str
    kind: str


class _Findings(BaseModel):
    items: list[_Item]


def chunk_chars(context_tokens: int | None = None) -> int:
    """How long one excerpt may be for this install's local window."""
    window = context_tokens or settings.local_context_tokens
    # Reasoning headroom is added to every call's max_tokens by the client, and
    # on a local runtime whatever is generated shares the window with the prompt.
    room = window - PROMPT_TOKENS - ANSWER_TOKENS - max(settings.reasoning_max_tokens, 0)
    return max(MIN_CHUNK_CHARS, min(MAX_CHUNK_CHARS, room * CHARS_PER_TOKEN))


# Where an excerpt may end, best first: a blank line, a line, a sentence, a word.
_BREAKS = ("\n\n", "\n", ". ", "; ", ", ", " ")


def chunks(text: str, size: int | None = None, overlap: int = OVERLAP_CHARS) -> list[list[int]]:
    """`[start, end]` offsets of excerpts covering `text`, each at most `size`.

    Cut at the latest natural break in the back half of the window, so an
    excerpt is never a sliver because a paragraph break happened to fall early.
    Consecutive excerpts overlap by about `overlap` characters, started at a
    word boundary: a name that straddles a cut is whole in the next excerpt.
    """
    size = size or chunk_chars()
    overlap = min(overlap, size // 4)
    spans: list[list[int]] = []
    start, length = 0, len(text)
    while start < length:
        end = min(start + size, length)
        if end < length:
            floor = start + size // 2
            for mark in _BREAKS:
                cut = text.rfind(mark, floor, end)
                if cut != -1:
                    end = cut + len(mark)
                    break
        spans.append([start, end])
        if end >= length:
            break
        resume = max(end - overlap, start + 1)
        space = text.find(" ", resume, end)
        start = space + 1 if space != -1 else end
    return spans


def normalise_kind(kind: str) -> str:
    key = (kind or "").strip().lower().replace(" ", "_")
    if key in KINDS:
        return key
    return _ALIASES.get(key, "other")


def locate(excerpt: str, text: str) -> str | None:
    """The span as the excerpt writes it, or None if it is not there.

    Exact first. Then forgiving about case and whitespace, the two things small
    models change while copying (a line break inside an address comes back as a
    space), and the excerpt's own spelling is returned so the browser can find
    it again verbatim.
    """
    needle = text.strip()
    if len(needle) < 2 or len(needle) > MAX_SPAN_CHARS:
        return None
    if needle in excerpt:
        return needle
    pattern = r"\s+".join(re.escape(part) for part in needle.split())
    found = re.search(pattern, excerpt, flags=re.IGNORECASE)
    return found.group(0) if found else None


def keep(excerpt: str, findings: _Findings) -> list[dict[str, str]]:
    """Only what the excerpt contains, once each, with a known kind."""
    seen: set[str] = set()
    kept: list[dict[str, str]] = []
    for item in findings.items:
        span = locate(excerpt, item.text)
        if span is None:
            continue
        # The excerpt itself, or most of it, is not a span: a model that gave
        # up and echoed its input would otherwise black out everything.
        if len(span) > len(excerpt) * 0.6 and len(excerpt) > 40:
            continue
        key = span.casefold()
        if key in seen:
            continue
        seen.add(key)
        kept.append({"text": span, "kind": normalise_kind(item.kind)})
    return kept


async def scan(provider: str, model: str, excerpt: str) -> tuple[list[dict[str, str]], int]:
    """What one excerpt contains that should be hidden, and how many spans the
    model proposed before those not in the excerpt were dropped.

    The count is for the progress log: a model that proposes twelve names of
    which two are real is visibly guessing, and the person watching should see
    that rather than a quiet "2 found".

    Raises `RedactError` for a provider that is not local, and lets `LLMError`
    through: the browser marks that excerpt as unchecked and says so, which is
    the honest outcome. Retrying it is the person's call.
    """
    if not is_local(provider):
        raise RedactError("Redact only runs on a local model (Ollama or LM Studio).")
    if not model:
        raise RedactError("Choose a local model first.")
    llm = LLM(model, provider)
    findings = await llm.structured(prompts.redact(excerpt), _Findings, max_tokens=ANSWER_TOKENS)
    kept = keep(excerpt, findings)
    # Counts only. The spans are the very thing this tool exists to keep out
    # of places like a log file.
    log.info(
        "redact: %s found %d spans (%d proposed) in %d chars",
        model,
        len(kept),
        len(findings.items),
        len(excerpt),
    )
    return kept, len(findings.items)


def pdf_text(data: bytes) -> tuple[str, int]:
    """The text layer of a PDF, pages separated by a blank line.

    Blocking (pypdf is pure Python), so callers run it in a thread. A scanned
    PDF has no text layer and comes back empty; that is reported as such rather
    than as an empty document with nothing to redact, which would read as a
    clean bill of health.
    """
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise RedactError("The PDF is password protected. Remove the password and try again.")
        # Mended here too: a lone surrogate from a formula cannot go out as
        # JSON (`fulltext.unsurrogate`).
        pages = [unsurrogate(page.extract_text() or "").strip() for page in reader.pages]
    except RedactError:
        raise
    except PdfReadError as exc:
        raise RedactError(f"Could not read the PDF: {exc}")
    except Exception as exc:
        # pypdf raises a zoo of types on malformed files, and an encrypted one
        # can need a crypto library that is not installed. Either way the person
        # can still paste the text, which the message says.
        log.warning("pdf extraction failed: %s", type(exc).__name__)
        raise RedactError("Could not read this PDF. Copy its text and paste it instead.")

    text = "\n\n".join(page for page in pages if page)
    if not text.strip():
        raise RedactError(
            "This PDF has no text layer, it is probably a scan. Run OCR on it first, "
            "or paste the text."
        )
    return text[:MAX_TEXT_CHARS], len(pages)
