"""One shared httpx client for the whole process.

Every outbound call (LLM, search, content, embeddings) goes through here so
connection pooling, limits and shutdown are decided once. Creating a client per
request works but re-establishes TLS on every LLM token stream, which is the
kind of waste that shows up as latency on a small VPS.
"""

from __future__ import annotations

import httpx

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    """The shared client, created on first use.

    No per-request timeout is set here: each caller has a very different idea of
    what "too slow" means (3s for discovery, minutes for a streamed answer), so
    timeouts are passed at the call site.
    """
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
            follow_redirects=True,
            headers={"User-Agent": "simplixity/0.1 (+https://github.com/stneric/simplixity)"},
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
