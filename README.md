# weatherbot

Personal weather data pipeline + tool layer. Pulls observations every five minutes from an [Ambient Weather Network](https://ambientweather.net/) station into Cloud SQL for PostgreSQL, and exposes a curated set of MCP tools (plus Google's Conversational Analytics QueryData) over that data via a private Cloud Run-hosted [MCP Toolbox for Databases](https://github.com/googleapis/mcp-toolbox).

Companion repo: [**`weatherbot-app`**](https://github.com/AndyFooBlah/weatherbot-app) — the mobile-first chat + voice frontend that consumes those tools through Firebase Cloud Functions.

## Status

Production. The full stack is live:

- **Cloud SQL Postgres** holds ~190k observations spanning ~2 years, dual-written to a wide `observations` table (canonical) and a narrow `sensors` / `sensor_readings` / `polls` schema that the tools read, with a tracked migration system.
- **Cloud Run Job** `weatherbot-sync` runs every 5 minutes via Cloud Scheduler, keeping the DB within ~5 min of live AWN data. Catches up automatically after outages up to 24h.
- **Cloud Run service** `weatherbot-toolbox` (private, IAM-gated) speaks MCP and exposes 7 tools: `list_stations`, `latest_observation`, `observations_in_range`, `summarize_period`, `list_sensors`, `list_unmonitored`, plus `ask_data` backed by Gemini Data Analytics' QueryData.
- **Python ADK agent** at `agent/weatherbot_agent/` remains usable for local dev (`bash agent/run-agent.sh`) but is no longer the production agent surface — that role moved to weatherbot-app + Gemini Live.

## Layout

| Path | What |
|---|---|
| `infra/00-enable-apis.sh` | Enables ~13 GCP APIs needed across the stack |
| `infra/01-create-sql.sh` | Cloud SQL Postgres instance + DB + app user + secret. Includes Data API enable + SSL hardening. |
| `infra/02-store-awn-secrets.sh` | Interactive prompt → Secret Manager for AWN API/Application keys |
| `infra/03-enable-gda-access.sh` | IAM DB auth + project IAM bindings for Conversational Analytics QueryData |
| `infra/04-deploy-sync-job.sh` | Builds + deploys the 5-min Cloud Run Job + Cloud Scheduler trigger |
| `infra/05-deploy-toolbox.sh` | Builds + deploys the private MCP Toolbox Cloud Run service |
| `db/schema.sql`, `db/migrations/`, `db/migrate.sh` | Bootstrap schema + per-migration files + idempotent runner with `schema_migrations` tracking |
| `ingest/` | Python package + `weatherbot` CLI: `list-devices`, `peek`, `sync-stations`, `backfill`, `catchup`, `sync`, `rehoist`, `field-stats`. Also the Cloud Run Job container source. |
| `agent/` | Local dev ADK agent + canonical `toolbox.yaml` + Dockerfile.toolbox |
| `docs/` | Design and process notes (e.g. the QueryData context-engineering write-up) |

## Prereqs

- `gcloud` CLI, authenticated as the account that owns your GCP project.
- `psql` (Postgres client) — for migrations and one-off queries.
- `cloud-sql-proxy` — used by `db/migrate.sh` and by Phase 3 IAM-grant scripts.
- `uv` (or Python 3.12+ with venv) — for the `weatherbot` CLI.
- `docker` — **not needed** locally; all container builds run on Cloud Build.

## First-time setup

Create (or pick) a GCP project and make sure billing is linked (`gcloud beta billing projects describe <your-project-id>`).

```bash
gcloud auth login
gcloud auth application-default login
gcloud auth application-default set-quota-project <your-project-id>

cp infra/env.sh.example infra/env.sh   # set PROJECT_ID, STATION_MAC, STATION_BACKFILL_FROM
source infra/env.sh

bash infra/00-enable-apis.sh
bash infra/01-create-sql.sh            # ~7 min to provision
bash db/migrate.sh                     # applies schema.sql + db/migrations/*.sql
bash infra/02-store-awn-secrets.sh     # prompts for AWN keys
bash infra/03-enable-gda-access.sh     # GDA + Cloud SQL IAM auth
bash infra/04-deploy-sync-job.sh       # build + deploy 5-min ingestion
bash infra/05-deploy-toolbox.sh        # build + deploy private toolbox service
```

After that, run an initial backfill from inside `ingest/`:

```bash
cd ingest
uv run weatherbot backfill              # backfills from STATION_BACKFILL_FROM — ~75 min for 2 years
```

The scheduled sync keeps the DB current from there.

## Secrets

All secrets (AWN API key, AWN Application key, Cloud SQL DB password) live in Google Secret Manager — never in the repo, never in client bundles, never in logs. The Cloud Run Job and the toolbox Cloud Run service both mount secrets via `--set-secrets` (no service-account keys anywhere).

## See also

- [GitHub issue tracker](https://github.com/AndyFooBlah/weatherbot/issues) — closed issues describe each numbered phase + the post-launch follow-ups.
