#!/usr/bin/env bash
# Store the Ambient Weather Network API Key + Application Key in Secret Manager.
# Prompts interactively with hidden input — keys never appear in shell history,
# script args, environment variables, or terminal scrollback.
# Safe to re-run: adds a new secret version (Secret Manager keeps prior versions).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env.sh
source "${SCRIPT_DIR}/env.sh"

gcloud config set project "${PROJECT_ID}" >/dev/null

prompt_and_store() {
  local secret_name="$1"
  local label="$2"
  local value

  printf '%s: ' "${label}" >&2
  read -rs value
  echo >&2

  if [[ -z "${value}" ]]; then
    echo "✗ Empty input — skipping ${secret_name}." >&2
    return 1
  fi

  if gcloud secrets describe "${secret_name}" >/dev/null 2>&1; then
    echo "→ Adding new version to existing secret ${secret_name}..." >&2
    printf '%s' "${value}" \
      | gcloud secrets versions add "${secret_name}" --data-file=- >/dev/null
  else
    echo "→ Creating secret ${secret_name}..." >&2
    printf '%s' "${value}" \
      | gcloud secrets create "${secret_name}" \
          --replication-policy=automatic \
          --data-file=- >/dev/null
  fi
  echo "✓ Stored ${secret_name} (length=${#value})." >&2
}

cat <<EOF
Paste your AWN keys when prompted. Input is hidden.
Find them at: https://ambientweather.net/account → API Keys

EOF

prompt_and_store "${SECRET_AWN_API_KEY}" "AWN API Key        "
prompt_and_store "${SECRET_AWN_APP_KEY}" "AWN Application Key"

cat <<EOF

✓ Done. Verify any time with:
  gcloud secrets versions access latest --secret=${SECRET_AWN_API_KEY} | wc -c
  gcloud secrets versions access latest --secret=${SECRET_AWN_APP_KEY} | wc -c
EOF
