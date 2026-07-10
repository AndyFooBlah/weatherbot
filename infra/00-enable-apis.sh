#!/usr/bin/env bash
# Enable all GCP APIs that weatherbot needs.
# Idempotent — safe to re-run.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env.sh
source "${SCRIPT_DIR}/env.sh"

echo "→ Setting active project to ${PROJECT_ID}"
gcloud config set project "${PROJECT_ID}" >/dev/null

APIS=(
  sqladmin.googleapis.com             # Cloud SQL
  servicenetworking.googleapis.com    # reserved for private-IP if we add it later
  secretmanager.googleapis.com        # AWN keys + DB password
  run.googleapis.com                  # Cloud Run + Cloud Run Jobs
  cloudscheduler.googleapis.com       # Trigger ingest every 15 min
  cloudbuild.googleapis.com           # Build container images
  artifactregistry.googleapis.com     # Store container images
  aiplatform.googleapis.com           # Vertex AI / Gemini / Agent Engine
  geminidataanalytics.googleapis.com  # Conversational Analytics API (QueryData tool)
  cloudaicompanion.googleapis.com     # Gemini for Google Cloud — required dep of geminidataanalytics
  iap.googleapis.com                  # Identity-Aware Proxy for the optional Web UI
  iam.googleapis.com
  compute.googleapis.com
  logging.googleapis.com
  monitoring.googleapis.com
)

echo "→ Enabling ${#APIS[@]} APIs (may take ~30s)..."
gcloud services enable "${APIS[@]}"

echo
echo "→ Verifying each API is enabled:"
ENABLED="$(gcloud services list --enabled --format='value(config.name)')"
fail=0
for api in "${APIS[@]}"; do
  if grep -qx "${api}" <<<"${ENABLED}"; then
    echo "  ✓ ${api}"
  else
    echo "  ✗ ${api}  (NOT ENABLED)"
    fail=1
  fi
done

if [[ $fail -ne 0 ]]; then
  echo
  echo "✗ One or more APIs failed to enable. See above." >&2
  exit 1
fi
echo
echo "✓ All ${#APIS[@]} APIs enabled."
