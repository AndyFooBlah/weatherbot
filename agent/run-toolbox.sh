#!/usr/bin/env bash
# Start the MCP Toolbox server (HTTP on :5000) using the local toolbox binary
# and ./toolbox.yaml. Fetches the DB password from Secret Manager and exports
# it as WEATHERBOT_DB_PASSWORD for the env-var substitution in toolbox.yaml.
#
# Run in its own terminal. Ctrl-C to stop.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=../infra/env.sh
source "${ROOT_DIR}/infra/env.sh"

BIN="${SCRIPT_DIR}/bin/toolbox"
TOOLS_FILE="${SCRIPT_DIR}/toolbox.yaml"

if [[ ! -x "${BIN}" ]]; then
  echo "✗ toolbox binary not found at ${BIN}" >&2
  echo "  Run: bash ${SCRIPT_DIR}/install-toolbox.sh" >&2
  exit 1
fi

if [[ ! -f "${TOOLS_FILE}" ]]; then
  echo "✗ toolbox.yaml not found at ${TOOLS_FILE}" >&2
  exit 1
fi

echo "→ Fetching DB password from Secret Manager..."
WEATHERBOT_DB_PASSWORD="$(gcloud secrets versions access latest \
    --project="${PROJECT_ID}" \
    --secret="${SECRET_DB_PASSWORD}")"
export WEATHERBOT_DB_PASSWORD

# If WEATHERBOT_GDA_CONTEXT_SET_ID isn't set, the ask_data tool's
# agentContextReference.contextSetId would interpolate to "", which the
# Conversational Analytics API rejects ("invalid resource name"). Strip
# the agentContextReference block in that case so ask_data still works
# (just without authored context) until the upload step is done.
# Markers around the block in toolbox.yaml: # ⟪AGENT_CONTEXT_BEGIN⟫ /
# # ⟪AGENT_CONTEXT_END⟫ — see agent/context-sets/README.md.
EFFECTIVE_TOOLS_FILE="${TOOLS_FILE}"
if [[ -z "${WEATHERBOT_GDA_CONTEXT_SET_ID:-}" ]]; then
  EFFECTIVE_TOOLS_FILE="$(mktemp -t weatherbot-toolbox.XXXXXX.yaml)"
  awk '
    /# ⟪AGENT_CONTEXT_BEGIN⟫/ { skip = 1 }
    !skip { print }
    /# ⟪AGENT_CONTEXT_END⟫/   { skip = 0 }
  ' "${TOOLS_FILE}" > "${EFFECTIVE_TOOLS_FILE}"
  echo "  (WEATHERBOT_GDA_CONTEXT_SET_ID unset — stripped agentContextReference)"
  trap 'rm -f "${EFFECTIVE_TOOLS_FILE}"' EXIT
fi

echo "→ Starting MCP Toolbox on http://127.0.0.1:5000 ..."
echo "  (config: ${EFFECTIVE_TOOLS_FILE})"
exec "${BIN}" --config "${EFFECTIVE_TOOLS_FILE}"
