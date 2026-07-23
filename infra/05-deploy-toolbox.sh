#!/usr/bin/env bash
# Deploy the MCP Toolbox as a private Cloud Run service `weatherbot-toolbox`.
# Idempotent — safe to re-run.
#
# Resources managed:
#   - Service account                weatherbot-toolbox-sa
#   - Cloud SQL IAM DB user          (SA, for GDA QueryData path)
#   - Postgres GRANT SELECT          (on public schema, for the SA)
#   - Postgres GRANT INSERT          (on events only — record_event tool)
#   - Container image                .../weatherbot/toolbox:<git-sha>
#   - Cloud Run service              weatherbot-toolbox (no-allow-unauth)
#
# IAM granted to the service account at project level:
#   - roles/cloudsql.client                connect via Cloud SQL Connector
#   - roles/cloudsql.instanceUser          IAM DB authentication
#   - roles/cloudsql.studioUser            required by the GDA Data API path
#   - roles/secretmanager.secretAccessor   DB password from Secret Manager
#   - roles/geminidataanalytics.dataAgentUser   QueryData (ask_data tool)
#   - roles/geminidataanalytics.queryDataUser   explicit caller of QueryData
#   - roles/serviceusage.serviceUsageConsumer   REQUIRED: GDA's QueryData
#       backend uses the caller's identity for the downstream Cloud SQL
#       Data API call, which checks `serviceusage.services.use` on the
#       project for consumer-project attribution. Without this, ask_data
#       fails with `PERMISSION_DENIED: Error from Cloud SQL Instance ...
#       [PERMISSION_DENIED]` only from Cloud Run origins (laptop
#       impersonation tokens chain through the human user's owner role
#       and incidentally satisfy the check).
#   - roles/logging.logWriter
#   - roles/monitoring.metricWriter        --telemetry-gcp metrics

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=env.sh
source "${SCRIPT_DIR}/env.sh"

gcloud config set project "${PROJECT_ID}" >/dev/null

# Overridable for the eval deployment (08-deploy-eval-toolbox.sh):
#   TOOLBOX_SERVICE_NAME — Cloud Run service name
#   TOOLBOX_DB_NAME      — Postgres database the toolbox points at
# Defaults deploy the production toolbox exactly as before.
SERVICE_NAME="${TOOLBOX_SERVICE_NAME:-weatherbot-toolbox}"
TOOLBOX_DB_NAME="${TOOLBOX_DB_NAME:-${SQL_DB_NAME}}"
SA_NAME="weatherbot-toolbox-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
AR_REPO="weatherbot"

GIT_SHA="$(cd "${ROOT_DIR}" && git rev-parse --short HEAD)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/toolbox:${GIT_SHA}"

echo "→ Deploying ${SERVICE_NAME} at ${IMAGE}"

# ─── 1. Service account ──────────────────────────────────────────────
if gcloud iam service-accounts describe "${SA_EMAIL}" >/dev/null 2>&1; then
  echo "  ✓ service account ${SA_EMAIL} exists"
else
  echo "  → creating service account ${SA_EMAIL}"
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="weatherbot MCP Toolbox" \
    --quiet >/dev/null
fi

# ─── 2. Project IAM bindings ─────────────────────────────────────────
for role in \
  roles/cloudsql.client \
  roles/cloudsql.instanceUser \
  roles/cloudsql.studioUser \
  roles/secretmanager.secretAccessor \
  roles/geminidataanalytics.dataAgentUser \
  roles/geminidataanalytics.queryDataUser \
  roles/serviceusage.serviceUsageConsumer \
  roles/logging.logWriter \
  roles/monitoring.metricWriter; do
  echo "  → ensure ${role}"
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="${role}" \
    --condition=None \
    --quiet >/dev/null
done

# ─── 3. Cloud SQL IAM DB user for the SA ─────────────────────────────
SA_DB_USER="${SA_EMAIL%.gserviceaccount.com}"  # Cloud SQL truncates the suffix
if gcloud sql users list --instance="${SQL_INSTANCE}" \
     --format='value(name)' | grep -qx "${SA_DB_USER}"; then
  echo "  ✓ Cloud SQL IAM user for ${SA_EMAIL} exists"
