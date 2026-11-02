"""`python -m app`: the entry point the container and local dev both use.

Running uvicorn programmatically keeps the port in one place (config, hence one
env var) instead of duplicating it across the Dockerfile, the compose file and
the README, where the three drift apart the first time it changes.
"""

from __future__ import annotations

import uvicorn

from app.config import settings


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level.lower(),
        # Traefik terminates TLS and sets these; without it every client looks
        # like the proxy and rate limiting becomes global. Who may set them is
        # a setting, see `forwarded_allow_ips` in app/config.py.
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        # SSE connections are long-lived by design; uvicorn's default graceful
        # shutdown would wait for them on every redeploy.
        timeout_graceful_shutdown=10,
        access_log=False,  # the request middleware logs what is worth logging
    )


if __name__ == "__main__":
    main()
