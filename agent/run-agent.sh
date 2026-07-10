#!/usr/bin/env bash
# Launch the ADK Web UI for the weatherbot agent.
# Requires: MCP Toolbox already running on :5000 (start it in another terminal
# with ./run-toolbox.sh), and ADC authenticated to your GCP project.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=../infra/env.sh
source "${ROOT_DIR}/infra/env.sh"

# Use Vertex AI for Gemini (ADC-based; no API key needed).
# Gemini 3.x models are served from the GLOBAL endpoint only — regional endpoints
# like us-central1 return 404 for these. Cloud SQL still lives in us-central1.
export GOOGLE_GENAI_USE_VERTEXAI=TRUE
export GOOGLE_CLOUD_PROJECT="${PROJECT_ID}"
export GOOGLE_CLOUD_LOCATION="${VERTEX_LOCATION:-global}"

# Where the MCP Toolbox is listening.
export TOOLBOX_URL="${TOOLBOX_URL:-http://127.0.0.1:5000}"

cd "${SCRIPT_DIR}"
echo "→ Launching ADK Web — discovers ./weatherbot_agent"
echo "  Once it prints a URL, open it in a browser and chat."
exec uv run adk web .
