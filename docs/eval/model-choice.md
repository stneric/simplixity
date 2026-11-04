# Which cheap, fast model for which role

November 2026. Nine cheap, fast models on OpenRouter, tested in the four roles the settings offer: router, judge, chat model and embeddings. Every test ran through the app's own code (the classifier prompt and schema, the citation check, the answer prompt at Balanced depth, the embedding prefixes), so the numbers show what Simplixity does with each model.

## TL;DR

- **Router:** Haiku. It classifies as well as Gemini 3.8 Flash (98% against 95%) for a fifth of the price. Its chat titles are weaker (70% against 97%), which matters only when titles are switched on.
- **Judge:** Haiku. It passed no bad claim, copied the evidence word for word 98% of the time, costs $0.11 per 1,000 claims, and comes from a different model family than the writer.
- **Chat model:** Gemini 3.8 Flash. First word after 1.8 s, 87% of its sentences cited, 98 to 100% of cited claims supported. DeepSeek Flash is as good for a fifth of the price.
- **Embeddings:** Voyage 4 Lite found the right passage for every question, as Gemini Embedding 2 did, at a tenth of the price.
- **Old default:** `openai/gpt-4o-mini`, which new profiles started with before this comparison, came last or nearly last in three of the four roles. A fresh install now starts with the picks above.
- **Evidence:** 60 questions, 40 claims, 10 answers and 30 embedding queries, written and labelled by Claude with no second reviewer. Differences of a few points are noise, and the judge and embedding tests were too easy to separate the best models.

## Result

A fresh install now uses these picks, pinned to the versions tested: `google/gemini-3.8-flash` writes, `anthropic/claude-haiku-5.5` triages and checks, `mongodb/voyage-4-lite` ranks. Every pick is at or near the top of its role. DeepSeek Flash as chat model would cut the price of an answer to a fifth at about the same quality.

The tests also turned up two bugs, listed at the end.

## Models

| Model | $ per 1M tokens in / out |
| --- | --- |
| `google/gemini-3.8-flash` | 0.825 / 4.125 |
| `google/gemini-3.5-flash-lite` | 0.30 / 2.50 |
| `~anthropic/claude-haiku-latest` (Haiku 5.5) | 0.10 / 0.50 |
| `~openai/gpt-luna-latest` | 0.10 / 0.50 |
| `~deepseek/deepseek-flash-latest` | 0.032 / 0.50 |
| `qwen/qwen3.8-flash` | 0.15 / 0.47 |
| `~z-ai/glm-flash-latest` | 0.027 / 0.10 |
| `minimax/minimax-m3.1-flash` | 0.15 / 0.60 |
| `openai/gpt-4o-mini` | the app's earlier default, as a reference |

Gemini 3.8 Flash ran twice, once by its own ID and once as `~google/gemini-flash-latest`. A call confirmed that the alias resolves to 3.8, and both runs cost the same, although OpenRouter still lists the alias at the 3.7 price. The tables below combine both runs.

## Router

### Classifier

60 questions in German and English, seven of them with a conversation before them, sent the way a run with depth and sources on "Auto" sends them. Graded per question: `needs_search`, `complexity`, `recency`, whether Scholar was picked, `answer_shape`, place, image, the standalone rewrite, and whether Scholar queries are in English. "All correct" means every graded field.

| Model | All correct | Median | p90 | $ per 1,000 questions |
| --- | --- | --- | --- | --- |
| Haiku | 98% | 3.1 s | 4.0 s | 0.70 |
| Gemini 3.8 Flash | 95% | 2.7 s | 3.8 s | 3.34 |
| DeepSeek Flash | 95% | 2.6 s | 5.5 s | 0.43 |
| GPT Luna | 90% | 3.9 s | 6.4 s | 0.17 |
| Qwen 3.8 Flash | 88% | 9.8 s | 13.9 s | 0.28 |
| Gemini 3.5 Flash Lite | 83% | 1.2 s | 1.6 s | 1.53 |
| MiniMax M3.1 Flash | 67% | 1.4 s | 3.7 s | 0.27 |
| gpt-4o-mini | 65% | 1.9 s | 2.8 s | 0.34 |
| GLM Flash | 60% | 2.3 s | 4.2 s | 0.33 |

Every model got `needs_search` right except gpt-4o-mini (97%). The weak models miss on `answer_shape`: MiniMax and GLM pick the wrong card or none a third of the time. gpt-4o-mini is the only one that missed the standalone rewrite and wrote German queries for Scholar.

