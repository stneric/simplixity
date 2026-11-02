"""Embeddings and passage ranking.

Deliberately empty. Re-exporting `rerank` from here made importing
`app.embed.embedder` (a self-contained HTTP client) drag in `app.search.base`
transitively, because `rerank` needs `SearchResult`. Every caller already
imports the submodule it wants, so the re-export bought nothing and welded two
unrelated layers together.
"""
