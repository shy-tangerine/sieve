# syntax=docker/dockerfile:1

# Base images are pinned by digest (issue #78): mutable tags are replaced with
# reviewed immutable digests at release time. To update, pull the new tag,
# record its digest, and review the diff before committing the pin.
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9 AS base

COPY --from=ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE NOTICE.ddgs.txt THIRD-PARTY-NOTICES.md ./
COPY licenses ./licenses
COPY sieve ./sieve

RUN uv pip install --system --no-cache ".[legacy-server]"

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin sieve
USER sieve

EXPOSE 8765

FROM base AS lite
ENTRYPOINT ["sieve", "mcp", "serve", "--transport", "http", "--host", "0.0.0.0", "--port", "8765"]

FROM base AS browser
USER root
RUN uv pip install --system --no-cache ".[browser]" \
    && python -m patchright install chromium
USER sieve
ENTRYPOINT ["sieve", "mcp", "serve", "--transport", "http", "--host", "0.0.0.0", "--port", "8765"]
