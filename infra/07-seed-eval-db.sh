#!/usr/bin/env bash
# Seed (or re-seed) the eval database `weatherbot_eval` from a fresh prod
# snapshot. Destructive to weatherbot_eval only; prod is read-only here.
# Re-run on demand whenever evals want fresh data (weatherbot#15).
#
# Pipeline:
#   1. Server-side export of the prod DB to GCS (no laptop round-trip —
#      the DB is ~3 GB) and import into a recreated weatherbot_eval.
#   2. Deterministic TIME SHIFT: every timestamptz column in every real
#      table is shifted forward by a WHOLE number of days so the newest
#      sensor reading lands within the last 24h of the seed run. Whole
#      days preserve local time-of-day (diurnal patterns stay realistic;
#      "yesterday afternoon" questions behave like prod).
#   3. Synthetic fixtures (db/eval_fixtures.sql): a known sensor gap and
#      known events at fixed offsets from "now", for estimate and
#      event-recall eval cases with computable ground truth.
#   4. eval_meta bookkeeping + grants for the toolbox SA (grants must be
#      re-applied every seed because the schema is recreated).
#
# Prereqs: gcloud (authed), cloud-sql-proxy, psql.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=env.sh
source "${SCRIPT_DIR}/env.sh"

EVAL_DB="${EVAL_DB:-weatherbot_eval}"
BUCKET="gs://${PROJECT_ID}-sql-exports"
STAMP="$(date +%Y%m%d-%H%M%S)"
EXPORT_URI="${BUCKET}/eval-seed/${SQL_DB_NAME}-${STAMP}.sql.gz"

gcloud config set project "${PROJECT_ID}" >/dev/null

echo "→ Seeding ${EVAL_DB} from ${SQL_DB_NAME} (instance ${SQL_INSTANCE})"

# ─── 1. Export bucket + instance-SA write access ─────────────────────
if ! gsutil ls -b "${BUCKET}" >/dev/null 2>&1; then
  echo "  → creating bucket ${BUCKET}"
  gsutil mb -l "${REGION}" "${BUCKET}" >/dev/null
fi
INSTANCE_SA="$(gcloud sql instances describe "${SQL_INSTANCE}" \
                 --format='value(serviceAccountEmailAddress)')"
gsutil iam ch "serviceAccount:${INSTANCE_SA}:roles/storage.objectAdmin" \
  "${BUCKET}" >/dev/null

# ─── 2. Server-side export of prod DB ────────────────────────────────
echo "  → exporting ${SQL_DB_NAME} to ${EXPORT_URI} (a few minutes)..."
gcloud sql export sql "${SQL_INSTANCE}" "${EXPORT_URI}" \
  --database="${SQL_DB_NAME}" --quiet

# ─── 3. Recreate eval DB and import ──────────────────────────────────
if gcloud sql databases describe "${EVAL_DB}" \
     --instance="${SQL_INSTANCE}" >/dev/null 2>&1; then
  echo "  → dropping existing ${EVAL_DB}"
  gcloud sql databases delete "${EVAL_DB}" \
    --instance="${SQL_INSTANCE}" --quiet
fi
echo "  → creating ${EVAL_DB}"
gcloud sql databases create "${EVAL_DB}" --instance="${SQL_INSTANCE}" --quiet

echo "  → importing snapshot into ${EVAL_DB} (a few minutes)..."
gcloud sql import sql "${SQL_INSTANCE}" "${EXPORT_URI}" \
  --database="${EVAL_DB}" --user="${SQL_DB_USER}" --quiet

echo "  → deleting snapshot object"
gsutil rm "${EXPORT_URI}" >/dev/null

# ─── 4. Proxy up for the transform phase ─────────────────────────────
PROXY_PORT="${PROXY_PORT:-15435}"
PROXY_LOG="$(mktemp -t weatherbot-eval-proxy.XXXXXX.log)"
cloud-sql-proxy --quota-project "${PROJECT_ID}" --port "${PROXY_PORT}" \
  "${CONNECTION_NAME}" >"${PROXY_LOG}" 2>&1 &
