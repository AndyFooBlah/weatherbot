#!/usr/bin/env bash
# scripts/invoke-gda.sh — Call Gemini Data Analytics QueryData directly,
# bypassing the MCP Toolbox entirely. Lets us see the *raw* API response
# (HTTP status, body, full error details) without any toolbox wrapping
# or transformation.
#
# Built for the ask_data PermissionDenied debugging round: when the
# toolbox surfaces "[PERMISSION_DENIED] An internal backend error
# occurred", we want to see whether the GDA API itself returns the same
# error when called with the *same* request shape, and what the
# uncondensed error structure looks like.
#
# Wire format verified against the v1beta proto in
# googleapis/googleapis: google/cloud/geminidataanalytics/v1beta/
#   data_chat_service.proto  →  rpc QueryData → POST /v1beta/{parent}:queryData
#                                QueryDataRequest{parent, prompt, context, generation_options}
#   datasource.proto         →  CloudSqlReference{database_reference: CloudSqlDatabaseReference{engine,projectId,region,instanceId,databaseId,...}}
#
# Usage:
#   bash scripts/invoke-gda.sh ['<natural-language query>']
#
# Defaults to "what was the lowest pool temperature in the last week?"
#
# Overridable env vars:
#   PROJECT_ID, REGION, SQL_INSTANCE, SQL_DB_NAME   (default from infra/env.sh)
#   GDA_ENDPOINT     (default: geminidataanalytics.googleapis.com)
#   GDA_API_VERSION  (default: v1beta — matches what MCP Toolbox uses)
#   IMPERSONATE_SA   (optional: impersonate this service account, e.g. the
#                    toolbox SA, so we make the request *as* the toolbox.
#                    e.g. weatherbot-toolbox-sa@<your-project-id>.iam.gserviceaccount.com)
#
# Prereqs:
#   - gcloud authenticated as a principal with
#     roles/geminidataanalytics.queryDataUser on the project (or
#     impersonating one).
#   - curl, python3

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Pull defaults from infra/env.sh so the request mirrors what the
# toolbox would issue.
if [[ -f "${REPO_ROOT}/infra/env.sh" ]]; then
  # shellcheck source=/dev/null
  source "${REPO_ROOT}/infra/env.sh"
fi

PROJECT_ID="${PROJECT_ID:?PROJECT_ID not set — source infra/env.sh first}"
REGION="${REGION:-us-central1}"
SQL_INSTANCE="${SQL_INSTANCE:-weatherbot-db}"
SQL_DB_NAME="${SQL_DB_NAME:-weatherbot}"

GDA_ENDPOINT="${GDA_ENDPOINT:-geminidataanalytics.googleapis.com}"
GDA_API_VERSION="${GDA_API_VERSION:-v1beta}"

QUERY="${1:-what was the lowest pool temperature in the last week?}"

# --- Mint an OAuth2 access token (NOT an identity token — GDA is a
#     Google API, not a Cloud Run service). Either gcloud's current
#     identity, or impersonate a service account (so we call as the
#     toolbox SA and reproduce exactly what toolbox would send).
if [[ -n "${IMPERSONATE_SA:-}" ]]; then
  TOKEN="$(gcloud auth print-access-token \
            --impersonate-service-account="${IMPERSONATE_SA}" \
            2>/dev/null || true)"
  IDENTITY="${IMPERSONATE_SA} (impersonated)"
else
  TOKEN="$(gcloud auth print-access-token 2>/dev/null || true)"
  IDENTITY="$(gcloud config get-value account 2>/dev/null || echo unknown)"
fi

if [[ -z "${TOKEN}" ]]; then
  echo "✗ Could not mint access token. Check 'gcloud auth' / impersonation perms." >&2
  exit 1
fi

URL="https://${GDA_ENDPOINT}/${GDA_API_VERSION}/projects/${PROJECT_ID}/locations/${REGION}:queryData"

# --- Build the request body --------------------------------------------------
# Exact shape from data_chat_service.proto's QueryDataRequest message,
# verified to match agent/toolbox.yaml's ask_data config.
REQUEST_BODY="$(PROJECT_ID="${PROJECT_ID}" REGION="${REGION}" \
                SQL_INSTANCE="${SQL_INSTANCE}" SQL_DB_NAME="${SQL_DB_NAME}" \
                QUERY="${QUERY}" python3 <<'PYEOF'
import json, os

project    = os.environ["PROJECT_ID"]
region     = os.environ["REGION"]
instance   = os.environ["SQL_INSTANCE"]
database   = os.environ["SQL_DB_NAME"]
query      = os.environ["QUERY"]

