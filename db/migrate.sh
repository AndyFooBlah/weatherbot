#!/usr/bin/env bash
# Apply db/schema.sql + any pending db/migrations/*.sql to Cloud SQL Postgres.
#
# Bootstrap: db/schema.sql is idempotent (CREATE TABLE IF NOT EXISTS) and is
# applied unconditionally on every run.
#
# Migrations: each db/migrations/NNN_*.sql is applied once and recorded in the
# schema_migrations table. NNN must be zero-padded numeric for sort order.
#
# Each migration file owns its own BEGIN/COMMIT — the runner only invokes psql
# with ON_ERROR_STOP=1 and records the version after successful return.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=../infra/env.sh
source "${ROOT_DIR}/infra/env.sh"

SCHEMA_FILE="${ROOT_DIR}/db/schema.sql"
MIGRATIONS_DIR="${ROOT_DIR}/db/migrations"
PROXY_PORT="${PROXY_PORT:-15432}"
PROXY_LOG="$(mktemp -t weatherbot-proxy.XXXXXX.log)"

# --- Dependency checks --------------------------------------------------
for cmd in cloud-sql-proxy psql gcloud; do
  command -v "${cmd}" >/dev/null || {
    echo "✗ '${cmd}' not found in PATH." >&2
    case "${cmd}" in
      cloud-sql-proxy) echo "  Install: brew install cloud-sql-proxy" >&2 ;;
      psql)            echo "  Install: brew install libpq && brew link --force libpq" >&2 ;;
    esac
    exit 1
  }
done

[[ -f "${SCHEMA_FILE}" ]] || { echo "✗ Schema file not found: ${SCHEMA_FILE}" >&2; exit 1; }

# --- Start proxy --------------------------------------------------------
CONNECTION_NAME="$(gcloud sql instances describe "${SQL_INSTANCE}" \
                     --project="${PROJECT_ID}" \
                     --format='value(connectionName)')"

echo "→ Starting cloud-sql-proxy on 127.0.0.1:${PROXY_PORT} for ${CONNECTION_NAME}"
echo "  (proxy log: ${PROXY_LOG})"
cloud-sql-proxy --port "${PROXY_PORT}" "${CONNECTION_NAME}" \
  >"${PROXY_LOG}" 2>&1 &
PROXY_PID=$!
trap 'kill "${PROXY_PID}" 2>/dev/null || true; wait "${PROXY_PID}" 2>/dev/null || true' EXIT

for _ in $(seq 1 30); do
  (echo > "/dev/tcp/127.0.0.1/${PROXY_PORT}") 2>/dev/null && break
  sleep 0.5
done
if ! (echo > "/dev/tcp/127.0.0.1/${PROXY_PORT}") 2>/dev/null; then
  echo "✗ cloud-sql-proxy did not become ready. Log follows:" >&2
  cat "${PROXY_LOG}" >&2
  exit 1
fi
echo "✓ Proxy ready."

# --- Credentials --------------------------------------------------------
DB_PASSWORD="$(gcloud secrets versions access latest \
                 --project="${PROJECT_ID}" \
                 --secret="${SECRET_DB_PASSWORD}")"
export PGPASSWORD="${DB_PASSWORD}"

psql_run() {
  # station_mac feeds the :'station_mac' seed rows in db/migrations/ — the
  # station MAC lives only in (gitignored) infra/env.sh, never in the SQL.
  psql -h 127.0.0.1 -p "${PROXY_PORT}" \
       -U "${SQL_DB_USER}" -d "${SQL_DB_NAME}" \
       -v ON_ERROR_STOP=1 \
       -v station_mac="${STATION_MAC:?STATION_MAC not set — source infra/env.sh}" "$@"
}

# --- 1. Bootstrap schema -----------------------------------------------
echo "→ Applying ${SCHEMA_FILE} (idempotent)..."
psql_run -f "${SCHEMA_FILE}" >/dev/null

# --- 2. schema_migrations tracking table -------------------------------
psql_run -c "
  CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
  );
" >/dev/null

# --- 3. Apply pending migrations --------------------------------------
if [[ -d "${MIGRATIONS_DIR}" ]]; then
  applied_count=0
  skipped_count=0
  shopt -s nullglob
  for f in "${MIGRATIONS_DIR}"/[0-9]*.sql; do
    version="$(basename "${f}" .sql)"
    if psql_run -tA -c "SELECT 1 FROM schema_migrations WHERE version = '${version}'" \
         | grep -q '^1$'; then
      echo "  ✓ already applied: ${version}"
      skipped_count=$((skipped_count + 1))
      continue
    fi
    echo "  → applying:        ${version}"
    psql_run -f "${f}" >/dev/null
    psql_run -c "INSERT INTO schema_migrations (version) VALUES ('${version}')" >/dev/null
    applied_count=$((applied_count + 1))
  done
  shopt -u nullglob
  echo
  echo "✓ Migrations: ${applied_count} applied, ${skipped_count} already up-to-date."
else
  echo "  (no db/migrations/ directory; only bootstrap schema applied)"
fi

# --- 4. Show table state ---------------------------------------------
echo
echo "→ Tables in ${SQL_DB_NAME}:"
psql_run -c '\dt'

unset PGPASSWORD
echo "✓ Done."
