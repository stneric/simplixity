# Changelog

All notable changes are listed here. The project follows
[Semantic Versioning](https://semver.org/); before 1.0, minor versions may
include breaking changes, which are called out.

## Unreleased
- An earlier answer reused as a project source no longer carries its own
  direct-quotation citations (`[3 quote: "…"]`) into the new answer, where the
  number would have pointed at a different source.
- A project created with a name of only spaces shows as "Untitled project"
  straight away, not as a blank row until the page is reloaded.
- A malformed session cookie is answered with the profile picker, not with a
  server error.
- A router model saved without a provider runs where the chat model does, as
  the title, judge and literature models already did.
- The `TRUSTED_HOSTS` setting is gone. It was read and never used.
- Internal clean-up: one copy each of the helpers that had been repeated across
  the store, the image proxies, the agent and the frontend.

## 0.3.0 (2026-11-11)
- Thoughts can be opened while the model is still thinking. The box used to
  be rebuilt on every streamed token, which swallowed the click and made an
  opened box jump back to its top. A reader at the bottom of the box now
  follows the newest thought.
- Setup instructions for the Simplixity model family on the server version.

## 0.2.2 (2026-11-11)
Small fix for running local models. Adjusted for usage with the simplixity model family. Not yet available on hf but in testing.

## 0.2.1 (2026-11-05)
- Images can be attached to a chat (PNG, JPEG, WebP, GIF): with the
  paperclip, by pasting, or by dropping them anywhere on the chat window,
  which now shows a drop overlay. They are sent to the chat model with the
  question, and again with follow-ups while that question is in the history.
  Pictures do not turn a chat into one that answers only from its files.
- The model picker marks models with vision. With a model that cannot see,
  image chips and a note above the composer say so and the question is held
  back; a request that gets through anyway is answered without the images
  and with a notice.

## 0.2.0 (2026-11-04)

- **Breaking:** models are chosen in the settings only. `DEFAULT_MODEL`,
  `DEFAULT_PROVIDER`, `ROUTER_MODEL`, `ROUTER_PROVIDER`, `TITLE_MODEL`,
  `TITLE_PROVIDER`, `EMBEDDING_MODEL`, `EMBEDDING_PROVIDER` and
  `EMBEDDING_ENABLED` in `.env` are no longer read. An install that set its
  models there and never saved them in the settings switches to the new
  defaults; picking them in the settings once keeps them.
- A fresh install starts with models tested in each role, pinned to the
  tested versions: `google/gemini-3.8-flash` writes, `anthropic/claude-haiku-5.5`
  triages and checks citations, `mongodb/voyage-4-lite` ranks. They were
  picked from nine cheap, fast models on OpenRouter, see
  `docs/eval/model-choice.md`. The old default `openai/gpt-4o-mini` is gone.
  Profiles with saved settings keep their models.
- With these defaults a question costs $0.019 on average at depth Auto,
  measured on 20 questions. Voyage 4 Lite ranked as well as Gemini Embedding 2
  and cuts the average from $0.034.
- Without an OpenRouter key the default router and judge step aside and the
  chat model does their work, so an install that only runs Ollama or LM Studio
  needs no changes.
- A fine-tuned `Qwen/Qwen3.5-0.8B` for chat titles that runs without a GPU,
  on Hugging Face as `stneric/simplixity-title-0.8b` and
  `stneric/simplixity-title-0.8b-GGUF`. 95% of its titles follow every format
  rule in Ollama, against 35% untuned; German vocabulary is still its weak
  point. See `docs/eval/title-model.md`.
- Chat titles work on models whose reasoning cannot be switched off, such as
  Gemini 3.5 Flash Lite and MiniMax on OpenRouter: the app asks once more with
  room to think and starts there for later titles.
- Local thinking models such as Qwen3.5 in Ollama name chats again. The app
  tells Ollama not to think for titles, and asks again with room to think if a
  model thinks anyway and writes nothing.
- A citation placed after the full stop ("…on SSDs. [1]") is checked with the
  sentence before it, and the prompt asks for the number before the full stop.
- The README lists the default models, why each was picked and what a
  question costs, and explains how to install the app on a phone.

## 0.1.1 (2026-11-03)

- The citation check rules on every cited statement of an answer: there is no
  ceiling of three calls any more, the judge's reply has room for every verdict
  in its batch, and a batch whose reply fails is halved and asked again instead
  of ending the check.
- Fewer false "partly" verdicts: the judge reads a claim's cited sources
  together and counts a paraphrase as support. Every claim ruled against gets
  a second look that splits it into its statements and asks for the source's
  words for each; the verdict follows from which quotes are found on the page,
  and the note names the statements that are not.
- The citation check is an icon under the answer that shows its verdict:
  orange with the number of problems, light green when every statement held,
  grey when it did not run. While it runs, the eye animates in the icon.
- Quieter progress display while a run is going, showing only the steps that
  matter, and a reworked settings dialog.
- The frontend in `web/js` is split into folders by area (`app`, `chat`,
  `render`, `literature`, `projects`, `reader`, `tools`, `shared`).
- Dependency updates, among them FastAPI 0.162, pydantic 2.14, pypdf 6.21 and
  certifi 2026.10.18. Dependabot now keeps them current.
- The README says what AI was used for in this project.

## 0.1.0 (2026-11-02)

First public release.

- A link in the question is read first and treated as the main source: it is
  cited as [1], read on every depth, and a question about the page alone is
  answered without a search.
