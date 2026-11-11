"""Whether the answer is actually supported by what it cites.

Everything else in this app works on the evidence *before* the answer exists:
which corpora to search, which results to trust, which passages reach the model.
Nothing looked at the finished text. So the one failure that matters most was
also the only one nobody checked. Not "no answer", which the run reports
honestly, but a confident sentence carrying `[3]` that source 3 does not say.
That is the failure a reader cannot catch without opening every link, which is
precisely what the citation was supposed to save them from.

Three rules keep this from becoming theatre:

* **The claims are enumerated deterministically, not by the model.** Asking a
  model which of its own sentences are worth checking invites it to check the
  safe ones. Every cited sentence goes in the list; the model only judges.
* **It judges citation fidelity, not truth.** "Does source 3 say this" is
  answerable from text already in front of the model and is worth a verdict.
  "Is this true of the world" is not, and a system that conflated the two would
  hand out a badge of correctness it has no way to earn. The UI says which one
  this is.
* **Nothing unchecked is ever counted as checked.** A claim the model skipped, a
  claim past the call ceiling or the budget, a response that arrived truncated:
  each is a gap in the check, and each is reported as one. The failure this
  guards against is the worst one a verifier has: a green line over an answer
  nobody ruled on. `unchecked` is therefore a verdict of its own, and the
  summary states how many of the answer's claims it covers.

A verdict is never allowed to fail a run: the answer is already written and on
screen, and an unchecked answer is what every other research tool ships.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from app.agent import blocks, prompts
from app.agent.context import RunContext
from app.config import settings
from app.embed.rerank import Passage
from app.llm import LLMError
from app.providers import catalog

log = logging.getLogger(__name__)

# How many claims one call rules on. Enough to cover an answer's load-bearing
# sentences without turning the check into a second writing call.
JUDGED_LIMIT = 14
# Every judgeable claim is ruled on: the batches cover the whole list, however
# many calls that takes. A fixed ceiling on calls reported a long answer as
# "14 of 38 checked", with the paragraphs it ends on never looked at, which is
# where a citation drifts furthest from its source. What bounds the check is
# `CLAIM_LIMIT` below and the time the check is granted past the preset
# (`Budget.allow_check`): the answer is on screen by then, so the extra time
# costs a later verdict, never a later answer. Whatever the clock does not reach
# is still counted and reported as unchecked.
CHECK_S = 90.0
# The ceiling on extraction itself, which exists only to bound the block's
# payload. An answer with two hundred cited sentences is pathological; one with
# thirty is an ordinary deep report, and its thirty are all enumerated.
CLAIM_LIMIT = 200
# How many findings get a second look of their own (`_second_look`).
SECOND_LOOK_LIMIT = 12
# The floor on what one verify call may answer with.
VERIFY_TOKENS = 1600
# What one verdict costs in the reply: the claim number, the verdict, a note
# and the source's words copied as evidence. A flat 1600 for fourteen of them
# cut the reply off mid-object, and the claims after it went unchecked.
PER_VERDICT_TOKENS = 220
# What one claim costs in a verify prompt: the sentence, plus its share of the
# evidence the model rules against. Crude, and only ever used to divide a small
# window into a batch size that fits in it.
PER_CLAIM_TOKENS = 300
# What the verify instructions themselves cost, measured on the built prompt.
VERIFY_RULES_TOKENS = 400
# What a cited source contributes when the run kept no ranked passage of it:
# see `_evidence_for`. Bounded, because this is the fallback for a source the
# writer's evidence set did not carry, not a second copy of the whole page.
FILL_IN_CHARS = 1200
# Below this a "sentence" is a stray fragment rather than an assertion. Kept low
# on purpose: "Der Preis liegt bei 302 USD [1]" is a claim, and a floor set high
# enough to feel safe silently stops checking the shortest, most factual
# sentences in the answer, which are the ones a wrong citation hurts most.
MIN_CLAIM_CHARS = 16
# Below this an answer is a line or two and carries no claim worth a block.
MIN_UNCITED_CHARS = 200
# A bracket that is not a citation: `[Quelle]`, `[source]`, `[implied by the
# context]`. A model that cannot see the numbered list sometimes writes the
# *shape* of a citation instead, and because nothing downstream matches the
# shape, the answer then passes for prose that happened to make no claims. A
# gemma4:e4b run filled a whole report with "[Quelle impliziert durch den
# Kontext]" and got no verdict block at all. The trailing lookahead keeps
# markdown links out of it.
_PLACEHOLDER = re.compile(r"\[(?!\d+(?:~\d+)?\])(?!\d{1,3}:)([^\]\n]{2,60})\](?!\()")

SUPPORTED = "supported"
PARTIAL = "partial"
UNSUPPORTED = "unsupported"
# Not a judgement but the absence of one. Every claim starts here and only a
# verdict the model actually returned, in a form this module recognises, moves
# it off. It is the default precisely because the alternative default was
# "supported", and that turned every gap in the check (a truncated response, a
# verdict string the model invented, a claim past the per-call limit) into a
# clean bill of health nobody issued.
UNCHECKED = "unchecked"
# What the model is allowed to say. `unchecked` is deliberately not in it: the
# model cannot decline a claim by returning an unknown word for it.
_VERDICTS = frozenset({SUPPORTED, PARTIAL, UNSUPPORTED})
_COUNTED = (SUPPORTED, PARTIAL, UNSUPPORTED, UNCHECKED)

_FENCE = re.compile(r"^\s*(```|~~~)")
# `[3]`, and `[3~0]`: a passage citation once `anchors` has taken its words out
# (literature projects, `prompts.LIBRARY_RULES`). The second group is the
# passage's position in the list `anchors` returned.
_CITATION = re.compile(r"\[(\d{1,3})(?:~(\d+))?\]")
# `[3: "the exact words on that page"]`, as the writer is asked to cite in a
# literature project, with whatever double quotation marks the model chose, and
# several passages of one page in one bracket: `[3: "…", "…"]`. Inside the marks
# anything goes, a page's own citation ("… every month [14]") and its own
# quotation marks ('we assume it to be "anomaly", and …') included: a passage
# ends only at a closing mark followed by the bracket's end or by the next
# passage. Ending at any mark would cut the passage at the first word the page
# quotes, fail the whole citation and leave it in the prose as text.
# Single quotes are not marks: they are apostrophes in half the passages worth
# citing. `web/js/render/markdown.js` parses the same shape.
_MARKS_OPEN = '"“„«»'
_MARKS_CLOSE = '"”“»«'
# The next passage is set off by a comma, a semicolon or a space: a passage
# ending in a quotation of its own, `… “walls”"]`, has a closing mark right
# before its own closing one, and read as the start of a next passage it broke
# the citation and left it in the prose as text.
_PASSAGE_END = rf"[{_MARKS_CLOSE}](?:\s*\]|\s*[,;]\s*[{_MARKS_OPEN}]|\s+[{_MARKS_OPEN}])"
_PASSAGE_TEXT = rf"(?:(?!{_PASSAGE_END})[^\n]){{3,600}}"
_ONE_PASSAGE = rf"[{_MARKS_OPEN}]{_PASSAGE_TEXT}[{_MARKS_CLOSE}]"
# `[3 quote: "…"]` is the same citation, shown to the reader as a direct
# quotation: checked exactly like the plain form, only rendered differently.
_PASSAGE_CITATION = re.compile(
    rf"\[(\d{{1,3}})(?:\s*[Qq]uote)?:\s*((?:{_ONE_PASSAGE}\s*[,;]?\s*)+)\]"
)
# Run over a citation's inside only, which `_PASSAGE_CITATION` cut off before
# its `]`, so the last passage's end is the end of the text.
_PASSAGE = re.compile(
    rf"[{_MARKS_OPEN}]((?:(?![{_MARKS_CLOSE}](?:\s*$|\s*[,;]\s*[{_MARKS_OPEN}]|\s+[{_MARKS_OPEN}]))[^\n]){{3,600}})[{_MARKS_CLOSE}]"
)


def anchors(answer: str) -> tuple[str, list[tuple[int, str]]]:
    """The answer with each passage citation reduced to a number, and the
    passages, in order.

    Taken out before anything else reads the answer: the quoted words are not
    part of the claim, and a full stop inside them would split the sentence
    they belong to in two.
    """
    found: list[tuple[int, str]] = []

    def keep(match: re.Match[str]) -> str:
        markers = []
        for text in _PASSAGE.findall(match.group(2)):
            found.append((int(match.group(1)), " ".join(text.split())))
            markers.append(f"[{match.group(1)}~{len(found) - 1}]")
        return "".join(markers)

    return _PASSAGE_CITATION.sub(keep, answer), found


# Sentence end, or a line break. Markdown answers put one claim per bullet as
# often as one per sentence, so both are boundaries.
_BOUNDARY = re.compile(r"[.!?]\s+|\n+")

# Full stops that end an abbreviation rather than a sentence. Without these, "Lt.
# Gen. Igor Kirillov" and "am 19. Dezember 1972" are three sentence ends between
# them, and the fragment that keeps the citation is the tail: a claim reading
# "Dezember 1972 nach zwölf Tagen zur Erde zurück" that nobody wrote and that the
# model is then asked to rule on. Both were observed in production answers.
#
# The bias is deliberate: under-splitting merges two sentences into one claim,
# which is checked as a whole and reads fine. Over-splitting invents a claim. So
# anything that *might* be an abbreviation is treated as one.
_ABBREVIATIONS = frozenset(
    """
    lt gen col maj capt sgt dr prof mr mrs ms st sen rep jr sr
    bzw ca ggf inkl usw etc vgl sog evtl u a d h z b ff nr abs art
    approx no fig vol ed eds pp cf ie eg al
    """.split()  # noqa: SIM905 (a word list reads better than 40 quoted strings)
)
# Trailing word characters only. Markdown sits against the abbreviation it
# emphasises (`**Lt.` and `am **19.`), and a pattern that swallowed the
# asterisks compared "**lt" against the list and split anyway.
_WORD_BEFORE = re.compile(r"([^\W_]*)$")
# A heading. It routinely carries a citation and asserts nothing on its own, the
# claim is in the prose underneath it.
_HEADING = re.compile(r"^\s*#{1,6}\s")
# A table row, and the `|---|:--|` rule that separates the header from the body.
# Rows are checked even though a line of pipes is not a sentence. Skipping them
# would be the largest hole in the check: asked for a timeline, the writer puts
# every fact in a table and cites each row, and the block would report on the
# two sentences of prose underneath it. One stored
# run answered in an eight-row table carrying ten distinct sources and was
# summarised as "All 2 cited statements supported by their sources", which is
# true of the two and reads as a statement about the answer.
#
# A row is not a sentence, but it is a claim, and `_as_sentence` is what hands
# it over as one.
_TABLE_ROW = re.compile(r"^\s*\|")
_TABLE_RULE = re.compile(r"^[\s|:-]+$")
# A line that begins something rather than continuing the line above it. Every
# other line was merely wrapped, and joining it back is what stops a claim from
# arriving as its second half.
_BLOCK_START = re.compile(r"^(\s*$|\s*(#{1,6}\s|[-*+]\s|\d{1,9}[.)]\s|>|\|))")

# Inline markdown, unwrapped rather than deleted, so the words survive.
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d{1,9}[.)])\s+|^\s*>\s?")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_CODE = re.compile(r"`([^`]+)`")
_STRONG = re.compile(r"\*\*([^*]+)\*\*")
_STRONG_ALT = re.compile(r"(?<!\w)__([^_]+)__(?!\w)")
# Underscore emphasis is left alone inside a word, because `snake_case` is not
# emphasis and mangling an identifier in a quoted claim is worse than an
# unrendered asterisk.
_EMPHASIS = re.compile(r"(?<!\w)\*([^*]+)\*(?!\w)")
_STRAY = re.compile(r"\*\*|`")
_SPACED_PUNCTUATION = re.compile(r"\s+([.,;:!?])")


@dataclass(slots=True)
class Claim:
    """One cited sentence of the answer, and the sources it leans on."""

    text: str
    sources: list[int]
    verdict: str = UNCHECKED
    note: str = ""
    # Numbers the answer cited that the run never registered. Judged here rather
    # than by the model: a citation to a source that does not exist is not a
    # matter of opinion.
    unknown: list[int] = field(default_factory=list)
    # "verbatim" or "altered" when the sentence quotes its source, "" otherwise.
    # Decided by string comparison, never by the model, see `check_quotes`.
    quote: str = ""
    # The sentence cited nothing itself and is checked against the sources of
    # the next sentence in its paragraph, see `paragraph_claims`.
    inherited: bool = False
    # What the model offered as the supporting words, before it is checked.
    evidence: str = ""
    # The passages the sentence itself cites, (source, exact words), and
    # whether every one of them is on its page word for word: "exact" or
    # "missing", "" when it cites none. See `check_passages`.
    passages: list[tuple[int, str]] = field(default_factory=list)
    passage: str = ""
    # Figures in the sentence that none of its cited sources contains, decided
    # by comparison, never by the model; see `check_numbers`.
    figures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "text": self.text,
            "sources": list(self.sources),
            "verdict": self.verdict,
            "note": self.note,
        }
        if self.quote:
            data["quote"] = self.quote
        if self.inherited:
            data["inherited"] = True
        if self.figures:
            data["figures"] = list(self.figures)
        if self.passage:
            data["passage"] = self.passage
            data["passages"] = [{"source": n, "text": text} for n, text in self.passages]
        return data


class Verdict(BaseModel):
    claim: int = 0
    verdict: str = ""
    note: str = ""
    # The words in the source that carry the claim, copied. Kept only once it
    # is found in the source (`attach_evidence`), and then shown highlighted.
    evidence: str = ""


class Support(BaseModel):
    verdicts: list[Verdict] = Field(default_factory=list)


class Part(BaseModel):
    text: str = ""
    source: int = 0
    quote: str = ""


class Breakdown(BaseModel):
    parts: list[Part] = Field(default_factory=list)


def _prose(answer: str) -> str:
    """The answer without fenced code. A `[3]` inside a code sample is a list
    index, not a citation, and checking it produces a confident nonsense verdict."""
    kept, fenced = [], False
    for line in answer.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            kept.append(line)
    return "\n".join(kept)


def _unwrap(text: str) -> str:
    """Undo markdown's soft wrapping, so a claim is a sentence and not a line.

    Prose arrives hard-wrapped, and splitting on newlines cuts a sentence
    wherever the writer's margin happened to fall. The fragment that keeps the
    citation is then the *second half* of the claim ("selbstgehostete Instanz
    von einer IP aus scrapt [2]"), and the model is asked to rule on a sentence
    nobody wrote. Lines that start a block of their own are left alone; every
    other line is joined to the one above it.
    """
    joined: list[str] = []
    for line in text.splitlines():
        if joined and joined[-1].strip() and not _BLOCK_START.match(line):
            joined[-1] = f"{joined[-1].rstrip()} {line.strip()}"
        else:
            joined.append(line)
    return "\n".join(joined)


def _ends_a_sentence(text: str, index: int) -> bool:
    """Whether the full stop at `index` closes a sentence or an abbreviation.

    Three things are never sentence ends: a known abbreviation, a single letter
    (an initial: "J. R. R. Tolkien"), and a number ("am 19. Dezember", "17.
    Dezember 2024", "Kapitel 3."). The last one costs the occasional genuine
    boundary, where a sentence really did end in a figure and the next began
    with one. That trade is worth taking every time: the loss is two sentences
    checked as one claim, and the alternative is a mutilated one.
    """
    if text[index] != ".":
        return True
    word = _WORD_BEFORE.search(text[:index]).group(1)
    if not word:
        return True
    return not (
        word.lower() in _ABBREVIATIONS or word.isdigit() or (len(word) == 1 and word.isalpha())
    )


def _fragments(text: str) -> list[str]:
    """The answer split into sentences and list items, abbreviations intact."""
    parts: list[str] = []
    start = 0
    for match in _BOUNDARY.finditer(text):
        if "\n" not in match.group() and not _ends_a_sentence(text, match.start()):
            continue
        parts.append(text[start : match.start() + 1])
        start = match.end()
    parts.append(text[start:])
    return parts


def _as_sentence(fragment: str) -> str:
    """A table row handed over as something a model can rule on.

    `| Oktober 2025 | Vergleich vor der Verhandlung [6][10] |` becomes
    "Oktober 2025: Vergleich vor der Verhandlung", which is a claim, and a
    perfectly checkable one. Anything that is not a row is returned untouched.

    The header row usually carries no citation and drops out on its own; the
    `|---|---|` rule is not a claim in any reading and returns empty, which the
    length floor then discards.
    """
    if not _TABLE_ROW.match(fragment):
        return fragment
    if _TABLE_RULE.match(fragment.strip()):
        return ""
    cells = [cell.strip() for cell in fragment.strip().strip("|").split("|")]
    kept = [cell for cell in cells if cell]
    # The first cell is the row's subject, a date, a name, a version. Colon
    # rather than a comma so it reads as the label it is.
    return f"{kept[0]}: {', '.join(kept[1:])}" if len(kept) > 1 else "".join(kept)


def _plain(text: str) -> str:
    """The claim as a reader sees it, not as markdown wrote it.

    Two reasons this is not cosmetic. The block renders text, never markup, so a
    surviving `**` shows up as literal asterisks in the one place that is meant
    to be quotable. And the model judging the claim is better off reading the
    sentence than reading its formatting: the `[2]` in particular, which
    `prompts.verify` states separately and far more clearly than an inline
    marker the model has to parse out of the prose.
    """
    text = _LIST_MARKER.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _CITATION.sub("", text)
    for pattern in (_CODE, _STRONG, _STRONG_ALT, _EMPHASIS):
        text = pattern.sub(r"\1", text)
    # A marker whose partner ended up in the neighbouring sentence has nothing to
    # unwrap and would otherwise be shown as literal asterisks.
    text = _STRAY.sub("", text)
    # Removing `[1]` leaves the space that was in front of it stranded before the
    # full stop.
    text = _SPACED_PUNCTUATION.sub(r"\1", text)
    return " ".join(text.split()).strip(" ,;")


def claims_of(
    answer: str,
    known: set[int],
    *,
    limit: int = CLAIM_LIMIT,
    passages: list[tuple[int, str]] | None = None,
) -> list[Claim]:
    """Every cited sentence and table row of the answer, in order.

    Deliberately mechanical. The model is not consulted about what counts as a
    claim, because the sentence it would leave off the list is the one worth
    checking, and the same goes for whole *forms* of answer: an app that asks
    its writer for tables and then checks only prose has excused the shape it
    recommends from the check it advertises.
    """
    claims: list[Claim] = []
    for fragment in _fragments(_unwrap(_prose(answer))):
        found = _CITATION.findall(fragment)
        cited = [int(number) for number, _ in found]
        if not cited or _HEADING.match(fragment):
            continue
        fragment = _as_sentence(fragment)
        # The numbers come off the raw fragment and the prose is cleaned after,
        # so stripping the markers cannot cost the claim its sources.
        text = _plain(fragment)
        if len(text) < MIN_CLAIM_CHARS:
            continue
        numbers = sorted(set(cited))
        claims.append(
            Claim(
                text=text,
                sources=numbers,
                unknown=[number for number in numbers if number not in known],
                passages=_passages_of(found, passages),
            )
        )
        if len(claims) >= limit:
            break
    return claims


# How many uncited sentences one block lists. Past this the answer has ignored
# its sources wholesale, and the count says so better than the list would.
UNCITED_LIMIT = 30


def _passages_of(
    found: list[tuple[str, str]], passages: list[tuple[int, str]] | None
) -> list[tuple[int, str]]:
    """The passages a fragment's `[n~k]` markers stand for."""
    if not passages:
        return []
    return [passages[int(k)] for _, k in found if k and int(k) < len(passages)]


def paragraph_claims(
    answer: str,
    known: set[int],
    *,
    limit: int = CLAIM_LIMIT,
    passages: list[tuple[int, str]] | None = None,
) -> tuple[list[Claim], list[str], list[str]]:
    """A literature project's claims: every sentence, not only cited ones, and
    the sentences that could not be tied to any source.

    There every sentence is meant to come from the papers, so a sentence with
    no number is not connective prose to skip. Writers commonly put the number
    once, at the end of the paragraph it supports; such a sentence takes the
    sources of the next cited sentence in its paragraph, and is checked against
    them like any other claim, so a borrowed number earns nothing it is not
    checked for. What is left, a sentence with no cited sentence after it in its
    paragraph, is returned on its own: a framing line, a statement that the
    papers do not cover something, or the model's own knowledge. Only the
    reader can tell those apart, so every one is shown. A line ending in a
    colon introduces what follows and is left out, as are headings.

    Returns the claims, those untied sentences, and the untied sentences the
    writer set apart in a blockquote as its own reasoning, which are listed
    as that rather than as a failure.
    """
    claims: list[Claim] = []
    uncited: list[str] = []
    own: list[str] = []
    for paragraph in _unwrap(_prose(answer)).split("\n"):
        # The writer's own reasoning, set apart as asked (`prompts.LIBRARY_RULES`):
        # its uncited sentences are not a failure to cite, and go to `own`.
        reasoning = paragraph.lstrip().startswith(">")
        pending: list[str] = []
        for fragment in _fragments(paragraph):
            if _HEADING.match(fragment):
                continue
            found = _CITATION.findall(fragment)
            cited = sorted({int(number) for number, _ in found})
            text = _plain(_as_sentence(fragment))
            if len(text) < MIN_CLAIM_CHARS:
                continue
            if not cited:
                if not text.endswith(":"):
                    pending.append(text)
                continue
            unknown = [number for number in cited if number not in known]
            claims += [
                Claim(text=earlier, sources=cited, unknown=unknown, inherited=True)
                for earlier in pending
            ]
            pending = []
            claims.append(
                Claim(
                    text=text,
                    sources=cited,
                    unknown=unknown,
                    passages=_passages_of(found, passages),
                )
            )
        if reasoning:
            own += pending
        else:
            uncited += pending
    return claims[:limit], uncited[:UNCITED_LIMIT], own[:UNCITED_LIMIT]


def _judgeable(claims: list[Claim]) -> list[Claim]:
    """Claims a model can rule on: the ones citing at least one real source."""
    return [claim for claim in claims if any(n not in claim.unknown for n in claim.sources)]


def _apply(claims: list[Claim], support: Support) -> None:
    """Fold the model's verdicts onto the claims it was given.

    A claim the model skipped, and a claim it answered with a word that is not
    one of the three verdicts, both keep their `unchecked` default. Neither is
    evidence against the answer. Inventing a finding out of a truncated
    response would make the block untrustworthy in the direction that matters,
    but neither is evidence *for* it either, and the earlier default of
    "supported" quietly asserted exactly that.
    """
    by_index = {verdict.claim: verdict for verdict in support.verdicts}
    for position, claim in enumerate(claims, start=1):
        verdict = by_index.get(position)
        if verdict is None:
            continue
        value = verdict.verdict.strip().lower()
        if value not in _VERDICTS:
            log.info("ignoring an unknown verdict %r on claim %d", verdict.verdict, position)
            continue
        claim.verdict = value
        claim.note = " ".join(verdict.note.split())[:200] if value != SUPPORTED else ""
        claim.evidence = " ".join(verdict.evidence.split())[:400]


def _finalise(claims: list[Claim]) -> None:
    """The verdicts this module decides on its own, after the model has spoken."""
    for claim in claims:
        if claim.unknown:
            claim.verdict = UNSUPPORTED
            missing = ", ".join(f"[{number}]" for number in claim.unknown)
            claim.note = f"Cites {missing}, which this run never retrieved."


# ------------------------------------------------------------------ quotations
#
# A quotation is the one claim a machine can check exactly. "The source says
# roughly this" needs a judge, and the judge is a model that can be wrong; "the
# source contains these words in this order" needs a string search. So a
# quoted sentence gets both: the model's verdict on what it claims, and a
# comparison of the quoted words against the full text of the page it cites.
#
# Only against text the run actually read. A search snippet is two lines an
# engine chose, and a quote missing from it says nothing about the page.

QUOTE_MIN_CHARS = 20
QUOTED = "verbatim"
ALTERED = "altered"
# Opening and closing marks in the languages answers are written in: “…”, „…“,
# "…", «…», »…«. Deliberately loose about which closes which: a model that opens
# with „ and closes with ” still quoted something.
_QUOTATION = re.compile(r"[“„\"«»]([^“”„\"«»]{" + str(QUOTE_MIN_CHARS) + r",600})[”“\"»«]")


def _comparable(text: str) -> str:
    """Text reduced to what a quotation has to preserve: the words, in order.

    Case, punctuation, ligatures, accents' encoding and whitespace all differ
    between a PDF's extracted text and a model's rendering of it without the
    quotation being any less faithful. A changed, added or dropped word does
    not survive this.
    """
    folded = unicodedata.normalize("NFKD", text).casefold()
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return " ".join(re.findall(r"\w+", folded))


def quotations(text: str) -> list[str]:
    return [m.group(1).strip() for m in _QUOTATION.finditer(text)]


def check_quotes(claims: list[Claim], registered: dict[int, object]) -> None:
    """Compare every quotation against the full text of the sources its
    sentence cites, and mark the claim.

    Found in one of them: `verbatim`. Found in none, with at least one of them
    read in full: `altered`, and the claim can no longer be `supported`,
    whatever the model ruled. A sentence that puts words in quotation marks
    asserts that the source says exactly those words, and that assertion is
    false. It stays `partial` rather than `unsupported` because the usual
    cause is a translation or a tidied quote, not an invention, and the note
    says which it has to be checked for.
    """
    for claim in claims:
        quoted = [q for q in quotations(claim.text) if len(_comparable(q)) >= QUOTE_MIN_CHARS]
        if not quoted:
            continue
        texts = [
            _comparable(getattr(registered.get(n), "content", "") or "")
            for n in claim.sources
            if n not in claim.unknown
        ]
        texts = [t for t in texts if t]
        if not texts:
            continue
        if all(any(_comparable(q) in t for t in texts) for q in quoted):
            claim.quote = QUOTED
            continue
        claim.quote = ALTERED
        if claim.verdict in (SUPPORTED, UNCHECKED, PARTIAL):
            claim.verdict = PARTIAL
            claim.note = (
                "The quoted words are not in the cited source word for word. Check it "
                "against the page: it may be translated or paraphrased inside quotation marks."
            )


# ------------------------------------------------------------------- figures
#
# A wrong number is the hallucination a reader is least able to catch and most
# likely to repeat: "rose by 23 %" reads exactly like "rose by 32 %", and a
# judge model skimming for gist waves either through. Whether the cited page
# contains the figure at all needs no model. So every figure in a claim is
# looked for in the full text of the sources it cites, and one that is in none
# of them takes the claim off `supported`.
#
# Written to miss rather than to accuse. A figure is only looked for when it
# can be found reliably (not a small integer a page may spell out, not part of
# a name like COVID-19 or GPT-4), both decimal conventions are tried (1,5 and
# 1.5; 1.500 and 1,500), a claim may round what the page says (33 % for 33.3 %),
# and nothing is decided unless every cited source was read in full: a figure
# missing from a two-line snippet says nothing about the page.

# A run of digits with the separators numbers are written with: 1,234.5,
# 1.234,5, 1 234 (a thin or no-break space), and a date's 17.12.2024.
_NUMBER = re.compile(r"(?<![\w.,])\d+(?:[.,\u202f\u00a0\u2009]\d+)*(?![\w])")
# Directly after a letter, or a letter and a hyphen: part of a name, a model,
# a standard (GPT-4, COVID-19, H2O, ISO-9001), never a quantity to look up.
_NAMED = re.compile(r"[^\W\d_]-?$")
_SCALES = {
    "thousand": 1e3, "tausend": 1e3, "tsd": 1e3,
    "million": 1e6, "millionen": 1e6, "mio": 1e6, "millions": 1e6,
    "billion": 1e9, "billions": 1e9, "milliarde": 1e9, "milliarden": 1e9, "mrd": 1e9,
}  # fmt: skip
_SCALE_AFTER = re.compile(r"\s*([^\W\d_]+)")
# Below this an integer is too often written as a word ("drei Studien", "two
# thirds") to be looked for as digits.
SMALL_FIGURE = 13


def _readings(token: str) -> set[float]:
    """Every value a written number can mean.

    One separator followed by exactly three digits is ambiguous, 1,500 and
    1.500 are a thousand and a half in one convention and one and a half in
    the other, so both are kept; anything else has one reading per convention
    that parses.
    """
    plain = re.sub("[\u202f\u00a0\u2009]", "", token)
    values: set[float] = set()
    for thousands, decimal in ((",", "."), (".", ",")):
        groups = plain.split(thousands)
        if len(groups) > 1 and not all(len(g) == 3 for g in groups[1:]):
            continue
        if len(groups) > 1 and decimal in groups[-1]:
            head, _, _rest = groups[-1].partition(decimal)
            if len(head) != 3:
                continue
        candidate = plain.replace(thousands, "").replace(decimal, ".")
        try:
            values.add(float(candidate))
        except ValueError:
            continue
    return values


def _decimals(token: str) -> int:
    """How many decimals a claim's figure is written with, for rounding."""
    match = re.search(r"[.,](\d{1,2})$", token)
    return len(match.group(1)) if match and not re.search(r"[.,]\d{3}$", token) else 0


def _scale(text: str, end: int) -> float:
    match = _SCALE_AFTER.match(text, end)
    word = match.group(1).lower().rstrip(".") if match else ""
    return _SCALES.get(word, 1.0)


def figures_in(text: str) -> list[tuple[str, set[float], int]]:
    """The figures of a claim worth looking up: the token, what it can mean
    (scaled by a following "million" and the like), its decimals."""
    found = []
    for match in _NUMBER.finditer(text):
        token = match.group()
        if _NAMED.search(text[: match.start()]):
            continue
        parts = re.split(r"[.,]", token)
        # A date or a version, 17.12.2024 or 3.12.1: each part on its own.
        if len(parts) > 2 and not all(len(p) == 3 for p in parts[1:]):
            for part in parts:
                if len(part) == 4:
                    found.append((part, {float(part)}, 0))
            continue
        values = _readings(token)
        if not values:
            continue
        integer = all(v == int(v) for v in values) and not re.search(r"[.,]\d{1,2}$", token)
        if (
            integer
            and max(values) < SMALL_FIGURE
            and not text[match.end() :].lstrip().startswith("%")
        ):
            continue
        scale = _scale(text, match.end())
        found.append((token, {v * scale for v in values} | values, _decimals(token)))
    return found


def _page_values(content: str) -> tuple[set[float], list[float]]:
    """Every value a page writes, exactly, and the same as a list for rounding;
    each part of a date or a version counted on its own as well."""
    exact: set[float] = set()
    for match in _NUMBER.finditer(content):
        token = match.group()
        values = _readings(token)
        scale = _scale(content, match.end())
        exact |= values | {v * scale for v in values}
        for part in re.split(r"[.,\u202f\u00a0\u2009]", token):
            if part.isdigit():
                exact.add(float(part))
    return exact, sorted(exact)


def _on_page(values: set[float], decimals: int, page: tuple[set[float], list[float]]) -> bool:
    exact, ordered = page
    if values & exact:
        return True
    # A claim may round what the page says: 33 % for 33.3 %, 1.5 for 1.47.
    step = 10.0**-decimals
    return any(
        abs(round(p, decimals) - v) < step / 10 and p != round(p, decimals)
        for v in values
        for p in ordered
        if abs(p - v) < step
    )


def check_numbers(claims: list[Claim], registered: dict[int, object]) -> None:
    """Look for every figure of each claim in the full text of its sources.

    Decided only when every source the claim cites was read in full: a source
    known only by its snippet may well hold the figure. A figure found in none
    of them is listed on the claim, and the claim can no longer be `supported`.
    It stays `partial` rather than `unsupported`, since the page may write the
    figure as a word or in another unit, and the note says to check it.
    """
    for claim in claims:
        figures = figures_in(claim.text)
        if not figures:
            continue
        cited = [registered.get(n) for n in claim.sources if n not in claim.unknown]
        contents = [getattr(source, "content", "") or "" for source in cited]
        if not contents or not all(contents):
            continue
        pages = [_page_values(content) for content in contents]
        missing = [
            token
            for token, values, decimals in figures
            if not any(_on_page(values, decimals, page) for page in pages)
        ]
        if not missing:
            continue
        claim.figures = list(dict.fromkeys(missing))
        if claim.verdict in (SUPPORTED, PARTIAL):
            claim.verdict = PARTIAL
            listed = ", ".join(claim.figures)
            claim.note = (
                f"{listed} {'is' if len(claim.figures) == 1 else 'are'} not in the cited "
                "source. Check the figure against the page: it may be written as a word or "
                "in another unit, or it may be wrong."
            )


EXACT = "exact"
MISSING = "missing"


def check_passages(claims: list[Claim], registered: dict[int, object]) -> None:
    """Compare every cited passage with the page it names, word for word.

    The passage is the writer's own pointer to where a claim stands, and it is
    what the reader opens and sees highlighted, so it has to be on that page.
    One that is not, a tidied quotation, words from a neighbouring page, an
    invention, takes the claim off `supported`: the citation says "it is here"
    and it is not. Only against pages read in full, which a library page always
    is.
    """
    for claim in claims:
        checked = [
            (n, text)
            for n, text in claim.passages
            if getattr(registered.get(n), "content", "")
            and len(_comparable(text)) >= QUOTE_MIN_CHARS // 2
        ]
        if not checked:
            continue
        missing = [
            (n, text)
            for n, text in checked
            if _comparable(text) not in _comparable(registered[n].content)  # type: ignore[attr-defined]
        ]
        claim.passage = MISSING if missing else EXACT
        for n, text in checked:
            source = registered[n]
            found = (n, text) not in missing and hasattr(source, "evidence_texts")
            if found and text not in source.evidence_texts:
                source.evidence_texts.append(text)
        if missing and claim.verdict in (SUPPORTED, UNCHECKED, PARTIAL):
            claim.verdict = PARTIAL
            where = ", ".join(f"[{n}]" for n, _ in missing)
            claim.note = (
                f"The cited passage is not on {where} word for word, so the place this "
                "claim points to does not exist as quoted."
            )


def attach_evidence(claims: list[Claim], registered: dict[int, object]) -> bool:
    """Hand each source the words of it that carry the answer's claims, for
    the reader to see highlighted where they stand. Returns whether any source
    got some.

    Two kinds, both checked by string comparison, never taken on trust: a
    quotation found verbatim, and the excerpt the judge copied out as its
    evidence, kept only if it is really in the source it is attributed to. A
    model asked to copy words sometimes tidies them, and a highlight of words
    the page does not contain would point the reader at nothing.
    """
    changed = False
    for claim in claims:
        if claim.verdict not in (SUPPORTED, PARTIAL):
            continue
        excerpts = [q for q in quotations(claim.text) if claim.quote == QUOTED]
        if claim.evidence:
            excerpts.append(claim.evidence)
        for excerpt in excerpts:
            if len(_comparable(excerpt)) < QUOTE_MIN_CHARS:
                continue
            for number in claim.sources:
                source = registered.get(number)
                if source is None or not hasattr(source, "evidence_texts"):
                    continue
                found = page_words(excerpt, getattr(source, "content", "") or "")
                if found:
                    if found not in source.evidence_texts:
                        source.evidence_texts.append(found)
                        changed = True
                    break
            else:
                log.info("evidence not on any cited page, dropped: %r", excerpt[:120])
    return changed


# How much of an excerpt a page sentence must share, word for word, to stand in
# for it. High, because the sentence is shown as the evidence.
EVIDENCE_OVERLAP = 0.6
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def page_words(excerpt: str, content: str) -> str:
    """The words of the page that an excerpt stands for, or "".

    The excerpt itself when the page contains it. Otherwise the page's own
    sentence that shares the most of its words, if that is most of them: a
    model asked to copy routinely tidies a symbol or drops an article, and
    throwing the excerpt away for that loses the one pointer to where the claim
    stands. What is returned is always the page's text, never the model's, so
    a highlight can only ever mark something the page says.
    """
    wanted = _comparable(excerpt)
    page = _comparable(content)
    if not wanted or not page:
        return ""
    if wanted in page:
        return excerpt
    words = set(wanted.split())
    best, score = "", 0.0
    for sentence in _SENTENCE_END.split(" ".join(content.split())):
        if len(sentence) > 600:
            continue
        shared = len(words & set(_comparable(sentence).split())) / len(words)
        if shared > score:
            best, score = sentence, shared
    return best if score >= EVIDENCE_OVERLAP else ""


def _pending(status: str, total: int) -> dict[str, object]:
    """What the block carries when there is no verdict in it: while the check
    runs, and when it could not run at all. Every count the renderer reads is
    present, and all of them say the same thing: nothing has been checked yet."""
    return {
        "status": status,
        "total": total,
        "checked": 0,
        "supported": 0,
        "partial": 0,
        "unsupported": 0,
        "unchecked": total,
        "claims": [],
    }


def summary_of(claims: list[Claim]) -> dict[str, object]:
    """The counts the block's one line is written from.

    `total` and `checked` are separate numbers. They were the same
    number when the claim list *was* the checked list, and that is precisely how
    an answer with thirty-eight cited sentences came to be summarised as "all 14
    cited statements supported": true of the fourteen, and read by everyone as
    a statement about the answer.
    """
    counts: dict[str, int] = dict.fromkeys(_COUNTED, 0)
    for claim in claims:
        counts[claim.verdict] += 1
    return {
        "passages": {
            EXACT: sum(1 for claim in claims if claim.passage == EXACT),
            MISSING: sum(1 for claim in claims if claim.passage == MISSING),
        },
        "quotes": {
            QUOTED: sum(1 for claim in claims if claim.quote == QUOTED),
            ALTERED: sum(1 for claim in claims if claim.quote == ALTERED),
        },
        "figures": sum(1 for claim in claims if claim.figures),
        "total": len(claims),
        "checked": len(claims) - counts[UNCHECKED],
        "supported": counts[SUPPORTED],
        "partial": counts[PARTIAL],
        "unsupported": counts[UNSUPPORTED],
        "unchecked": counts[UNCHECKED],
        "claims": [claim.as_dict() for claim in claims],
    }


def _reply_tokens(claims: int) -> int:
    """What a reply ruling on this many claims may cost."""
    return max(VERIFY_TOKENS, claims * PER_VERDICT_TOKENS)


def _plan_for_window(ctx: RunContext, window: int | None = None) -> tuple[int, int, int]:
    """How many claims one call may carry, what the answer may cost, and how
    many characters of evidence fit beside them.

    The writer's prompt is fitted to a local runtime's window (see
    `orchestrator._fit_to_window`); without the same treatment here the check
    inherits exactly the bug the writer was cured of. It showed as the worst
    possible verdict block: a local run came back "0 of 4 checked", because
    fourteen claims and their evidence were truncated to 4096 tokens and the
    model had nothing left to rule on.

    A remote model keeps the ordinary batch size: its window is far larger than
    anything assembled here. Either way the reply is given room for every
    verdict in the batch, since a reply cut off mid-object rules on nothing.
    """
    if window is None:
        window = settings.local_context_tokens if ctx.checker().is_local else 0
    if window <= 0:
        return JUDGED_LIMIT, _reply_tokens(JUDGED_LIMIT), 0

    # The verdicts share the window with the claims they judge.
    response = max(200, min(_reply_tokens(JUDGED_LIMIT), window // 4))
    room = window - response - VERIFY_RULES_TOKENS
    per_call = min(JUDGED_LIMIT, room // PER_CLAIM_TOKENS, response // PER_VERDICT_TOKENS)
    per_call = max(2, per_call)
    # Half the remaining room for evidence, half for the claims themselves.
    evidence_chars = max(1000, (room - per_call * 60) // 2 * 4)
    return per_call, response, evidence_chars


async def _report_uncited(ctx: RunContext, answer: str) -> dict[str, object] | None:
    """An answer that cites nothing, said out loud.

    Returning None would emit no block, and no block is exactly what a clean
    check looks like. The one answer that ignored the citation contract outright
    would be the only one the reader got no verdict on.
    """
    text = _plain(_unwrap(_prose(answer)))
    if len(text) < MIN_UNCITED_CHARS:
        return None

    placeholders = [m.group(1) for m in _PLACEHOLDER.finditer(answer)]
    data: dict[str, object] = {
        **_pending(blocks.STATUS_UNCITED, 0),
        # The words the model used instead of a number, so the reader can see
        # what happened rather than being told the answer is untrustworthy.
        "placeholders": sorted(set(placeholders))[:3],
    }
    await ctx.stream.emit_block(blocks.verification_block(data))
    return data


def _batches(claims: list[Claim], *, per_call: int = JUDGED_LIMIT) -> list[list[Claim]]:
    """The judgeable claims, all of them, cut into the groups one call each
    rules on."""
    size = max(1, per_call)
    return [claims[index : index + size] for index in range(0, len(claims), size)]


def _evidence_for(
    claims: list[Claim], passages: list[Passage], registered: dict[int, object]
) -> list[Passage]:
    """The text the model rules against: the cited sources, and only those.

    Two things are intended. Sources the answer did *not* cite are left out:
    a model shown the whole evidence pile can find a claim "supported" by a
    source the answer never named, which is the opposite of the question being
    asked. And a cited source that reached the answer without leaving a ranked
    passage behind is filled in from what the run actually retrieved for it,
    rather than being treated as absent.

    That second case is not hypothetical. The deep path writes its report from
    the passages *and* from per-sub-question notes, and re-ranks against the
    main question before writing, so a source that carried a sub-question can
    hold a citation while owning no passage in the final set. Judged against an
    empty evidence list, every sentence resting on it comes back unsupported.
    """
    cited = {number for claim in claims for number in claim.sources if number not in claim.unknown}
    evidence = [passage for passage in passages if passage.source_index in cited]

    # The passage a claim cites by its words, from the page it names. The ranked
    # set holds a few passages of each page, not necessarily that one: a writer
    # who quotes the page's fourth paragraph, which it saw in a neighbouring
    # chunk, was otherwise judged against the first and ruled unsupported for
    # citing exactly what it was asked to. Words that are not on the page find
    # nothing here, and `check_passages` says so separately.
    shown = {_comparable(passage.text) for passage in evidence}
    for claim in claims:
        for number, words in claim.passages:
            source = registered.get(number)
            wanted = _comparable(words)
            if source is None or not wanted:
                continue
            for excerpt in getattr(source, "excerpts", None) or []:
                key = _comparable(excerpt)
                if wanted in key and key not in shown:
                    shown.add(key)
                    evidence.append(_passage_of(source, number, excerpt, read=True))
                    break

    for index in sorted(cited - {passage.source_index for passage in evidence}):
        source = registered.get(index)
        text = (getattr(source, "evidence", "") or "").strip()
        if not text:
            continue
        evidence.append(_passage_of(source, index, text[:FILL_IN_CHARS], read=bool(source.content)))
    return evidence


def _passage_of(source: object, index: int, text: str, *, read: bool) -> Passage:
    """Text of a registered source as a passage the judge can be shown."""
    return Passage(
        text=text,
        source_index=index,
        title=source.title,
        url=source.url,
        domain=source.domain,
        tier=source.tier,
        tier_label=source.tier_label,
        published=source.published,
        read=read,
    )


async def _second_look(
    ctx: RunContext,
    question: str,
    claims: list[Claim],
    passages: list[Passage],
    registered: dict[int, object],
    plan: tuple[int, int],
) -> None:
    """Look again at every claim ruled against, one claim per call, and this
    time make the judge find the support instead of the flaw.

    Asked "what is missing", a judge finds something: a "strictly" the source
    does not use, "a model" where the source said "an autoencoder", a part its
    other cited source states. Those findings survived a prompt that forbade
    them. So the second look asks the other question: split the sentence into
    its statements, and copy, for each, the source's words that state it. The
    verdict is then counted rather than chosen (`_rule_parts`).
    """
    flagged = [claim for claim in claims if claim.verdict in (PARTIAL, UNSUPPORTED)]
    for claim in flagged[:SECOND_LOOK_LIMIT]:
        if not ctx.budget.can_verify():
            log.info("second look stopped early: %s", ctx.budget.summary())
            return
        reply_tokens, evidence_chars = plan
        evidence = _evidence_for([claim], passages, registered)
        if evidence_chars:
            evidence, _dropped = prompts.fit_passages(evidence, max_chars=evidence_chars)
        try:
            ctx.budget.spend_llm()
            breakdown = await ctx.checker().structured(
                prompts.verify_parts(question, claim, prompts.format_evidence(evidence)),
                Breakdown,
                max_tokens=max(VERIFY_TOKENS, reply_tokens // 2),
            )
        except LLMError as exc:
            log.info("second look failed: %s", exc)
            continue
        _rule_parts(claim, breakdown, evidence, registered)


# How much of a claim's own words its parts must repeat before a breakdown is
# taken as covering the claim. Below it the judge left something out, most
# likely the part it could not find, and the first ruling stands.
PARTS_COVERAGE = 0.7


def _content_words(text: str) -> set[str]:
    return {word for word in _comparable(text).split() if len(word) > 3 and not word.isdigit()}


def _rule_parts(
    claim: Claim, breakdown: Breakdown, evidence: list[Passage], registered: dict[int, object]
) -> None:
    """Turn a breakdown into a verdict, deterministically.

    A part counts as stated only when its quote is found in what the claim
    cites, the passages the judge was shown or the page itself (`page_words`),
    so a quote the model made up carries nothing. Every part stated is
    supported; some, partial, with the note naming the parts that are not.
    A breakdown that does not cover the claim, or states none of it, leaves
    the first ruling as it was: the second look may clear a finding, but it
    is not a new way of making one.
    """
    parts = [part for part in breakdown.parts if part.text.strip()]
    wanted = _content_words(claim.text)
    said = set().union(*(_content_words(part.text) for part in parts)) if parts else set()
    if not parts or (wanted and len(wanted & said) / len(wanted) < PARTS_COVERAGE):
        log.info("second look did not cover the claim; first ruling stands")
        return

    cited = set(claim.sources) - set(claim.unknown)
    texts = [passage.text for passage in evidence if passage.source_index in cited]
    texts += [getattr(registered.get(number), "content", "") or "" for number in sorted(cited)]
    stated: list[tuple[Part, str]] = []
    missing: list[Part] = []
    for part in parts:
        found = ""
        if len(_comparable(part.quote)) >= QUOTE_MIN_CHARS // 2:
            found = next((hit for t in texts if (hit := page_words(part.quote, t))), "")
        if found:
            stated.append((part, found))
        else:
            missing.append(part)

    if not stated:
        return
    if not missing:
        claim.verdict, claim.note = SUPPORTED, ""
        claim.evidence = " ".join(stated[0][1].split())[:400]
        return
    claim.verdict = PARTIAL
    gaps = "; ".join(f'"{" ".join(part.text.split())}"' for part in missing)
    claim.note = f"Not in the cited sources: {gaps}"[:200]
    claim.evidence = " ".join(stated[0][1].split())[:400]


async def _rule(
    ctx: RunContext,
    question: str,
    batch: list[Claim],
    passages: list[Passage],
    registered: dict[int, object],
    plan: tuple[int, int],
) -> bool:
    """Have the judge rule on one batch. False when some of it went unruled.

    A failed call is not the end of the check. The usual failure is a reply
    cut off mid-object, and the batch's claims were then left unchecked along
    with every batch after it: "14 of 38 checked". So the batch is halved and
    each half asked again, down to a single claim, and the next batch is asked
    regardless. What still fails stays `unchecked` and is counted as such.
    """
    # Asked per call, not once for the lot: the check runs after the answer is
    # on screen, so stopping costs the reader verdicts they can see are
    # missing, while overrunning the budget costs them the run's own guarantees.
    if not ctx.budget.can_verify():
        log.info("citation check stopped early: %s", ctx.budget.summary())
        return False

    reply_tokens, evidence_chars = plan
    evidence = _evidence_for(batch, passages, registered)
    if evidence_chars:
        # Same cut as the writer's, at a passage boundary and by rank, rather
        # than wherever the runtime's window happens to end.
        evidence, _dropped = prompts.fit_passages(evidence, max_chars=evidence_chars)
    try:
        ctx.budget.spend_llm()
        support = await ctx.checker().structured(
            prompts.verify(question, batch, prompts.format_evidence(evidence)),
            Support,
            max_tokens=reply_tokens,
        )
    except LLMError as exc:
        log.info("citation check of %d claims failed: %s", len(batch), exc)
        if len(batch) == 1:
            return False
        half = len(batch) // 2
        first = await _rule(ctx, question, batch[:half], passages, registered, plan)
        second = await _rule(ctx, question, batch[half:], passages, registered, plan)
        return first and second
    _apply(batch, support)
    return True


async def check(
    ctx: RunContext, question: str, answer: str, passages: list[Passage]
) -> dict[str, object] | None:
    """Judge the answer's citations and emit the verdict block.

    Returns the block data, or None when there was nothing to check: an answer
    with no citations, no passages behind it, or no budget left to ask. Never
    raises: the answer is already on the reader's screen.
    """
    if not passages or not answer.strip():
        return None

    # Every source the run registered, not only those holding a ranked passage.
    # A number the answer cited that is in the sidebar was retrieved. It simply
    # did not survive the last ranking, and telling the reader it "was never
    # retrieved" about a source they can click is both false and the most
    # damning thing this block knows how to say.
    registered: dict[int, object] = dict(ctx.sources.all())
    known = {passage.source_index for passage in passages} | set(registered)
    original = answer
    answer, cited_passages = anchors(answer)
    # In a literature project every sentence is meant to come from the papers,
    # so every sentence is a claim, and the ones tied to no source are listed.
    own: list[str] = []
    if getattr(ctx, "library", None) is not None:
        claims, uncited, own = paragraph_claims(answer, known, passages=cited_passages)
    else:
        claims, uncited = claims_of(answer, known, passages=cited_passages), []
    if not claims and (uncited or own):
        data = {**summary_of([]), "status": blocks.STATUS_DONE, "uncited": uncited, "own": own}
        await ctx.stream.emit_block(blocks.verification_block(data))
        return data
    if not claims:
        return await _report_uncited(ctx, original)

    # Everything a model can rule on. What the batches below do not reach stays
    # `unchecked` and is counted as such, which is the whole difference between
    # a limit and a blind spot.
    judgeable = _judgeable(claims)
    window = await catalog.window_of(ctx.checker())
    per_call, reply_tokens, evidence_chars = _plan_for_window(ctx, window)
    groups = _batches(judgeable, per_call=per_call)
    # The check's own allowance past the preset: a call for every batch, as
    # many again for the halves of a batch whose reply did not parse, and one
    # for each finding's second look.
    ctx.budget.allow_check(calls=3 * len(judgeable), seconds=CHECK_S)
    if judgeable and not ctx.budget.can_verify():
        # Said rather than skipped in silence. An absent block is indistinguish-
        # able from a check that ran and found nothing wrong, and that reading
        # is the flattering one, which is exactly why it must not be available.
        log.info("no budget left for the citation check: %s", ctx.budget.summary())
        await ctx.stream.emit_block(
            blocks.verification_block(_pending(blocks.STATUS_SKIPPED, len(claims)))
        )
        return None

    block_id = ""
    if judgeable:
        # Emitted before the call, not after. This is a whole model round-trip
        # over an answer the reader can already see, and a page that looks
        # finished while it is not is the one thing the phase machinery in this
        # app exists to prevent. `status` drives the reading animation.
        opening = blocks.verification_block(_pending(blocks.STATUS_ACTIVE, len(claims)))
        block_id = opening["id"]
        await ctx.stream.emit_block(opening)

        failed = False
        plan = (reply_tokens, evidence_chars)
        for batch in groups:
            if not await _rule(ctx, question, batch, passages, registered, plan):
                failed = True
        await _second_look(ctx, question, judgeable, passages, registered, plan)
        if failed and all(claim.verdict == UNCHECKED for claim in judgeable):
            # Said, not swallowed. Removing the block would leave the reader
            # believing an answer was checked when it was not, and reporting
            # the claims as they stand (every one of them unchecked) would be a
            # verdict list with nothing in it.
            await ctx.stream.emit_patch(
                block_id,
                [blocks.replace("/data", _pending(blocks.STATUS_FAILED, len(claims)))],
            )
            return None

    # After the model and before the bookkeeping, so a quote the model called
    # supported is still caught, and a citation of a source that does not exist
    # still overrides both.
    check_quotes(claims, registered)
    check_passages(claims, registered)
    check_numbers(claims, registered)
    _finalise(claims)
    if attach_evidence(claims, registered) or any(claim.passage == EXACT for claim in claims):
        # The sidebar and every citation read the sources, so this is where the
        # highlights reach the reader.
        await ctx.sources.refresh()
    data = {**summary_of(claims), "status": blocks.STATUS_DONE}
    if uncited:
        data["uncited"] = uncited
    if own:
        data["own"] = own
    if block_id:
        await ctx.stream.emit_patch(block_id, [blocks.replace("/data", data)])
    else:
        # Every claim cited a source the run never retrieved, so no model was
        # asked, but that is a finding, and it still has to be shown.
        await ctx.stream.emit_block(blocks.verification_block(data))
    return data
