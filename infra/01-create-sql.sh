#!/usr/bin/env bash
# Create Cloud SQL Postgres instance + database + app user.
# Generates a random app-user password and stores it in Secret Manager.
# Idempotent — safe to re-run.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env.sh
source "${SCRIPT_DIR}/env.sh"

gcloud config set project "${PROJECT_ID}" >/dev/null

# --- 1. Instance ----------------------------------------------------------
if gcloud sql instances describe "${SQL_INSTANCE}" >/dev/null 2>&1; then
  echo "✓ Cloud SQL instance ${SQL_INSTANCE} already exists, skipping create."
else
  echo "→ Creating Cloud SQL instance ${SQL_INSTANCE} (this takes 5–10 min)..."
  gcloud sql instances create "${SQL_INSTANCE}" \
    --database-version="${SQL_VERSION}" \
    --tier="${SQL_TIER}" \
    --region="${REGION}" \
    --storage-size=10GB \
    --storage-type=SSD \
    --storage-auto-increase \
    --backup \
    --backup-start-time=09:00 \
    --availability-type=zonal \
    --edition=ENTERPRISE
fi

# --- 1b. Enable Cloud SQL Data API (needed for Gemini Data Analytics QueryData) -
#         Safe to re-apply; gcloud no-ops if already set.
echo "→ Ensuring Cloud SQL Data API access is enabled..."
gcloud sql instances patch "${SQL_INSTANCE}" \
  --data-api-access=ALLOW_DATA_API \
  --quiet >/dev/null

# --- 1c. Require TLS + client cert for all connections.
#         All our access paths (Cloud SQL Auth Proxy, Cloud SQL Python Connector,
#         MCP Toolbox's cloud-sql-postgres source, Cloud SQL Data API) negotiate
#         client certs automatically. Blocks any accidental raw-TCP connection
#         straight to the public IP.
echo "→ Ensuring SSL mode = TRUSTED_CLIENT_CERTIFICATE_REQUIRED..."
gcloud sql instances patch "${SQL_INSTANCE}" \
  --ssl-mode=TRUSTED_CLIENT_CERTIFICATE_REQUIRED \
  --quiet >/dev/null

# --- 2. Database ----------------------------------------------------------
if gcloud sql databases describe "${SQL_DB_NAME}" --instance="${SQL_INSTANCE}" >/dev/null 2>&1; then
  echo "✓ Database ${SQL_DB_NAME} already exists, skipping create."
else
  echo "→ Creating database ${SQL_DB_NAME}..."
  gcloud sql databases create "${SQL_DB_NAME}" --instance="${SQL_INSTANCE}"
fi

# --- 3. Password secret ---------------------------------------------------
if gcloud secrets describe "${SECRET_DB_PASSWORD}" >/dev/null 2>&1; then
  echo "✓ Secret ${SECRET_DB_PASSWORD} already exists, reusing existing password."
  DB_PASSWORD="$(gcloud secrets versions access latest --secret="${SECRET_DB_PASSWORD}")"
else
  echo "→ Generating random app-user password and storing in Secret Manager..."
  DB_PASSWORD="$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 32)"
  printf '%s' "${DB_PASSWORD}" \
    | gcloud secrets create "${SECRET_DB_PASSWORD}" \
        --replication-policy=automatic \
        --data-file=-
fi

# --- 4. App user ----------------------------------------------------------
if gcloud sql users list --instance="${SQL_INSTANCE}" --format="value(name)" \
     | grep -qx "${SQL_DB_USER}"; then
  echo "✓ User ${SQL_DB_USER} exists; ensuring password matches secret..."
  gcloud sql users set-password "${SQL_DB_USER}" \
    --instance="${SQL_INSTANCE}" \
    --password="${DB_PASSWORD}" >/dev/null
else
  echo "→ Creating app user ${SQL_DB_USER}..."
  gcloud sql users create "${SQL_DB_USER}" \
    --instance="${SQL_INSTANCE}" \
    --password="${DB_PASSWORD}" >/dev/null
fi

# --- 5. Print connection info --------------------------------------------
CONNECTION_NAME="$(gcloud sql instances describe "${SQL_INSTANCE}" \
                     --format='value(connectionName)')"

cat <<EOF

✓ Cloud SQL ready.
    instance        : ${SQL_INSTANCE}
    database        : ${SQL_DB_NAME}
    user            : ${SQL_DB_USER}
    password secret : ${SECRET_DB_PASSWORD} (latest version)
    connection name : ${CONNECTION_NAME}

Next: connect via cloud-sql-proxy and apply schema. E.g.:
    cloud-sql-proxy ${CONNECTION_NAME} &
    PGPASSWORD="\$(gcloud secrets versions access latest --secret=${SECRET_DB_PASSWORD})" \\
      psql -h 127.0.0.1 -U ${SQL_DB_USER} -d ${SQL_DB_NAME} -f db/schema.sql

(A db/migrate.sh wrapper for this is coming next.)
EOF
