#!/usr/bin/env bash
# Deploy the daily sensor-outage report as a Cloud Run Job triggered by
# Cloud Scheduler once a day. Emails the report via the Gmail API.
#
# Reuses the weatherbot-sync service account and container image (the report
# is just another subcommand of the same `weatherbot` CLI). Idempotent.
#
# Prerequisites (one-time, manual): the Gmail OAuth refresh token + client
# credentials must exist in Secret Manager. See docs/gmail-report-setup.md.
#   SECRET_GMAIL_CLIENT_ID / _SECRET / _REFRESH_TOKEN in Secret Manager,
#   REPORT_FROM_EMAIL / REPORT_TO_EMAIL in infra/env.sh.
#
# Resources managed:
#   - Cloud Run Job         weatherbot-outage-report  (command: outage-report --send)
#   - Cloud Scheduler job   weatherbot-outage-report-daily
# Reused from 04-deploy-sync-job.sh:
#   - Service account       weatherbot-sync-sa  (already has cloudsql.client,
#                           secretAccessor, run.invoker, artifactregistry.reader,
#                           logging.logWriter)
#   - Image                 .../weatherbot/sync:<git-sha>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=env.sh
source "${SCRIPT_DIR}/env.sh"

gcloud config set project "${PROJECT_ID}" >/dev/null

JOB_NAME="weatherbot-outage-report"
SCHEDULER_NAME="weatherbot-outage-report-daily"
SA_EMAIL="weatherbot-sync-sa@${PROJECT_ID}.iam.gserviceaccount.com"
AR_REPO="weatherbot"
# 8:00 AM local (America/Los_Angeles) — after the typical dawn recovery, so
# an overnight outage that has resolved still shows in the prior-24h window.
SCHEDULE="0 8 * * *"
SCHEDULE_TZ="America/Los_Angeles"

: "${REPORT_FROM_EMAIL:?set REPORT_FROM_EMAIL in infra/env.sh (the Gmail account that sends)}"
: "${REPORT_TO_EMAIL:?set REPORT_TO_EMAIL in infra/env.sh (where the report goes)}"
: "${SECRET_GMAIL_CLIENT_ID:=weatherbot-gmail-client-id}"
: "${SECRET_GMAIL_CLIENT_SECRET:=weatherbot-gmail-client-secret}"
: "${SECRET_GMAIL_REFRESH_TOKEN:=weatherbot-gmail-refresh-token}"

GIT_SHA="$(cd "${ROOT_DIR}" && git rev-parse --short HEAD)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/sync:${GIT_SHA}"

echo "→ Deploying ${JOB_NAME} (image ${IMAGE})"

# ─── Build image (same source as the sync job; idempotent per SHA) ───
echo "  → building image (1–2 min)..."
gcloud builds submit "${ROOT_DIR}/ingest" --tag="${IMAGE}" --region="${REGION}" >/dev/null

JOB_ENV_VARS="PROJECT_ID=${PROJECT_ID}"
JOB_ENV_VARS+=",REGION=${REGION}"
JOB_ENV_VARS+=",CONNECTION_NAME=${CONNECTION_NAME}"
JOB_ENV_VARS+=",SQL_DB_USER=${SQL_DB_USER}"
JOB_ENV_VARS+=",SQL_DB_NAME=${SQL_DB_NAME}"
JOB_ENV_VARS+=",SECRET_DB_PASSWORD=${SECRET_DB_PASSWORD}"
JOB_ENV_VARS+=",REPORT_FROM_EMAIL=${REPORT_FROM_EMAIL}"
JOB_ENV_VARS+=",REPORT_TO_EMAIL=${REPORT_TO_EMAIL}"
JOB_ENV_VARS+=",SECRET_GMAIL_CLIENT_ID=${SECRET_GMAIL_CLIENT_ID}"
JOB_ENV_VARS+=",SECRET_GMAIL_CLIENT_SECRET=${SECRET_GMAIL_CLIENT_SECRET}"
JOB_ENV_VARS+=",SECRET_GMAIL_REFRESH_TOKEN=${SECRET_GMAIL_REFRESH_TOKEN}"

# The image ENTRYPOINT is `weatherbot`; --args overrides the default CMD
# (`sync`) so this job runs `weatherbot outage-report --send`.
JOB_ARGS="outage-report,--send"

if gcloud run jobs describe "${JOB_NAME}" --region="${REGION}" >/dev/null 2>&1; then
  echo "  → updating Cloud Run Job ${JOB_NAME}"
  gcloud run jobs update "${JOB_NAME}" \
    --region="${REGION}" --image="${IMAGE}" \
    --service-account="${SA_EMAIL}" \
    --set-env-vars="${JOB_ENV_VARS}" --args="${JOB_ARGS}" \
    --max-retries=1 --task-timeout=120s --memory=512Mi --quiet >/dev/null
else
  echo "  → creating Cloud Run Job ${JOB_NAME}"
  gcloud run jobs create "${JOB_NAME}" \
    --region="${REGION}" --image="${IMAGE}" \
    --service-account="${SA_EMAIL}" \
    --set-env-vars="${JOB_ENV_VARS}" --args="${JOB_ARGS}" \
    --max-retries=1 --task-timeout=120s --memory=512Mi --quiet >/dev/null
fi

# ─── Cloud Scheduler daily trigger ───────────────────────────────────
JOB_URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run"
if gcloud scheduler jobs describe "${SCHEDULER_NAME}" --location="${REGION}" >/dev/null 2>&1; then
  echo "  → updating Cloud Scheduler ${SCHEDULER_NAME}"
  gcloud scheduler jobs update http "${SCHEDULER_NAME}" \
    --location="${REGION}" --schedule="${SCHEDULE}" --time-zone="${SCHEDULE_TZ}" \
    --uri="${JOB_URI}" --http-method=POST \
    --oauth-service-account-email="${SA_EMAIL}" --quiet >/dev/null
else
  echo "  → creating Cloud Scheduler ${SCHEDULER_NAME}"
  gcloud scheduler jobs create http "${SCHEDULER_NAME}" \
    --location="${REGION}" --schedule="${SCHEDULE}" --time-zone="${SCHEDULE_TZ}" \
    --uri="${JOB_URI}" --http-method=POST \
    --oauth-service-account-email="${SA_EMAIL}" --quiet >/dev/null
fi

cat <<EOF

✓ Deployed.
  Job:       ${JOB_NAME}   (command: weatherbot ${JOB_ARGS//,/ })
  Schedule:  ${SCHEDULE}  (${SCHEDULE_TZ})  → daily 8am local
  Sends:     ${REPORT_FROM_EMAIL} → ${REPORT_TO_EMAIL}

Run once now (to test the Gmail path end-to-end):
  gcloud run jobs execute ${JOB_NAME} --region=${REGION} --wait

Tail logs:
  gcloud logging read 'resource.labels.job_name="${JOB_NAME}"' --limit=20 --freshness=1h
EOF
