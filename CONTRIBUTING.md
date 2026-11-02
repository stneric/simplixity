# Contributing

Bug reports, fixes and improvements are welcome. For anything larger than a
small fix, please open an issue first and describe what you want to change, so
we can agree on the approach before you spend time on it.

## Setup

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), Node.js 20+ (only to
run the frontend tests) and a Redis server for running the app locally.

```bash
uv sync
cp .env.example .env      # add an OpenRouter key, or use a local model
redis-server &
uv run python -m app      # http://localhost:3000
```

Search needs SearXNG. `docker compose up -d searxng` starts the configured
instance; point `SEARXNG_API_URL` in `.env` at it.

## Tests and checks

```bash
uv run pytest                            # backend
node --test "tests/frontend/*.test.mjs"  # frontend, no npm install needed
uv run ruff check app tests
uv run ruff format app tests
```

CI runs all four on every pull request.

The test suite never touches the network, a real Redis or a real API key.
`tests/conftest.py` provides `fakeredis` and a temporary SQLite database, and
model calls are stubbed. Please keep it that way: a test that needs a running
service ends up skipped in practice.

The frontend tests run the real modules from `web/` against a small DOM stub
in `tests/frontend/dom.mjs`. If a renderer needs something the stub does not
have, extend the stub.

## Conventions

- **Comments explain why.** Most of the code is commented at length,
  especially where a design looks odd. Please match that, and write the
  comment about the code as it is, not about what it used to be; history
  belongs in the commit message.
- **Failures are reported, not swallowed.** A failing step degrades the answer
  and says so in the transcript. That is why the code has broad `except`
  clauses; each one carries a comment saying why, and ruff's `B904` is off.
- **Every run ends with `done`.** Whatever new exit path you add to a run, it
  must still reach the runner's terminal event.
- **Dependencies.** There are eight runtime dependencies. A ninth has to be
  pure Python with no build step. There is deliberately no provider SDK.
- **Frontend.** No framework, no build step, no CDN. Never assign `innerHTML`
  with anything derived from model output; build nodes with `createElement`
  and `textContent`. Links go through `web/links.js`.
- **Schema changes** are additive migrations in `app/store/db.py`.
- **Prompts** all live in `app/agent/prompts.py`.
- Typographic characters (’ “ ” — ×) in prose and prompts are intentional and
  allowed in ruff's configuration.

[docs/architecture.md](docs/architecture.md) explains how the pieces fit
together. Where to make common changes:

| Change | Where |
|---|---|
| A new block kind | `app/agent/blocks.py` and a renderer in `web/blocks.js` |
| A new answer shape | a `Shape` in `app/agent/shapes.py`, its schema in `app/agent/widgets.py`, a renderer in `web/blocks.js` |
| A new search lens | `app/search/lenses.py`, and its engines in the SearXNG config in `docker-compose.yml` |
| A depth preset | `app/agent/depth.py` (the UI reads it from `/api/depths`) |

## Pull requests

- Keep a pull request to one change. A refactor and a feature are two PRs.
- Add or update tests for what you changed.
- Describe what the change does and why in the PR description. If it changes
  something visible, a screenshot helps.

By contributing you agree that your contribution is licensed under the
project's license, the GNU AGPL v3.0 or later.
