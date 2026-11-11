# TODO

## Store the writer's evidence with each answer

Today a message keeps the answer, the source list (title, URL, a ~150-character
snippet) and the verification result, but not the passages the writer actually
wrote from: ranked page text, a median of 14 passages of ~900 characters. Without
them a production answer cannot be checked against its input later, and it
cannot be used as a training example for a local writer model (pairing an answer
with snippets would teach a model to state facts it was never shown).

- Where: `app/agent/orchestrator.py`, right after `messages = build(passages, compact)`.
  Save what the prompt builder received, not the finished prompt: the kept
  passages (`Passage` as a dict: text, source_index, title, url, tier, tier_label,
  published, read), plus `shape`, `facets`, `depth`, `compact`, `library`,
  `linked`, and for deep runs `findings` and `unanswered`. Then the prompt can be
  rebuilt for any window size or ruleset.
- Storage: a new nullable column on `messages` (e.g. `evidence TEXT`, JSON), or a
  side table keyed by `message_id` if the size (~15-30 KB per answer) matters.
- Also worth keeping: the router's `Classification` for the turn.
- Privacy: this is page text the user's question pulled in, so it is as private
  as the chat. Delete it with the chat, and leave it out of any export unless
  asked.

Context: simplixity-writer-training (local 4B writer model) had to re-run the
whole pipeline to freeze evidence because of this.
