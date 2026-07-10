#!/usr/bin/env bash
# Deploy the weatherbot incremental sync as a Cloud Run Job triggered by
# Cloud Scheduler every 5 minutes.
#
# Idempotent — safe to re-run; updates resources in place.
#
# Resources managed:
#   - Service account                 weatherbot-sync-sa
#   - Artifact Registry repo          weatherbot          (Docker, ${REGION})
#   - Container image                 .../weatherbot/sync:<git-sha>
#   - Cloud Run Job                   weatherbot-sync
#   - Cloud Scheduler HTTP job        weatherbot-sync-tick
#
# IAM granted to the service account at project level:
#   - roles/cloudsql.client                connect to Postgres
#   - roles/secretmanager.secretAccessor   AWN keys + DB password
#   - roles/run.invoker                    Scheduler → Job invocation
#   - roles/artifactregistry.reader        Job pulls its own image
#   - roles/logging.logWriter              Writes to Cloud Logging

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=env.sh
source "${SCRIPT_DIR}/env.sh"

gcloud config set project "${PROJECT_ID}" >/dev/null

JOB_NAME="weatherbot-sync"
SA_NAME="weatherbot-sync-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
SCHEDULER_NAME="weatherbot-sync-tick"
AR_REPO="weatherbot"
SCHEDULE="*/5 * * * *"

# Image tag = current git SHA for traceability.
GIT_SHA="$(cd "${ROOT_DIR}" && git rev-parse --short HEAD)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/sync:${GIT_SHA}"

echo "→ Deploying weatherbot-sync"
echo "  image: ${IMAGE}"

# ─── 1. Service account ──────────────────────────────────────────────
if gcloud iam service-accounts describe "${SA_EMAIL}" >/dev/null 2>&1; then
  echo "  ✓ service account ${SA_EMAIL} already exists"
else
  echo "  → creating service account ${SA_EMAIL}"
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="weatherbot scheduled sync" \
    --quiet >/dev/null
fi

# ─── 2. IAM bindings ─────────────────────────────────────────────────
for role in \
  roles/cloudsql.client \
  roles/secretmanager.secretAccessor \
  roles/run.invoker \
  roles/artifactregistry.reader \
  roles/logging.logWriter; do
  echo "  → ensure ${role}"
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="${role}" \
    --condition=None \
    --quiet >/dev/null
done

# ─── 3. Artifact Registry ────────────────────────────────────────────
if gcloud artifacts repositories describe "${AR_REPO}" \
     --location="${REGION}" >/dev/null 2>&1; then
  echo "  ✓ artifact registry repo ${AR_REPO} exists"
else
  echo "  → creating artifact registry repo ${AR_REPO}"
  gcloud artifacts repositories create "${AR_REPO}" \
    --location="${REGION}" \
    --repository-format=docker \
    --quiet >/dev/null
fi

# ─── 4. Build + push image via Cloud Build ──────────────────────────
echo "  → building image (1–2 min)..."
gcloud builds submit "${ROOT_DIR}/ingest" \
  --tag="${IMAGE}" \
  --region="${REGION}"

# ─── 5. Cloud Run Job ────────────────────────────────────────────────
JOB_ENV_VARS="PROJECT_ID=${PROJECT_ID}"
JOB_ENV_VARS+=",SQL_DB_USER=${SQL_DB_USER}"
JOB_ENV_VARS+=",SQL_DB_NAME=${SQL_DB_NAME}"
JOB_ENV_VARS+=",CONNECTION_NAME=${CONNECTION_NAME}"
JOB_ENV_VARS+=",SECRET_AWN_API_KEY=${SECRET_AWN_API_KEY}"
JOB_ENV_VARS+=",SECRET_AWN_APP_KEY=${SECRET_AWN_APP_KEY}"
JOB_ENV_VARS+=",SECRET_DB_PASSWORD=${SECRET_DB_PASSWORD}"

if gcloud run jobs describe "${JOB_NAME}" \
     --region="${REGION}" >/dev/null 2>&1; then
  echo "  → updating Cloud Run Job ${JOB_NAME}"
  gcloud run jobs update "${JOB_NAME}" \
    --region="${REGION}" \
    --image="${IMAGE}" \
    --service-account="${SA_EMAIL}" \
    --set-env-vars="${JOB_ENV_VARS}" \
    --max-retries=1 \
    --task-timeout=300s \
    --memory=512Mi \
    --quiet >/dev/null
else
  echo "  → creating Cloud Run Job ${JOB_NAME}"
  gcloud run jobs create "${JOB_NAME}" \
    --region="${REGION}" \
    --image="${IMAGE}" \
    --service-account="${SA_EMAIL}" \
    --set-env-vars="${JOB_ENV_VARS}" \
    --max-retries=1 \
    --task-timeout=300s \
    --memory=512Mi \
    --quiet >/dev/null
fi

# ─── 6. Cloud Scheduler trigger ──────────────────────────────────────
JOB_URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run"

if gcloud scheduler jobs describe "${SCHEDULER_NAME}" \
     --location="${REGION}" >/dev/null 2>&1; then
  echo "  → updating Cloud Scheduler ${SCHEDULER_NAME}"
  gcloud scheduler jobs update http "${SCHEDULER_NAME}" \
    --location="${REGION}" \
    --schedule="${SCHEDULE}" \
    --uri="${JOB_URI}" \
    --http-method=POST \
    --oauth-service-account-email="${SA_EMAIL}" \
    --quiet >/dev/null
else
  echo "  → creating Cloud Scheduler ${SCHEDULER_NAME}"
  gcloud scheduler jobs create http "${SCHEDULER_NAME}" \
    --location="${REGION}" \
    --schedule="${SCHEDULE}" \
    --uri="${JOB_URI}" \
    --http-method=POST \
    --oauth-service-account-email="${SA_EMAIL}" \
    --quiet >/dev/null
fi

cat <<EOF

✓ Deployed.

Manually trigger a run:
  gcloud run jobs execute ${JOB_NAME} --region=${REGION} --wait

List recent executions:
  gcloud run jobs executions list --job=${JOB_NAME} --region=${REGION} --limit=5

Tail logs from the latest execution:
  gcloud logging read 'resource.type="cloud_run_job"
      resource.labels.job_name="${JOB_NAME}"' \\
    --limit=50 --format='value(timestamp,textPayload)' --freshness=1h

Pause the schedule (e.g. while debugging):
  gcloud scheduler jobs pause ${SCHEDULER_NAME} --location=${REGION}
EOF
