#!/usr/bin/env bash
# Enable the prerequisites for Conversational Analytics / Gemini Data Analytics
# (QueryData) to call Cloud SQL Postgres:
#
#   1. IAM database authentication on the instance (requires a brief restart)
#   2. Your active gcloud account as a Cloud SQL IAM database user
#   3. SELECT privileges for that user on the public schema
#   4. Project IAM role roles/geminidataanalytics.dataAgentUser on the user
#   5. Project IAM role roles/serviceusage.serviceUsageConsumer on the user
#
# Idempotent — safe to re-run.
#
# ─── IMPORTANT: serviceusage.services.use requirement ──────────────────────
# ANY principal that calls Gemini Data Analytics QueryData (whether a human
# user via gcloud impersonation or a service account from Cloud Run / GKE /
# anywhere else) needs serviceusage.services.use permission on the project
# the query targets — typically via roles/serviceusage.serviceUsageConsumer.
#
# QueryData's backend uses the *caller's* identity for the downstream call
# to Cloud SQL Data API executeSql, which performs consumer-project
# attribution and requires that permission. Without it, the call fails with:
#
#   PERMISSION_DENIED: Error from Cloud SQL Instance '...':
#     [PERMISSION_DENIED] An internal backend error occurred.
#
# (The client-visible message is wrapped — the actual server-side error is
# "Caller does not have required permission to use project ... Grant the
# caller the roles/serviceusage.serviceUsageConsumer role".)
#
# This script grants the role to your human user. The toolbox service
# account gets the same role from infra/05-deploy-toolbox.sh.
#
# WARNING: step 1 patches database flags. This script preserves the existing
# flag set and only adds cloudsql.iam_authentication=on. The Cloud SQL
# instance will restart (~30–60s downtime for a small instance).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=env.sh
source "${SCRIPT_DIR}/env.sh"

gcloud config set project "${PROJECT_ID}" >/dev/null

USER_EMAIL="$(gcloud config get-value account 2>/dev/null)"
[[ -n "${USER_EMAIL}" ]] || { echo "✗ no active gcloud account; run 'gcloud auth login'" >&2; exit 1; }
echo "→ Active principal: ${USER_EMAIL}"

# ─── 1. Enable IAM database authentication on the instance ──────────────
# Use Python on the JSON output — gcloud's --format=value() can't reliably
# emit "name=value,name=value" pairs for an array. This preserves any
# pre-existing flags the user may have set via console.
INSTANCE_JSON="$(gcloud sql instances describe "${SQL_INSTANCE}" --format=json)"

CURRENT_IAM_FLAG="$(python3 -c '
import json, sys
flags = (json.loads(sys.argv[1]).get("settings") or {}).get("databaseFlags") or []
print(next((f["value"] for f in flags if f["name"] == "cloudsql.iam_authentication"), ""))
' "${INSTANCE_JSON}")"

if [[ "${CURRENT_IAM_FLAG}" == "on" ]]; then
  echo "✓ cloudsql.iam_authentication=on already set"
else
  echo "→ Enabling IAM database authentication (instance will restart briefly)..."
  NEW_FLAGS="$(python3 -c '
import json, sys
flags = (json.loads(sys.argv[1]).get("settings") or {}).get("databaseFlags") or []
merged = {f["name"]: f["value"] for f in flags}
merged["cloudsql.iam_authentication"] = "on"
print(",".join(f"{k}={v}" for k, v in merged.items()))
' "${INSTANCE_JSON}")"
  gcloud sql instances patch "${SQL_INSTANCE}" \
    --database-flags="${NEW_FLAGS}" \
    --quiet
fi

# ─── 2. Add user as an IAM DB user ──────────────────────────────────────
if gcloud sql users list --instance="${SQL_INSTANCE}" \
     --format='value(name)' | grep -qx "${USER_EMAIL}"; then
  echo "✓ ${USER_EMAIL} is already a Cloud SQL user"
else
  echo "→ Creating IAM DB user ${USER_EMAIL}..."
  gcloud sql users create "${USER_EMAIL}" \
    --instance="${SQL_INSTANCE}" \
    --type=cloud_iam_user
fi

# ─── 3. Grant SELECT via cloud-sql-proxy + psql as the app user ─────────
command -v cloud-sql-proxy >/dev/null || {
  echo "✗ cloud-sql-proxy not in PATH" >&2; exit 1; }
command -v psql >/dev/null || {
  echo "✗ psql not in PATH" >&2; exit 1; }

CONNECTION_NAME="$(gcloud sql instances describe "${SQL_INSTANCE}" \
                     --format='value(connectionName)')"
PROXY_PORT="${PROXY_PORT:-15433}"
PROXY_LOG="$(mktemp -t weatherbot-proxy.XXXXXX.log)"

echo "→ Starting cloud-sql-proxy on 127.0.0.1:${PROXY_PORT}..."
cloud-sql-proxy --port "${PROXY_PORT}" "${CONNECTION_NAME}" >"${PROXY_LOG}" 2>&1 &
PROXY_PID=$!
trap 'kill "${PROXY_PID}" 2>/dev/null || true; wait "${PROXY_PID}" 2>/dev/null || true' EXIT

for _ in $(seq 1 30); do
  (echo > "/dev/tcp/127.0.0.1/${PROXY_PORT}") 2>/dev/null && break
  sleep 0.5
done
(echo > "/dev/tcp/127.0.0.1/${PROXY_PORT}") 2>/dev/null || {
  echo "✗ proxy didn't come up. Log:" >&2; cat "${PROXY_LOG}" >&2; exit 1; }

DB_PASSWORD="$(gcloud secrets versions access latest --secret="${SECRET_DB_PASSWORD}")"

echo "→ Granting SELECT to ${USER_EMAIL} in database ${SQL_DB_NAME}..."
PGPASSWORD="${DB_PASSWORD}" psql \
  -h 127.0.0.1 -p "${PROXY_PORT}" \
  -U "${SQL_DB_USER}" \
  -d "${SQL_DB_NAME}" \
  -v ON_ERROR_STOP=1 \
  -c "GRANT USAGE ON SCHEMA public TO \"${USER_EMAIL}\";" \
  -c "GRANT SELECT ON ALL TABLES IN SCHEMA public TO \"${USER_EMAIL}\";" \
  -c "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO \"${USER_EMAIL}\";"

# ─── 4. Project IAM roles for GDA QueryData ─────────────────────────────
# See top-of-file note about serviceusage.serviceUsageConsumer — that role
# is the one that explains the "ask_data PERMISSION_DENIED" regression we
# debugged on 2026-06-17/18.
for role in \
  roles/geminidataanalytics.dataAgentUser \
  roles/cloudsql.instanceUser \
  roles/cloudsql.studioUser \
  roles/serviceusage.serviceUsageConsumer; do
  echo "→ Granting ${role} to ${USER_EMAIL}..."
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="user:${USER_EMAIL}" \
    --role="${role}" \
    --condition=None \
    --quiet >/dev/null
done

cat <<EOF

✓ Done. Retry an ask_data question through the agent — no restart needed
  on the agent or toolbox (next call will pick up the new IAM grants).
EOF
