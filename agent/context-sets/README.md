# QueryData Context Sets

This directory holds the JSON context-set file that QueryData (the
Conversational Analytics API behind our `ask_data` MCP tool) uses to learn
the weatherbot schema, our enum vocabularies, and our most common query
shapes.

## Why we need this

QueryData generates SQL from natural language. Without a context set it
sees only the bare table schemas — column names and types. That's not
enough to handle questions like:

> Count of days in the last two weeks when the pool temperature was over 90

The planner confidently emitted:

```sql
WHERE "s"."physical_location" = 'pool'    -- lowercase: zero rows
```

…because nothing told it the column actually stores `'Pool'` (title case).
A *value search* in our context set fixes this: when the user says "pool",
QueryData's planner first runs a trigram-fuzzy lookup against
`sensors.physical_location` and substitutes the real enum value into the
WHERE clause.

The full context-set shape is documented in the Google
[Context Engineering Agent plugin][cea] (`db-context-engineering` on the
Claude Code marketplace). It supports three kinds of items:

* **value_searches** — case-insensitive / fuzzy / semantic lookups that
  canonicalize user vocabulary before slotting into SQL. Fixes the
  `'pool'` vs `'Pool'` class of bug.
* **templates** — verified NL→SQL pairs the planner can imitate.
  Especially important for our timezone-sensitive aggregates (we have to
  remind QueryData that "yesterday" is a local-calendar concept against
  UTC-stored timestamps).
* **facets** — reusable WHERE-clause fragments (the planner can compose
  them into a larger query).

## Prerequisites

The trigram value_searches in `weatherbot-narrow.json` use Postgres's
`pg_trgm` extension (`%` similarity operator, `<->` distance,
`gist_trgm_ops` indexes).

Apply migration **003** before uploading the context set:

```bash
bash db/migrate.sh
```

This creates the extension and three GiST trigram indexes on the
`sensors` catalog (`physical_location`, `measurement_type`,
`display_name`). The catalog is small (~40 rows) — the indexes cost
basically nothing.

## Upload workflow

Context-set upload is currently console-only — Google has not shipped a
`gcloud` or REST API for it yet (verified 2026-06-18).

1. **Open Cloud SQL Studio** for the weatherbot instance:
   `gcloud sql instances describe ${SQL_INSTANCE}` to confirm the name,
   then open
   <https://console.cloud.google.com/sql/instances> → click the instance
   → **Studio** in the left rail.

2. **Switch to Data Agents → Context Sets** in Studio.

3. **Create a new context set** (give it a stable ID like
   `weatherbot-narrow-v1`; you'll need it for `toolbox.yaml`).

4. **Upload `weatherbot-narrow.json`** via the Browse picker.

5. **Copy the full resource name** displayed after upload — it looks
   like
   ```
   projects/${PROJECT_ID}/locations/us-central1/contextSets/weatherbot-narrow-v1
   ```

6. **Wire it into `agent/toolbox.yaml`** by adding
   `agentContextReference.contextSetId` under the `ask_data` tool's
   `context.datasourceReferences.cloudSqlReference`. The relevant block
   becomes:
   ```yaml
   ask_data:
     kind: cloud-gemini-data-analytics-query
     source: weatherbot-gda
     # …description, location…
     context:
       datasourceReferences:
         cloudSqlReference:
           databaseReference:
             projectId:  ${PROJECT_ID}
             region:     ${REGION}
             instanceId: ${SQL_INSTANCE}
             databaseId: ${SQL_DB_NAME}
             engine:     POSTGRESQL
             tableIds:
               - stations
               - sensors
               - sensor_readings
               - unmonitored_locations
           agentContextReference:
             contextSetId: projects/${PROJECT_ID}/locations/${REGION}/contextSets/weatherbot-narrow-v1
   ```

7. **Redeploy the toolbox**:
   ```bash
   bash infra/05-deploy-toolbox.sh
   ```

8. **Verify** with the failing query:
   ```bash
   bash scripts/invoke-tool.sh ask_data \
     '{"query":"Count of days in the last two weeks when the pool temperature was over 90"}'
   ```
   You should see a non-zero count and a SQL plan that filters by
   `physical_location = 'Pool'` (title case).

## Iterating on the context set

The current file was authored by hand against the patterns published in
the Context Engineering Agent's `context-generation-guide` skill. For a
fuller iterative tuning loop (bootstrap from schema → evaluate → hill-
climb against a golden dataset), install the plugin and use its
workflow:

```bash
claude plugin marketplace add https://github.com/GoogleCloudPlatform/db-context-enrichment.git
claude plugin install db-context-engineering@db-context-enrichment-marketplace
# Restart Claude Code so the skills load, then in a fresh session:
/autoctx-init        # scaffolds an autoctx/ workspace + tools.yaml
/autoctx-bootstrap   # generates a baseline context from the schema
/autoctx-evaluate    # measures NL→SQL accuracy against a golden set
/autoctx-hillclimb   # iteratively improves the context
```

The plugin's MCP toolbox config (`autoctx/tools.yaml`) needs the same
Cloud SQL connection details as `agent/toolbox.yaml`. The plugin uses
ADC (Application Default Credentials), so make sure you've run
`gcloud auth application-default login` in advance.

## Related files

* `db/migrations/003_pg_trgm_for_value_search.sql` — enables `pg_trgm`
  and GiST indexes the value_searches depend on.
* `agent/toolbox.yaml` — `ask_data` tool definition; needs the
  `agentContextReference.contextSetId` field after upload.
* `scripts/invoke-tool.sh` — invokes any toolbox tool from the command
  line; the regression check for this fix.

[cea]: https://github.com/GoogleCloudPlatform/db-context-enrichment
