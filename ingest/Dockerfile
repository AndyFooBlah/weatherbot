# weatherbot ingest container — runs as a Cloud Run Job, triggered every
# 5 minutes by Cloud Scheduler. Built and pushed by infra/04-deploy-sync-job.sh.
#
# Entrypoint is the `weatherbot` CLI; default command is `sync`, which calls
# incremental_sync for each station on the account.

FROM python:3.12-slim

WORKDIR /app

# Deps layer first (cached when only source changes)
COPY pyproject.toml ./
COPY weatherbot_ingest ./weatherbot_ingest

RUN pip install --no-cache-dir .

# Cloud Run provides ADC automatically — Cloud SQL Connector and Secret
# Manager both pick it up via the assigned service account. No proxy
# sidecar needed.

ENTRYPOINT ["weatherbot"]
CMD ["sync"]