The router's cost is almost all input, because the classifier's system prompt runs to about 2,500 tokens. Gemini's input price is eight times Haiku's, so it costs five times as much here.

### Titles

The router also names new chats unless a title model is set. Same 60 messages and checks as in [title-model.md](title-model.md).

| Model | Format rules | Key term named | German message, English title |
| --- | --- | --- | --- |
| Gemini 3.8 Flash (as `~google/gemini-flash-latest`) | 97% | 96% | 100% |
| DeepSeek Flash | 97% | 96% | 100% |
| Qwen 3.8 Flash | 93% | 96% | 87% |
| gpt-4o-mini | 75% | 88% | 63% |
| GLM Flash | 73% | 89% | 80% |
| Haiku | 70% | 96% | 77% |
| GPT Luna | 63% | 89% | 53% |
| Gemini 3.8 Flash (by its own ID), Gemini 3.5 Flash Lite, MiniMax | every call failed | | |

Haiku names the subject well but runs long: 9 of 60 titles have more than six words and 9 hit the 24-token limit. The failures are the first bug below.

Haiku and Gemini classify about equally well, and Haiku costs a fifth, about $2.60 less per 1,000 questions. Gemini writes better titles, but titles are off by default, and the [title model](title-model.md) does that job locally. Haiku also accepts having its reasoning switched off, so it avoids the first bug below. DeepSeek is cheaper still, but its p90 latency is the highest of the three, and the classifier runs before the first word of every answer.

## Judge

The citation check. 10 cases with sources and 40 claims, 22 of them supported, each case judged twice. The companies and programmes in the sources are made up, so no model can answer from memory. The claims contain the traps the judge prompt names: a figure without its qualifier ("3x faster", but only on SSDs), a generalisation from a narrow sample, an attribution the source does not make, a German claim against an English source, and true facts the source does not state ("Rayleigh described it in 1871"). Where two verdicts are defensible, both count.

| Model | Exact verdict | Bad claims passed | Good claims flagged | Evidence quote on the page | Per call | $ per 1,000 claims |
| --- | --- | --- | --- | --- | --- | --- |
| GPT Luna | 100% | 0 of 36 | 0 of 44 | 100% | 6.1 s | 0.07 |
| DeepSeek Flash | 100% | 0 of 36 | 0 of 44 | 94% | 4.6 s | 0.15 |
| Haiku | 99% | 0 of 36 | 1 of 44 | 98% | 3.3 s | 0.11 |
| Gemini 3.8 Flash | 99% | 1 of 72 | 0 of 88 | 100% | 3.5 s | 0.46 |
| Qwen 3.8 Flash | 99% | 0 of 36 | 1 of 44 | 100% | 10.5 s | 0.11 |
| GLM Flash | 98% | 1 of 36 | 0 of 44 | 92% | 2.2 s | 0.04 |
| MiniMax M3.1 Flash | 98% | 2 of 36 | 0 of 44 | 94% | 1.7 s | 0.06 |
| Gemini 3.5 Flash Lite | 95% | 4 of 36 | 0 of 44 | 93% | 1.3 s | 0.24 |
| gpt-4o-mini | 91% | 7 of 36 | 0 of 44 | 75% | 3.3 s | 0.06 |

The app highlights the evidence quote in the source, so a quote that is not on the page word for word cannot be shown.

Six models are within one claim of perfect, so this set does not separate them. The [Jev evaluation](jev-evaluation.md), on claims closer to real runs, put Haiku at 37 to 39 of 40. Haiku has the lowest price of the fast, accurate judges, and with Gemini writing the answer it does not share the writer's blind spots. GPT Luna and DeepSeek were as accurate and cheaper but slower per call, and the reader waits for the verdicts. Flash Lite, MiniMax and gpt-4o-mini let bad claims through.

## Chat model

The same 10 cases, each a question with its sources, 5 in German and 5 in English, answered at Balanced depth and streamed. The app's own parser split every answer into claims, and two judges from different families (Haiku and Gemini 3.8) checked them.