else
  echo "  → creating Cloud SQL IAM user for ${SA_EMAIL}"
  # Cloud SQL requires the .gserviceaccount.com suffix stripped.
  gcloud sql users create "${SA_DB_USER}" \
    --instance="${SQL_INSTANCE}" \
    --type=cloud_iam_service_account \
    --quiet
fi

# ─── 4. GRANT SELECT in Postgres via cloud-sql-proxy ─────────────────
command -v cloud-sql-proxy >/dev/null || {
  echo "✗ cloud-sql-proxy not in PATH" >&2; exit 1; }
command -v psql >/dev/null || {
  echo "✗ psql not in PATH" >&2; exit 1; }

CONNECTION_NAME="$(gcloud sql instances describe "${SQL_INSTANCE}" \
                     --format='value(connectionName)')"
PROXY_PORT="${PROXY_PORT:-15434}"
PROXY_LOG="$(mktemp -t weatherbot-proxy.XXXXXX.log)"

echo "  → starting cloud-sql-proxy on 127.0.0.1:${PROXY_PORT}..."
cloud-sql-proxy --port "${PROXY_PORT}" "${CONNECTION_NAME}" \
  >"${PROXY_LOG}" 2>&1 &
PROXY_PID=$!
trap 'kill "${PROXY_PID}" 2>/dev/null || true; wait "${PROXY_PID}" 2>/dev/null || true' EXIT

for _ in $(seq 1 30); do
  (echo > "/dev/tcp/127.0.0.1/${PROXY_PORT}") 2>/dev/null && break
  sleep 0.5
done
(echo > "/dev/tcp/127.0.0.1/${PROXY_PORT}") 2>/dev/null || {
  echo "✗ proxy didn't start. Log:" >&2; cat "${PROXY_LOG}" >&2; exit 1; }

DB_PASSWORD="$(gcloud secrets versions access latest --secret="${SECRET_DB_PASSWORD}")"
echo "  → GRANT SELECT to ${SA_DB_USER} on public schema of ${TOOLBOX_DB_NAME}..."
PGPASSWORD="${DB_PASSWORD}" psql \
  -h 127.0.0.1 -p "${PROXY_PORT}" \
  -U "${SQL_DB_USER}" -d "${TOOLBOX_DB_NAME}" \
  -v ON_ERROR_STOP=1 \
  -c "GRANT USAGE ON SCHEMA public TO \"${SA_DB_USER}\";" \
  -c "GRANT SELECT ON ALL TABLES IN SCHEMA public TO \"${SA_DB_USER}\";" \
  -c "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO \"${SA_DB_USER}\";" \
  -c "GRANT INSERT ON events TO \"${SA_DB_USER}\";" \
  >/dev/null

kill "${PROXY_PID}" 2>/dev/null || true
wait "${PROXY_PID}" 2>/dev/null || true
trap - EXIT

# ─── 5. Artifact Registry — reuse existing repo ─────────────────────
if ! gcloud artifacts repositories describe "${AR_REPO}" \
       --location="${REGION}" >/dev/null 2>&1; then
  echo "  → creating artifact registry repo ${AR_REPO}"
  gcloud artifacts repositories create "${AR_REPO}" \
    --location="${REGION}" \
    --repository-format=docker \
    --quiet >/dev/null
fi

# ─── 6. Build + push image via Cloud Build (temp build context) ────
BUILD_DIR="$(mktemp -d -t weatherbot-toolbox-build.XXXXXX)"
trap "rm -rf '${BUILD_DIR}'" EXIT
cp "${ROOT_DIR}/agent/Dockerfile.toolbox" "${BUILD_DIR}/Dockerfile"

# Strip the agentContextReference block when no context set is configured —
# otherwise ${WEATHERBOT_GDA_CONTEXT_SET_ID} interpolates to "" inside the
# container and Conversational Analytics rejects it as an invalid resource
# name. Matches the same ⟪AGENT_CONTEXT_BEGIN⟫/⟪AGENT_CONTEXT_END⟫
# markers used by agent/run-toolbox.sh for the local dev path.
if [[ -n "${WEATHERBOT_GDA_CONTEXT_SET_ID:-}" ]]; then
  echo "  → keeping agentContextReference (ctx=${WEATHERBOT_GDA_CONTEXT_SET_ID##*/})"
  cp "${ROOT_DIR}/agent/toolbox.yaml" "${BUILD_DIR}/toolbox.yaml"
