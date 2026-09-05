# syntax=docker/dockerfile:1.7
#
# livepeer-open-clearinghouse-gateway image — Python 3.13, FastAPI, uv-managed deps.
# Multi-stage build: a `builder` stage installs deps into /opt/venv, then
# `runtime` copies that venv plus source + frontend assets.
#
# Runs as uid/gid 65532:65532 to match the daemons' UDS volume ownership.

# -----------------------------------------------------------------------------
# Stage 1: builder
# -----------------------------------------------------------------------------
ARG PYTHON_VERSION=3.13
ARG UV_VERSION=0.5
ARG VERSION=dev
ARG REVISION=unknown

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# Build tools + libpq for asyncpg's optional native deps.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# uv from the official image, no installer to keep things deterministic.
COPY --from=uv /uv /uvx /usr/local/bin/

WORKDIR /build

# Layer 1: lockfile only — maximizes cache hits when source changes
# but deps don't.
COPY pyproject.toml ./
COPY uv.lock* ./

# Sync into /opt/venv. --no-install-project so we don't try to install
# livepeer_open_clearinghouse itself before the source is present.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Layer 2: source. LICENSE + README.md are referenced from pyproject.toml
# (`license = { file = "LICENSE" }`, `readme = "README.md"`) so hatchling
# needs them during the build.
COPY LICENSE README.md ./
COPY src/ ./src/
COPY web/ ./web/
COPY migrations/ ./migrations/
COPY alembic.ini ./

# Install the project itself into the venv. --no-editable is critical:
# without it uv installs a .pth pointing at /build/src, which doesn't exist
# in the runtime stage and Python can't find `livepeer_open_clearinghouse` at startup.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable \
        --reinstall-package livepeer-open-clearinghouse

# Settlement verification is a billing boundary.  Importing eth-hash alone
# does not prove a Keccak backend made it into the production dependency set.
RUN /opt/venv/bin/python -c "from eth_hash.auto import keccak; assert len(keccak(b'')) == 32"

# -----------------------------------------------------------------------------
# Stage 2: runtime
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ARG VERSION
ARG REVISION

# OCI image metadata — the publish workflow's docker/metadata-action
# layers more labels on top at release time (revision, version, created).
# These bake-in labels carry across `make image-build` and any local
# build, matching the sibling repos' published-image shape.
LABEL org.opencontainers.image.title="livepeer-open-clearinghouse-gateway" \
      org.opencontainers.image.description="FastAPI clearinghouse gateway for Livepeer probabilistic-payment ticket minting and session settlement." \
      org.opencontainers.image.source="https://github.com/livepeer/livepeer-cloud-spe" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    OPEN_CLEARINGHOUSE_HOME=/srv/livepeer_open_clearinghouse

# libpq for asyncpg + tini for a clean PID 1.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 65532 livepeer_open_clearinghouse \
    && useradd --system --uid 65532 --gid 65532 \
        --home-dir $OPEN_CLEARINGHOUSE_HOME \
        --shell /usr/sbin/nologin livepeer_open_clearinghouse

WORKDIR $OPEN_CLEARINGHOUSE_HOME

# venv (full dep tree) from the builder.
COPY --from=builder /opt/venv /opt/venv

# Application source, frontend assets, and migrations.
COPY --chown=65532:65532 src/ ./src/
COPY --chown=65532:65532 web/ ./web/
COPY --chown=65532:65532 migrations/ ./migrations/
COPY --chown=65532:65532 alembic.ini ./

USER 65532:65532
EXPOSE 8000

# Schema migration is an explicit one-shot deployment job. The application
# refuses readiness on a mismatched revision; it never races Alembic from
# every gateway replica.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "livepeer_open_clearinghouse.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
