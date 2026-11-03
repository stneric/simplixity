# Architecture

This document explains how a question becomes a cited answer, and why some
parts are built the way they are. It assumes you have read the
[README](../README.md).

```
browser ──HTTP/SSE──▶ app (FastAPI) ──┬── Redis     live run state, event streams
                                      ├── SQLite    chats, profiles, projects, PDFs
                                      ├── SearXNG   web search
                                      ├── r.jina.ai page reading
                                      └── model APIs (OpenRouter, Groq, Ollama, LM Studio)
```

## Layout

```
app/
  main.py          FastAPI app, middleware, lifespan (orphan reaping at boot)
  runs.py          the seam between HTTP, Redis and the agent
  config.py        settings from environment variables
  api/             HTTP routes: chat, chats, events (SSE), models, profiles, …
  agent/
    orchestrator.py  route, then fetch, answer directly, or research, then write
    route.py         how much work a question is worth
    deep.py          plan, parallel sub-questions, gap round, report
    research.py      the primitives: search, read, rank, write
    verify.py        the citation check
    depth.py         budget presets            budget.py   spending and reserves
    shapes.py        answer layouts            widgets.py  card schemas
    prompts.py       every prompt, in one file
    blocks.py        the block protocol        context.py  per-run state
    runner.py        background supervision of runs
  llm/             one OpenAI-compatible client for every provider
  search/          SearXNG client, lenses, quality tiers, page reading, library search
  embed/           embeddings and reranking, with a keyword fallback
  scholar/         paper databases, merging, feedback ranking, BibTeX, PDF matching
  tools/           literature workflow, full-text upload, documents, redaction
  store/           SQLite schema and queries, Redis streams
web/               the frontend: ES modules, no framework, no build step
tests/             pytest suite and node:test frontend suite
```

## The run lifecycle

An HTTP request starts a run but never owns it. `POST /api/chat` validates the
request, stores the question, and hands the run to `app/agent/runner.py`, which
supervises it as a background task in one process-wide pool bounded by
`MAX_CONCURRENT_RUNS`. The response returns immediately with a run id. The
browser then subscribes to `GET /api/runs/{id}/events`.

The runner enforces one rule: every exit path emits a terminal `done` event,
whether the run finished, failed, was cancelled or hit the hard time limit. A
run that ends without one leaves the UI showing a spinner forever. The SSE
layer (`app/api/events.py`) never invents a `done` event itself, because it
only knows that the connection broke, not that the run did.

Runs live in the server process. Anything still marked `running` when the app
starts was orphaned by a restart, and the lifespan handler in `app/main.py`
closes it with an error.

## Streaming

Run events go into a Redis Stream, and the stream entry id is used as the SSE
event id (`app/store/stream.py`). When the connection drops, the browser's
`EventSource` reconnects with `Last-Event-ID`, which is exactly the cursor
`XREAD` takes. That is the whole resume mechanism: no reconnect endpoint, no
client-side state machine, no in-memory replay buffer. A reload, a sleeping
laptop or a network change all resume the same way.

`web/sse.js` coalesces renders to one per animation frame, so rejoining a long
run does not re-render once per replayed event.

## Block protocol

The agent emits blocks and the frontend renders them. Neither knows anything
else about the other.

```
block  {id, type, data}       a new block
patch  {id, ops}              changes to an existing block
phase  {phase}                coarse UI state
done   {status, …}            terminal
```

Ops are the RFC 6902 subset `add`, `replace`, `remove`, plus `append` for
streamed text, since resending the whole answer on every token would be
wasteful (`store.stream.apply_patch`). Block kinds and phase names are defined
in `app/agent/blocks.py`; each kind has a renderer in `web/blocks.js`.

Anything that has to outlive the run must be a block. `done` is not persisted
and the stream expires after a day, so the run footer (`provenance`) and the
reason a run failed (`notice`) are blocks. Reopening a chat a week later shows
which engines were used and why a run stopped.

`web/blocks.js` keeps one DOM node per block id and re-renders only what
changed. Replacing everything on every patch would destroy text selection and
close open `<details>` several times a second while an answer streams.

## Budgets and routing

Research depth is expressed as budgets, never as iteration counts. A preset in
`app/agent/depth.py` caps search width, pages read, evidence size and wall
clock time. `app/agent/budget.py` tracks what a run has spent and reserves time
and one model call for the writer up front, so a run that exhausts its budget
still answers with what it found.

`app/agent/route.py` decides how much a question is worth in two steps.
`triage()` is plain string matching with no model call; it only acts when it is
certain (small talk, a weather question naming a place) and otherwise declines.
`plan()` then maps the classifier's complexity estimate onto a preset.

The router may only reduce work. A preset the user picked is a ceiling and is
never widened, and every reduction is reported in the run footer next to what
was asked for.

