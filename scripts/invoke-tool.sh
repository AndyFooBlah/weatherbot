#!/usr/bin/env bash
# scripts/invoke-tool.sh — Invoke an MCP tool on the deployed weatherbot-toolbox
# Cloud Run service from the command line, with full response logging.
#
# Built for the ask_data debugging round: lets us capture the exact wire
# response, including error fields and the body of any wrapped tool errors,
# that the SPA's callTool Cloud Function would normally receive.
#
# Usage:
#   bash scripts/invoke-tool.sh <tool_name> '<json args>'
#
# Examples:
#   bash scripts/invoke-tool.sh list_stations '{}'
#   bash scripts/invoke-tool.sh latest_observation '{}'
#   bash scripts/invoke-tool.sh list_sensors '{"location_query":"%pool%"}'
#   bash scripts/invoke-tool.sh ask_data \
#     '{"query":"what was the lowest pool temperature in the last week?"}'
#
# Set the toolbox URL first (find yours with `gcloud run services describe`):
#   TOOLBOX_URL=https://your-toolbox.run.app bash scripts/invoke-tool.sh ...
#
# Prereqs:
#   - gcloud authenticated as a principal with roles/run.invoker on the toolbox
#   - curl, python3

set -euo pipefail

TOOLBOX_URL="${TOOLBOX_URL:?TOOLBOX_URL not set — export the Cloud Run URL of your deployed toolbox service}"

if [[ $# -lt 1 ]]; then
  cat <<EOF >&2
Usage: $0 <tool_name> [<json args>]

Examples:
  $0 list_stations '{}'
  $0 latest_observation '{}'
  $0 ask_data '{"query":"what was the lowest pool temperature last week?"}'

To list available tools, run:
  $0 --list
EOF
  exit 1
fi

TOOL_NAME="$1"
TOOL_ARGS="${2:-{\}}"

# Mint an identity token for the toolbox audience.
TOKEN="$(gcloud auth print-identity-token 2>/dev/null || true)"
if [[ -z "${TOKEN}" ]]; then
  echo "✗ Could not mint identity token. Run 'gcloud auth login' first." >&2
  exit 1
fi

call_mcp() {
  local body="$1"
  local out
  out="$(mktemp)"
  local code
  code="$(curl -sS \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -X POST \
    -d "${body}" \
    -w '%{http_code}' \
    -o "${out}" \
    "${TOOLBOX_URL}/mcp")"
  echo "${code}|${out}"
}

# --- Special: --list to show all available tools --------------------------
if [[ "${TOOL_NAME}" == "--list" ]]; then
  resp="$(call_mcp '{"jsonrpc":"2.0","id":"cli-list","method":"tools/list"}')"
  body="${resp#*|}"
  python3 - <<PYEOF
import json
with open("${body}") as f:
    data = json.load(f)
tools = data.get("result", {}).get("tools", [])
print(f"{len(tools)} tools available:\n")
for t in tools:
    req = set(t.get("inputSchema", {}).get("required", []))
    props = t.get("inputSchema", {}).get("properties", {})
    params = ", ".join(
        f"{p}{'*' if p in req else ''}: {props[p].get('type','?')}"
        for p in props
    ) or "(no params)"
    print(f"  • {t['name']}({params})")
PYEOF
  exit 0
fi

# --- Validate args are JSON ------------------------------------------------
if ! TOOL_ARGS_JSON="${TOOL_ARGS}" python3 -c '
import json, os
json.loads(os.environ["TOOL_ARGS_JSON"])
' 2>/dev/null; then
  echo "✗ Invalid JSON in args: ${TOOL_ARGS}" >&2
  exit 1
fi

# --- Build the JSON-RPC request safely (env vars dodge quoting hell) ------
REQUEST="$(TOOL_NAME="${TOOL_NAME}" TOOL_ARGS_JSON="${TOOL_ARGS}" python3 <<'PYEOF'
import json, os, time
req = {
    "jsonrpc": "2.0",
    "id": f"cli-{int(time.time()*1000)}",
    "method": "tools/call",
    "params": {
        "name": os.environ["TOOL_NAME"],
        "arguments": json.loads(os.environ["TOOL_ARGS_JSON"]),
    },
}
print(json.dumps(req))
PYEOF
)"

# --- Print request, fire, capture response -------------------------------
echo "→ POST ${TOOLBOX_URL}/mcp"
echo "  tool:  ${TOOL_NAME}"
echo "  args:  ${TOOL_ARGS}"
echo "  audience: ${TOOLBOX_URL}"
echo

start_ms="$(python3 -c 'import time; print(int(time.time()*1000))')"
resp="$(call_mcp "${REQUEST}")"
end_ms="$(python3 -c 'import time; print(int(time.time()*1000))')"

HTTP_STATUS="${resp%%|*}"
BODY_FILE="${resp#*|}"
trap "rm -f '${BODY_FILE}'" EXIT
DURATION_MS=$((end_ms - start_ms))

echo "← HTTP ${HTTP_STATUS}  (${DURATION_MS}ms)"
echo

# Always print the raw body first — preserves any bytes the JSON pretty-print
# would lose (e.g. trailing whitespace, BOM, partial content on error).
echo "── Raw response (unmodified bytes) ──"
cat "${BODY_FILE}"
echo
echo

# Pretty-print + decoded interpretation.
BODY_FILE="${BODY_FILE}" python3 <<'PYEOF'
import json, os, sys
path = os.environ["BODY_FILE"]
try:
    with open(path) as f:
        data = json.load(f)
except Exception as e:
    print(f"── Could not parse response as JSON ──\n{e}")
    sys.exit(0)

print("── Pretty-printed JSON ──")
print(json.dumps(data, indent=2))
print()

print("── Decoded interpretation ──")
if "error" in data:
    err = data["error"]
    print(f"JSON-RPC error: code={err.get('code')}  message={err.get('message')}")
    if err.get("data"):
        print("  data:", json.dumps(err["data"], indent=2))
elif "result" in data:
    r = data["result"]
    if isinstance(r, dict) and r.get("isError"):
        print("Tool returned isError=true (tool-level error):")
        for c in r.get("content", []):
            print(f"  [{c.get('type')}] {c.get('text','')}")
    else:
        print("Tool returned isError=false (success).")
        contents = r.get("content", []) if isinstance(r, dict) else []
        for c in contents:
            text = c.get("text", "")
            preview = text if len(text) <= 2000 else text[:2000] + f"\n… ({len(text) - 2000} more chars)"
            print(f"  [{c.get('type')}]")
            for line in preview.splitlines():
                print(f"    {line}")
else:
    print("Unexpected response shape:", json.dumps(data, indent=2))
PYEOF