body = {
    "parent": f"projects/{project}/locations/{region}",
    "prompt": query,
    "context": {
        "datasourceReferences": {
            "cloudSqlReference": {
                "databaseReference": {
                    "engine":     "POSTGRESQL",
                    "projectId":  project,
                    "region":     region,
                    "instanceId": instance,
                    "databaseId": database,
                },
            },
        },
    },
    "generationOptions": {
        "generateQueryResult": True,
        "generateNaturalLanguageAnswer": True,
        "generateExplanation": True,
    },
}
print(json.dumps(body, indent=2))
PYEOF
)"

echo "→ POST ${URL}"
echo "  query:    ${QUERY}"
echo "  project:  ${PROJECT_ID}"
echo "  region:   ${REGION}"
echo "  instance: ${SQL_INSTANCE}"
echo "  database: ${SQL_DB_NAME}"
echo "  identity: ${IDENTITY}"
echo
echo "── Request body ──"
echo "${REQUEST_BODY}"
echo

# --- Fire ---------------------------------------------------------------------
OUT="$(mktemp)"
HDR="$(mktemp)"
trap "rm -f '${OUT}' '${HDR}'" EXIT

start_ms="$(python3 -c 'import time; print(int(time.time()*1000))')"
HTTP_STATUS="$(curl -sS \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -H "X-Goog-User-Project: ${PROJECT_ID}" \
  -X POST \
  -d "${REQUEST_BODY}" \
  -D "${HDR}" \
  -w '%{http_code}' \
  -o "${OUT}" \
  "${URL}")"
end_ms="$(python3 -c 'import time; print(int(time.time()*1000))')"
DURATION_MS=$((end_ms - start_ms))

echo "← HTTP ${HTTP_STATUS}  (${DURATION_MS}ms)"
echo
echo "── Response headers ──"
cat "${HDR}"
echo
echo "── Raw response body (unmodified bytes) ──"
cat "${OUT}"
echo
echo

# --- Try to pretty-print + decode --------------------------------------------
BODY_FILE="${OUT}" python3 <<'PYEOF'
import json, os, sys

path = os.environ["BODY_FILE"]
with open(path) as f:
    raw = f.read().strip()

if not raw:
    print("── Empty response body ──")
    sys.exit(0)

def try_json(s):
    try:
        return json.loads(s)
    except Exception:
        return None

parsed = try_json(raw)
if parsed is None:
    # Try NDJSON.
    parts = []
    for line in raw.splitlines():
        line = line.strip()
        if not line: continue
        v = try_json(line)
        if v is not None:
            parts.append(v)
    if parts:
        parsed = parts

if parsed is None:
    print("── Could not parse response as JSON ──")
    sys.exit(0)

print("── Pretty-printed JSON ──")
print(json.dumps(parsed, indent=2))
print()

# Surface any error blocks prominently — including nested ones in arrays.
def walk_for_errors(node, path="$"):
    found = []
    if isinstance(node, dict):
        if "error" in node and isinstance(node["error"], dict):
            err = node["error"]
            if "code" in err or "message" in err or "status" in err:
                found.append((path + ".error", err))
        for k, v in node.items():
            found.extend(walk_for_errors(v, f"{path}.{k}"))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found.extend(walk_for_errors(v, f"{path}[{i}]"))
    return found

errors = walk_for_errors(parsed)
if errors:
    print("── Detected error blocks ──")
    for path, err in errors:
        print(f"  at {path}:")
        print("    " + json.dumps(err, indent=2).replace("\n", "\n    "))
else:
    # Success path — summarize the QueryDataResponse fields.
    print("── QueryDataResponse summary ──")
    r = parsed if isinstance(parsed, dict) else (parsed[0] if parsed else {})
    if "generatedQuery" in r:
        print("  generatedQuery:")
        for line in r["generatedQuery"].splitlines():
            print(f"    {line}")
    if "intentExplanation" in r:
        print(f"  intentExplanation: {r['intentExplanation']}")
    if "naturalLanguageAnswer" in r:
        print(f"  naturalLanguageAnswer: {r['naturalLanguageAnswer']}")
    if "queryResult" in r:
        qr = r["queryResult"]
        rows = qr.get("rows") or qr.get("data") or []
        print(f"  queryResult: {len(rows)} row(s)")
        for row in rows[:5]:
            print(f"    {json.dumps(row)}")
        if len(rows) > 5:
            print(f"    … ({len(rows) - 5} more)")
PYEOF