## Single sources of truth

Several definitions are served to the UI rather than duplicated in it:

- `app/agent/depth.py` → `/api/depths` (the depth menu)
- `app/search/lenses.py` → `/api/lenses` (the source menu)
- `app/agent/shapes.py` → the card schemas in `app/agent/widgets.py`

`tests/test_widgets.py` fails if a shape has no schema or a schema no shape.

The SearXNG engine names in `docker-compose.yml` must match
`app/search/lenses.py`. An unknown engine name silently disables SearXNG's
engine filter entirely, and `tests/test_sources.py` checks the two lists
against each other.

## Answer shapes, cards and the overview

The classifier picks an answer shape: plain prose, or one of the fixed layouts
(figure, series, table, comparison, steps, weather, match, profile, image,
diagram). A layout is a card the model fills as JSON against a schema; the
server validates it and the browser draws it. A weak model that writes clumsy
prose still produces a correct card, because it only has to get a few named
fields right. If it gets those wrong too, the card is dropped and the prose
stands alone.

Every card schema is a `widgets.Card` and validates itself in `payload()`
against the run's numbered sources. A card that cites a source the run never
retrieved is thrown away. `Filled` is the one place raw model values are
coerced and trimmed, so a schema only declares limits with `max_length`.

Pictures are a separate decision. The image shape always searches for one,
cards that draw their own content (weather, match, figure and the rest) never
do, and everywhere else, the profile included, the classifier's `wants_image`
decides. A question that asks for a picture by name ("Bild von X", "show me a
picture of X") sets it regardless, through a word match in `route.asks_to_see`,
and asking to see something always means searching. The picture search uses
the classifier's `image_query`, a short English caption of the subject, because
image archives index captions. A profile or image card picks its picture with a
model call; a prose answer shows the best-ranked result of the picture search
without one.

Between fixed layouts and prose sits the *overview*: the classifier can name up
to three facets (facts, stats, timeline, pros and cons, list, grid, callout),
and a panel is composed from those, every item cited on its own. Like the
explainer diagram it is optional; if it fails, the prose is unaffected. The
writer is told what the panel holds so it does not repeat it. Adding a facet
takes a `Facet` in `shapes.py`, an entry in `prompts.OVERVIEW_FACETS`, a rule in
`widgets._COMPLETE` and a renderer in `overviewSections` in `web/blocks.js`.

All prompts are in `app/agent/prompts.py`, so they can be read side by side.

## Source quality and ranking

`app/search/quality.py` gives every result a provenance tier: official text,
peer-reviewed or academic (a DOI, a known publisher), documentation, newsroom,
reference, ordinary web. Results are filtered and ordered in this sequence:

1. **Relevance.** A result whose title and snippet share none of the
   question's subject terms is dropped. Terms are weighted by how many of the
   results use them, so the question's own phrasing ("is … worth taking") does
   not count as its subject.
2. **Provenance.** Nothing is blocklisted. Low-quality pages are outranked, so
   a question *about* a consultancy's opinion can still find it.
3. **Agreement.** How many independent engines returned a result, and for how
   many of the run's queries.
4. **Freshness**, only when the classifier judged the question time-sensitive.

No domain may take more than two citation slots. The same tier labels reach the
model with each source in the prompt, along with publisher, date and whether
the page was read in full or only as a snippet.

The Science lens points the same pipeline at PubMed, arXiv, Crossref, OpenAlex,
Semantic Scholar and Google Scholar. Lenses combine, and their results are
ranked against each other.

## Linked pages

A link in the question names the page the answer is about, which outranks
anything the ranking could decide. `app/search/links.py` finds public http(s)
links in the question (at most three; `localhost`, private addresses and bare
container names are ignored, since a self-hosted reader sits on the same network
as Redis and SearXNG). `app/agent/linked.py` then reads each page before the
classifier runs, on every preset including Quick, and registers it first,
outside the source budget, so the first link is always source [1].

The classifier sees the opening of the page. A request about the page itself
("summarise this") is answered from the page without a search; a request that
needs more ("what do other sources say?") searches with queries written from
the page's subject, and no query ever carries the URL. The writer gets the
page's passages ahead of the ranked ones, so trimming for a small window cuts
search results first, and a rule in the prompt names the linked source as the
subject of the answer. A page that cannot be read is reported in a notice, and
the run falls back to an ordinary search. In a literature project with the web
off, links are not followed.

## The citation check

`app/agent/verify.py` runs after the answer has streamed, so it never delays
it. The claims are enumerated by code, not chosen by the model, and each is
sent with the passages of the sources it cites. A judge model rules on each:
supported, partial or unsupported. Three mechanical checks run alongside and
the model cannot overrule them:

- quotations and cited passages must appear word for word in the source,
- every number in a claim must appear in the full text of its sources
  (`check_numbers`, both decimal conventions, rounding allowed),
- a citation to a number the run never retrieved is unsupported.

The judge is `judge_model` when set, otherwise the router model's rule. A claim
nobody ruled on (past the per-call limit, a truncated response, no budget left)
stays `unchecked` and is shown as such. The footer names both the writer and
the judge.

Earlier turns of a conversation reach the model without their old citation
numbers (`prompts.trim_history`), so a follow-up cannot cite a source from a
previous run by accident.

## Persistence

SQLite holds everything durable: profiles, chats, messages, settings, projects,
reading lists and uploaded files. Redis holds only live run state and may be
lost without losing history.

Schema changes are additive migrations in `app/store/db.py`.
`CREATE TABLE IF NOT EXISTS` skips a table that already exists, so a new
column needs an entry in `MIGRATIONS`. Indexes are created after migrations,
because an index over a column a migration is about to add cannot be created
first.

### Profiles

Chats, projects and settings belong to a profile (`app/store/profiles.py`),
optionally protected by a scrypt-hashed password. `POST /api/profiles/login`
sets a signed session cookie; `api.deps.current_user` verifies it and answers
401 without one, which is the frontend's cue to show the profile picker
(`web/profile.js`). The signature covers the password hash, so changing a
password ends other sessions.

Store functions take a `user=` argument. Every route passes the profile;
`None` means unscoped and is for internal callers only. A row that belongs to
another profile reads as missing (404). Data from before profiles existed
belongs to `db.DEFAULT_USER`.

Profiles keep people who share an install apart. They are not hardened
authentication: there is no login rate limit and no password recovery.

## Model client

`app/llm/client.py` is a single client for every provider. OpenRouter, Groq,
Ollama and LM Studio all speak the OpenAI chat-completions format, so they
differ only in base URL. There is no provider SDK, on purpose: the client sets
explicit timeouts and a bounded concurrency limit, both of which an SDK's
defaults would override (a ten-minute timeout with two retries can hang a run
for half an hour).

Structured output (`LLM.structured`) tries native JSON-schema mode first and
falls back to prompting for JSON, showing the model its own invalid reply on
retry.

## Literature projects and attachments

See [literature.md](literature.md) for the user-facing workflow. In short: a
project that contains a reading list is a *literature project*, and a chat
with attached files behaves the same way. Their runs search a
`search.library.Library` (the uploaded PDFs, cut into page-bound passages and
embedded) instead of, or alongside, the web. `runs._start` decides this from
where the chat is filed, never from the request.

Such a run never answers from the model alone, does no lookups or picture
searches, and its writer gets `prompts.LIBRARY_RULES`: each statement is
anchored to a passage, `[4: "exact words"]`, and shown as paper and page. The
verifier then checks every sentence, not only cited ones
(`verify.paragraph_claims`), and compares each anchored passage word for word
with its page (`verify.check_passages`). The writer's own reasoning goes in
blockquotes and is listed as such rather than as missing a source.

The library search has floors (`LEXICAL_COVERAGE`, `SEMANTIC_GAP`) so it can
find nothing, and a question the papers do not cover gets no answer rather
than one from the model's own knowledge. `tools/calibrate_library.py` measures
the semantic floor for an install.

Stored embeddings are tagged with their model and version
(`embedder.vector_tag`). After a model change a run re-embeds a slice of the
missing vectors before searching and the rest in a background task
(`Library._heal`).

## Frontend

No framework, no build step, no CDN. The ES modules under `web/` are served as
they are, and the Content Security Policy allows only `'self'`. File names are
not hashed, so the server sends `no-cache, must-revalidate` and the service
worker fetches network-first.

The markdown renderer (`web/markdown.js`) never assigns `innerHTML`. It builds
DOM nodes with `createElement` and `textContent`, so model output cannot become
markup and there is nothing to sanitise. The TeX and diagram renderers follow
the same rule. Every outbound link and citation goes through `web/links.js`,
which checks the URL scheme and opens the link in a new tab with `noopener`.

Under each answer sits the *tray*: one row of icons for what is about the
answer rather than part of it (research steps, sources, a clean citation check,
the run footer, copy, time and model). Each icon's tooltip is the summary line,
and a click opens the full block. Anything the reader has to see, such as a
check that found problems or a failure notice, stays in the transcript as
text.

## Failure handling

A failure degrades the answer and says so in the transcript; it never vanishes
silently and never crashes the run past the user. A dead search engine, a
rate-limited page reader or an exhausted budget each produce a notice. This is
why the code has many broad `except` clauses: each one is commented where it
appears, and ruff's `B904` is disabled for this reason.
