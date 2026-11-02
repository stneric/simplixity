"""Paper databases, queried directly, for the literature page.

Separate from `app/search`. That package answers a question from
whatever a search engine returns and cites pages; this one builds a reading list
the person screens paper by paper, and it needs what SearXNG throws away: the
whole abstract, stable ids to say "more like this one", and the citation graph.
"""
