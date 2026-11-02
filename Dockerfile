FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependency layer first, so source edits do not invalidate it.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY web ./web
# Measures a literature project's semantic bar on the live database; see the file.
COPY tools/calibrate_library.py ./tools/calibrate_library.py

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && useradd --create-home --uid 1000 simplixity \
    && mkdir -p /data && chown -R simplixity:simplixity /data /app

# Deliberately no `USER simplixity`: the entrypoint needs root to fix the ownership of
# a bind-mounted /data, and drops to simplixity with setpriv before exec'ing the app.
# The Python process never runs as root.
ENTRYPOINT ["docker-entrypoint.sh"]

# Matches app.config.port; the reverse proxy targets the same number.
EXPOSE 3000
CMD ["python", "-m", "app"]
