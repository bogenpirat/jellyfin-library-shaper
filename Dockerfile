# syntax=docker/dockerfile:1
FROM python:3.14-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.10.3 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# Full test suite. Runs as an unprivileged user: root can read chmod-000 files, which would defeat
# the readiness tests.
FROM base AS test
RUN uv sync --frozen && useradd --create-home --uid 1000 tester
COPY tests ./tests
USER tester
CMD ["pytest", "-q", "-p", "no:cacheprovider"]

FROM base AS runtime
ENV STATE_DIR=/config
ENTRYPOINT ["python", "-m", "shaper"]
CMD ["run"]
