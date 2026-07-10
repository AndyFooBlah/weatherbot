#!/usr/bin/env bash
# scripts/setup-gda-grpc.sh — Bootstrap the venv that invoke-gda-grpc.py uses.
# Idempotent: skips creation if the venv already has the package installed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${SCRIPT_DIR}/.gda-grpc-venv"

if [[ ! -d "${VENV}" ]]; then
  echo "→ Creating venv at ${VENV}..."
  python3 -m venv "${VENV}"
fi

if "${VENV}/bin/python3" -c "import google.cloud.geminidataanalytics_v1beta" 2>/dev/null; then
  echo "✓ google-cloud-geminidataanalytics already installed in venv."
else
  echo "→ Installing google-cloud-geminidataanalytics + google-auth..."
  "${VENV}/bin/pip" install --quiet --upgrade pip
  "${VENV}/bin/pip" install --quiet google-cloud-geminidataanalytics google-auth
fi

echo
echo "Run the gRPC test:"
echo "  ${VENV}/bin/python3 ${SCRIPT_DIR}/invoke-gda-grpc.py"
echo
echo "Or as the toolbox SA:"
echo "  IMPERSONATE_SA=weatherbot-toolbox-sa@\${PROJECT_ID}.iam.gserviceaccount.com \\"
echo "    ${VENV}/bin/python3 ${SCRIPT_DIR}/invoke-gda-grpc.py"
