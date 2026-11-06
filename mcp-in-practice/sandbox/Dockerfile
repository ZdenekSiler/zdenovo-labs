# The blog-analytics MCP server, built to be run locked down (see run.sh).
# Base images pinned by digest: a tag can be re-pointed, a digest can't.
FROM ghcr.io/astral-sh/uv:0.11.7@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a AS uv

FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54 AS build
COPY --from=uv /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
# Exact versions from the lock, runtime dependencies only (no pytest, no anthropic).
RUN uv sync --frozen --no-dev --no-install-project
COPY blogmcp ./blogmcp
COPY sandbox/naughty_server.py ./sandbox/naughty_server.py

FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
# No uv, no compiler, no package manager cache in the final image: nothing to install with.
COPY --from=build /app /app
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin mcp
USER 10001:10001
WORKDIR /app
ENV PATH=/app/.venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    BLOG_DB=/data/blog.db HOME=/nonexistent
ENTRYPOINT ["python", "-m", "blogmcp.server"]
