# weatherbot ingest container — runs as a Cloud Run Job, triggered every
# 5 minutes by Cloud Scheduler. Built and pushed by infra/04-deploy-sync-job.sh.
#
# Entrypoint is the `weatherbot` CLI; default command is `sync`, which calls
# incremental_sync for each station on the account.
#
# Dependencies install from uv.lock (`uv sync --locked`), so the image ships
# exactly the versions Dependabot maintains in the lockfile, never whatever
# PyPI has newest at build time (AndyFooBlah/andy-private-docs#57).

FROM ghcr.io/astral-sh/uv:0.13.7 AS uv

FROM python:3.12-slim

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app

# Use the image's Python (never download one), install into /app/.venv, and
# copy files rather than hardlink from the cache (which is not kept).
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1

# Deps layer first (cached when only source changes)
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY weatherbot_ingest ./weatherbot_ingest
RUN uv sync --locked --no-dev --no-editable

ENV PATH="/app/.venv/bin:$PATH"

# Cloud Run provides ADC automatically — Cloud SQL Connector and Secret
# Manager both pick it up via the assigned service account. No proxy
# sidecar needed.

ENTRYPOINT ["weatherbot"]
CMD ["sync"]