| Model | First word | Total | Sentences cited | Checkable claims per answer | Supported (Haiku / Gemini judge) | $ per 1,000 answers |
| --- | --- | --- | --- | --- | --- | --- |
| Gemini 3.8 Flash | 1.8 s | 3.0 s | 87% | 4.1 | 98% / 100% | 1.59 |
| DeepSeek Flash | 1.7 s | 3.0 s | 83% | 3.9 | 100% / 100% | 0.29 |
| Gemini 3.5 Flash Lite | 0.7 s | 1.0 s | 87% | 3.3 | 91% / 94% | 0.78 |
| GLM Flash | 0.8 s | 1.7 s | 74% | 3.2 | 94% / 100% | 0.18 |
| MiniMax M3.1 Flash | 1.0 s | 1.7 s | 73% | 4.1 | 93% / 93% | 0.34 |
| Qwen 3.8 Flash | 2.8 s | 4.2 s | 74% | 2.7 | 96% / 96% | 0.23 |
| Haiku | 3.6 s | 4.3 s | 53% | 4.8 | 98% / 100% | 0.55 |
| GPT Luna | 3.2 s | 3.5 s | 52% | 0.3 | 100% / 100% | 0.14 |
| gpt-4o-mini | 1.7 s | 2.7 s | 29% | 1.2 | 92% / 100% | 0.22 |

Every model answered in the reader's language except GPT Luna once, which answered an English question about a German subsidy in German. The test sources are short, so a real answer from ten long passages costs several times more.

Haiku cites accurately but adds its own advice between cited sentences ("If you run 3.9 or 3.10, you must upgrade Python first"), so half of what it writes goes unchecked. GPT Luna puts the citation after the full stop, which the app does not attach to the sentence (second bug below). gpt-4o-mini wrote bullet lists without citations. Flash Lite is by far the fastest and has the lowest support rate.

Gemini 3.8 Flash gets to the first word in under 2 s, cites the most and nearly everything it cites holds up. DeepSeek Flash matches it for a fifth of the price, about $1.30 less per 1,000 answers on these short inputs.

## Embeddings

45 short passages in 15 topics, three per topic on related aspects, so two of them are hard negatives. 30 questions, one German and one English per topic. Most passages are English, so half the questions test a German question against an English passage, which is what Scholar searches need.

| Model | Top hit | Top hit, German questions | In top 3 | Dimensions | $ per 1M tokens |
| --- | --- | --- | --- | --- | --- |
| `google/gemini-embedding-2` | 100% | 100% | 100% | 3072 | 0.20 |
| `mongodb/voyage-4` | 100% | 100% | 100% | 1024 | 0.06 |
| `mongodb/voyage-4-lite` | 100% | 100% | 100% | 1024 | 0.02 |
| `intfloat/multilingual-e5-large` | 100% | 100% | 100% | 1024 | 0.01 |
| `nvidia/nemotron-3-embed-1b:free` | 97% | 90% | 100% | 2048 | free |
| `perplexity/pplx-embed-v1-4b` | 97% | 90% | 100% | 2560 | 0.03 |
| `cohere/embed-5-fast` | 93% | 80% | 100% | 2048 | 0.08 |
| `nvidia/nemotron-3.5-embed-1b:free`, `-8b:free` | failed | | | | free |

The free default, Nemotron 3, misses only on German questions. Nemotron 3.5 fails with HTTP 502 from three texts per request on, and the app sends up to 64. multilingual-e5-large reads only 512 tokens, too few for the app's passages.

Voyage 4 Lite ranks as well as Gemini Embedding 2 for a tenth of the price, with vectors a third the size, so it is the default. On 20 full runs at depth Auto it brought the average cost of a question from $0.034 to $0.019. Changing the model makes stored vectors stale, and a literature project is embedded again on its next search.

## Bugs found

1. **Titles fail on models that require reasoning.** For titles the app sends `reasoning: {enabled: false}`. Gemini 3.8 Flash by its own ID, Gemini 3.5 Flash Lite and MiniMax answer HTTP 400 "Reasoning is mandatory for this endpoint and cannot be disabled", and `runs._title` only logs it, so the chat keeps its truncated name. `~google/gemini-flash-latest` works today because OpenRouter routes it to an endpoint that allows it, which can change.
2. **A citation after the full stop is not checked.** `verify.claims_of` does not attach "…on SSDs. [1]" to the sentence before it, so the reader sees a cited answer the citation check never looked at. The prompt does not say where the number goes.

Neither is fixed yet.

## Limits

- Small samples: one classifier question moves the score by 1.7 points.
- Claude wrote the test texts and labels for this test, following the wording of the app's prompts. Two labels were corrected after the first run because they contradicted the prompt.
- Latency was measured once, on 4 November 2026 in the afternoon (CET), and depends on the provider OpenRouter routes to.
- The `~…-latest` aliases will move on. These results are for the models they pointed to that day.
