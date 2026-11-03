# Jev as classifier and source checker

November 2026. Tested `typesafe/jev-1.14` on OpenRouter against Claude Haiku 5.5, which Simplixity uses as its judge.

## TL;DR

- **Cost:** Jev costs a quarter of Haiku, but that saves only 0.135 per 1,000 claims. Saving $10 a month would take about 75,000 claims so it might be worth it if simplixity is deployed in a team environment.
- **Classifier:** No gain. Jev can't write `search_queries`, `standalone_question` or `place`, so the Haiku call stays for every question that searches, and that call already returns `needs_search` and `complexity`.
- **Source check:** Same accuracy, but no explanation on flagged claims and no exact source passage for highlighting.
- **Stability:** The endpoint is alpha as of now, and the first call after a pause takes about 10 s.
- **Evidence:** 40 claims and 60 questions, labelled by one person. The models are within noise of each other.
- **Speed is the real argument.** Under 1 s against 7 to 22 s is noticeable.

## Result

Not adopted. Jev is about as accurate as Haiku and a lot faster, but the savings come to only a few dollars a month. Jev can't replace the classifier either, because it can't write search queries. That doesn't justify another API to maintain.

## Source check

40 claims checked against the sources they cite. 15 of them are supported and 25 are partial or unsupported. Sources were sent the way the app sends them: two 1,200-character chunks each.

| | Haiku 5.5 | Jev 1.14 |
| --- | --- | --- |
| Correct (of 40) | 37 to 39 | 37 to 38 |
| Bad claims passed (of 25) | 0 to 1 | 1 |
| Cost per 1,000 claims | $0.18 | $0.045 |
| Time per check | 7 to 22 s | under 1 s |

Both models stumble on the same kind of claim: one that leaves out a qualifier the source included. Jev's usual mistake is flagging a correct paraphrase as partial. When Jev's confidence was 0.5 or higher, its verdict was right every time, so that would work as the cut-off for handing a claim to Haiku.

For comparison, gpt-4o-mini got 30 to 33 right and let 4 to 6 bad claims through. With long pages it fell to 24.

## Classifier

60 questions in English and German. 50 of them need a search.

| Field | Haiku 5.5 | gpt-4o-mini | Jev 1.14 |
| --- | --- | --- | --- |
| `needs_search` | 100% | 88% | 100% |
| `complexity` | 98% | 92% | 96% |
| `recency` | 92% | 86% | 100% |
| `answer_shape` | 100% | 94% | 96% |
| Median latency | 3.5 s | 2.0 s | 0.24 s |

Jev only answers fixed questions. It can't produce `search_queries`, `standalone_question` or `place`, so the LLM call would still be needed for every question that searches. Jev also turned out to be sensitive to how its criteria are worded: after one criterion was shortened, `needs_search` dropped from 100% to 90%.

gpt-4o-mini skipped 7 searches it should have run. That matters if the classifier ever falls back to it.

## What we'd lose

- The judge's short explanation on flagged claims, since Jev returns only a label.
- The exact source passage used for highlighting. `page_words` would have to approximate it.
- Some stability. The endpoint is still alpha (`/api/alpha/decisions`), and the first call after a pause takes about 10 s.

## When to look again

- The verification delay becomes a real problem.
- Volume grows by a lot.
- The decisions API leaves alpha.

## Caveats

The sample is small: 60 questions and 40 claims, all labelled by one person. One answer moves a percentage by 2 to 5 points, so the Haiku and Jev results are within noise of each other. The claims were written for the test, not taken from real runs. All the runs together cost about $0.30.