PROXY_PID=$!
trap 'kill "${PROXY_PID}" 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do
  (echo > "/dev/tcp/127.0.0.1/${PROXY_PORT}") 2>/dev/null && break
  sleep 0.5
done

DB_PASSWORD="$(gcloud secrets versions access latest --secret="${SECRET_DB_PASSWORD}")"
PSQL=(psql -h 127.0.0.1 -p "${PROXY_PORT}" -U "${SQL_DB_USER}" -d "${EVAL_DB}" \
      -X -v ON_ERROR_STOP=1)

# ─── 5. Whole-day time shift across every timestamptz column ─────────
echo "  → time-shifting all timestamptz columns (whole days)..."
PGPASSWORD="${DB_PASSWORD}" "${PSQL[@]}" <<'SQL'
DO $$
DECLARE
  shift_days integer;
  prod_max   timestamptz;
  rec        record;
BEGIN
  SELECT max(observed_at) INTO prod_max FROM sensor_readings;
  shift_days := floor(extract(epoch FROM (now() - prod_max)) / 86400)::int;
  RAISE NOTICE 'newest reading %, shifting all timestamps by % days',
    prod_max, shift_days;

  FOR rec IN
    SELECT c.table_name, c.column_name
      FROM information_schema.columns c
      JOIN pg_class pc ON pc.relname = c.table_name
      JOIN pg_namespace pn ON pn.oid = pc.relnamespace
                          AND pn.nspname = c.table_schema
     WHERE c.table_schema = 'public'
       AND c.data_type = 'timestamp with time zone'
       AND pc.relkind = 'r'          -- real tables only, not views
  LOOP
    EXECUTE format(
      'UPDATE %I SET %I = %I + make_interval(days => %s) WHERE %I IS NOT NULL',
      rec.table_name, rec.column_name, rec.column_name, shift_days,
      rec.column_name);
  END LOOP;

  CREATE TABLE IF NOT EXISTS eval_meta (
    seeded_at        timestamptz NOT NULL DEFAULT now(),
    shift_days       integer     NOT NULL,
    prod_max_obs     timestamptz NOT NULL,
    note             text
  );
  INSERT INTO eval_meta (shift_days, prod_max_obs, note)
  VALUES (shift_days, prod_max,
          'seeded by infra/07-seed-eval-db.sh — timestamps are prod + shift_days');
END $$;
SQL

# ─── 6. Synthetic fixtures ───────────────────────────────────────────
echo "  → applying synthetic fixtures..."
PGPASSWORD="${DB_PASSWORD}" "${PSQL[@]}" \
  -f "${ROOT_DIR}/db/eval_fixtures.sql"

# ─── 7. Grants for the toolbox SA (schema was recreated) ─────────────
SA_DB_USER="weatherbot-toolbox-sa@${PROJECT_ID}.iam"
echo "  → grants for ${SA_DB_USER}..."
PGPASSWORD="${DB_PASSWORD}" "${PSQL[@]}" \
  -c "GRANT CONNECT ON DATABASE ${EVAL_DB} TO \"${SA_DB_USER}\";" \
  -c "GRANT USAGE ON SCHEMA public TO \"${SA_DB_USER}\";" \
  -c "GRANT SELECT ON ALL TABLES IN SCHEMA public TO \"${SA_DB_USER}\";" \
  -c "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO \"${SA_DB_USER}\";" \
  -c "GRANT INSERT ON events TO \"${SA_DB_USER}\";" \
  >/dev/null

# ─── 8. Sanity report ────────────────────────────────────────────────
PGPASSWORD="${DB_PASSWORD}" "${PSQL[@]}" -t \
  -c "SELECT 'readings: ' || count(*) || ', newest: ' || max(observed_at) FROM sensor_readings;" \
  -c "SELECT 'events: ' || count(*) FROM events;" \
  -c "SELECT 'meta: shift=' || shift_days || 'd seeded=' || seeded_at FROM eval_meta ORDER BY seeded_at DESC LIMIT 1;"

echo "✓ ${EVAL_DB} seeded."