else
  echo "  → WEATHERBOT_GDA_CONTEXT_SET_ID unset — stripping agentContextReference"
  awk '
    /# ⟪AGENT_CONTEXT_BEGIN⟫/ { skip = 1 }
    !skip { print }
    /# ⟪AGENT_CONTEXT_END⟫/   { skip = 0 }
  ' "${ROOT_DIR}/agent/toolbox.yaml" > "${BUILD_DIR}/toolbox.yaml"
fi

echo "  → building image (1–2 min)..."
gcloud builds submit "${BUILD_DIR}" \
  --tag="${IMAGE}" \
  --region="${REGION}"

# ─── 7. Cloud Run service ───────────────────────────────────────────
SERVICE_ENV_VARS="PROJECT_ID=${PROJECT_ID}"
SERVICE_ENV_VARS+=",REGION=${REGION}"
SERVICE_ENV_VARS+=",SQL_INSTANCE=${SQL_INSTANCE}"
SERVICE_ENV_VARS+=",SQL_DB_NAME=${TOOLBOX_DB_NAME}"
SERVICE_ENV_VARS+=",SQL_DB_USER=${SQL_DB_USER}"
# Optional — full resource name of the QueryData context set that teaches
# ask_data our schema vocabulary + golden templates. Empty value is OK:
# the toolbox treats agentContextReference.contextSetId="" as "no context"
# and ask_data still runs (but with the 'pool' vs 'Pool' bug intact).
# See agent/context-sets/README.md for the upload workflow.
SERVICE_ENV_VARS+=",WEATHERBOT_GDA_CONTEXT_SET_ID=${WEATHERBOT_GDA_CONTEXT_SET_ID:-}"

# Flags valid on both `run services update` and `run deploy`. The auth
# posture (`--no-allow-unauthenticated`) is settable only on `deploy`;
# on update the existing IAM policy is preserved, so we exclude it.
UPDATE_FLAGS=(
  --region="${REGION}"
  --image="${IMAGE}"
  --service-account="${SA_EMAIL}"
  --port=5000
  --set-env-vars="${SERVICE_ENV_VARS}"
  --set-secrets="WEATHERBOT_DB_PASSWORD=${SECRET_DB_PASSWORD}:latest"
  --ingress=all
  --cpu=1
  --memory=512Mi
  --min-instances=0
  --max-instances=2
  --timeout=60s
  --quiet
)

if gcloud run services describe "${SERVICE_NAME}" \
     --region="${REGION}" >/dev/null 2>&1; then
  echo "  → updating Cloud Run service ${SERVICE_NAME}"
  gcloud run services update "${SERVICE_NAME}" "${UPDATE_FLAGS[@]}" >/dev/null
else
  echo "  → creating Cloud Run service ${SERVICE_NAME}"
  gcloud run deploy "${SERVICE_NAME}" "${UPDATE_FLAGS[@]}" \
    --no-allow-unauthenticated >/dev/null
fi

URL="$(gcloud run services describe "${SERVICE_NAME}" \
        --region="${REGION}" \
        --format='value(status.url)')"

cat <<EOF

✓ Deployed.

  service URL:  ${URL}
  ingress:      all (IAM-only auth, no public access)
  image:        ${IMAGE}
  SA:           ${SA_EMAIL}

Verify (should return 403 unauthenticated, 200 with identity token):

  curl -s -o /dev/null -w "%{http_code}\\n" "${URL}/"
  curl -s -o /dev/null -w "%{http_code}\\n" \\
       -H "Authorization: Bearer \$(gcloud auth print-identity-token)" \\
       "${URL}/"

Tail logs:

  gcloud logging read 'resource.type="cloud_run_revision"
      resource.labels.service_name="${SERVICE_NAME}"' \\
    --limit=30 --freshness=10m --format='value(timestamp,jsonPayload.message)'
EOF
