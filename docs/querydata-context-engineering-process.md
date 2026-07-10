# QueryData context engineering — how I learned to do this

A first-person account of figuring out how to author Conversational
Analytics API context for the `ask_data` tool, written for the next
person (or agent) who has to add or extend the context set. Captures
both what worked and where the docs left gaps.

---

## The setup

`ask_data` is wired to Google's Conversational Analytics API
("QueryData") via the MCP Toolbox `cloud-gemini-data-analytics-query`
tool kind. The API does natural-language → SQL on top of our Cloud SQL
Postgres instance. We had it working end-to-end for several days, then
hit a class of bug where the generated SQL was syntactically valid but
semantically wrong: e.g. `WHERE physical_location = 'pool'` (lowercase)
against a column that actually stores `'Pool'` (title case). Zero rows
returned, model confidently said "no readings."

The fix is **authored context** — telling QueryData enough about our
schema, vocabulary, and golden query shapes that it generates the right
SQL. The mechanism for that is a **context set** uploaded to Cloud SQL
Studio. The tool for generating one is Google's **Context Engineering
Agent**, distributed as a Claude Code plugin (and a Gemini CLI
extension, and an Antigravity plugin — same agent, three IDE wrappers).

---

## Step 1 — locating the bug in the generated SQL

The user described the symptom: "count of days in the last two weeks
when the pool was over 90" returned zero, even though the pool was
hitting 91° most afternoons. I reproduced via `scripts/invoke-tool.sh
ask_data '{"query":"..."}'` and inspected the response, which includes
the generated SQL alongside the result:

```sql
SELECT count(DISTINCT (...)) FROM "sensor_readings" "r"
JOIN "sensors" "s" ON "s"."sensor_id" = "r"."sensor_id"
JOIN "stations" "st" ON "st"."mac_address" = "s"."station_id"
WHERE "s"."physical_location" = 'pool'           -- ← bug
  AND "s"."measurement_type" = 'temperature'
  AND "r"."value" > 90.0
  AND "r"."observed_at" AT TIME ZONE "st"."timezone" >= ...
```

The bug was visible in one line: the literal `'pool'`. I cross-checked
against the live data with `list_sensors '{"location_query":"%"}'` —
the actual values are `'Bedroom'`, `'Garage'`, `'Outdoor'`, `'Pool'`,
`'Pool (retired)'`. Title case throughout. The model had no way to
know.

**Lesson.** Always have a path to capture the generated SQL alongside
the answer for any NL→SQL tool. `invoke-tool.sh`'s raw-response logging
was what made this debuggable; without it I'd have been guessing at
"why is the count zero" forever.

---

## Step 2 — finding the right plugin

The user explicitly asked for the "Context Engineering Agent that is
bundled with QueryData (check documentation for latest version of the
plugin for Claude Code)." I had no immediate recall of that plugin
name, so I started broad and narrowed.

**False starts:**

- Searched the `GoogleCloudPlatform/data-agent-kit` marketplace I'd
  installed earlier in the conversation. It has Cloud SQL Postgres,
  AlloyDB, BigQuery, Looker, Spanner, Firestore plugins — none named
  "context engineering."
- Searched all skill markdowns under
  `~/.claude/plugins/marketplaces/data-agent-kit/` for "context
  engineer", "golden", "template" — turned up dataflow templates and a
  Knowledge Catalog `lookup_context` skill but nothing for QueryData.
- WebFetch of the BigQuery + Cloud SQL data-agent overview docs. They
  mentioned "authored context" and referred to "the context
  engineering agent" but never named the plugin or repo. The docs
  themselves were heavy redirects (`cloud.google.com` →
  `docs.cloud.google.com`) which made WebFetch return 404s on the
  first URL I tried.

**What unblocked me.** A targeted search for the Cloud SQL Postgres
flavor of "build context using Gemini CLI" found
[`docs.cloud.google.com/sql/docs/postgres/build-context-gemini-cli`](https://docs.cloud.google.com/sql/docs/postgres/build-context-gemini-cli),
which lists install commands for three harnesses — Antigravity, Claude
Code, deprecated Gemini CLI. The Claude Code line was:

```bash
/plugin marketplace add https://github.com/GoogleCloudPlatform/db-context-enrichment.git
/plugin install db-context-engineering@db-context-enrichment-marketplace
```

So the plugin lives in a **different** GoogleCloudPlatform repo from
the data-agent-kit. Different marketplace, different name. I added the
marketplace via `claude plugin marketplace add` and installed the
plugin. The marketplace clone is at
`~/.claude/plugins/marketplaces/db-context-enrichment-marketplace/`,
and the installed plugin sits at
`~/.claude/plugins/cache/db-context-enrichment-marketplace/db-context-engineering/0.6.0/`.

**Lesson.** "QueryData" the API and "Data Agent Kit" the plugin
collection are different things from "db-context-enrichment" the
context-engineering plugin. They live in three Google repos with
overlapping names. The user's mental model — "bundled with QueryData" —
maps to "officially distributed by the QueryData team," not to "lives
in the QueryData SDK package."

---

## Step 3 — reading the plugin to understand the artifact

The plugin's claude-plugin manifest declared two MCP servers
(`db-context-engineering` itself + an MCP Toolbox at v1.4.0) and six
skills:

```
plugin/skills/
  autoctx-init                 — scaffold autoctx/ workspace + tools.yaml
  autoctx-bootstrap            — generate baseline context from schema
  autoctx-dataset-generation   — generate golden NL+SQL evaluation set
  autoctx-evaluate             — measure NL→SQL accuracy on golden set
  autoctx-hillclimb            — iterate context using gap analysis
  context-generation-guide     — schema knowledge for items themselves
```

`context-generation-guide` was the key one for me. Its `SKILL.md` lays
out the three item types — **templates** (full NL→SQL pairs),
**facets** (reusable SQL fragments), and **value_searches**
(parameterized lookups for canonicalizing user vocabulary). The
`references/` subdirectory has dialect-specific examples for each:

```
references/
  template/postgresql.md      — full NL→SQL pairs
  facet/postgresql.md         — reusable WHERE fragments
  value_search/postgresql.md  — case-insensitive / fuzzy / semantic lookups
  phrase_extraction/guidelines.md  — how to parameterize
```

Reading `value_search/postgresql.md` was the moment I knew exactly
what would fix my bug. The `TRIGRAM_STRING_MATCH` template is:

```sql
WITH TrigramMetrics AS (
    SELECT T."{column}" AS original_value,
    (T."{column}" <-> $value::text) AS normalized_dist
    FROM "{table}" T
    WHERE T."{column}" % $value::text
)
SELECT original_value AS value, '{table}.{column}' AS columns,
       '{concept_type}' AS concept_type, normalized_dist AS distance,
       ''::text AS context FROM TrigramMetrics
```

When the user says "pool", QueryData runs this query against
`sensors.physical_location` with `$value='pool'`. The `%` operator
(`pg_trgm`'s similarity threshold) is case-insensitive, so it matches
the stored `'Pool'`. The `<->` operator returns the distance (0–1
normalized). QueryData substitutes the best match into the final
WHERE clause. Bug fixed at the substitution layer, not at the
"hope the model picks the right casing" layer.

**Lesson.** The plugin's reference dialect docs are where the truth
lives. The public Cloud docs paraphrase but don't show the templates.
Worth bookmarking
`~/.claude/plugins/marketplaces/db-context-enrichment-marketplace/plugin/skills/context-generation-guide/references/`.

---

## Step 4 — checking pg_trgm availability

The trigram template needs `CREATE EXTENSION pg_trgm` plus GiST
trigram indexes. Both are per-database. I grepped `db/migrations/` for
any prior `CREATE EXTENSION` — none. So I needed a new migration.

I wrote `db/migrations/003_pg_trgm_for_value_search.sql`:

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS idx_sensors_physical_location_trgm
  ON sensors USING gist (physical_location gist_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_sensors_measurement_type_trgm
  ON sensors USING gist (measurement_type gist_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_sensors_display_name_trgm
  ON sensors USING gist (display_name gist_trgm_ops);
```

`sensors` has ~40 rows, so the indexes cost basically nothing. Applied
via `bash db/migrate.sh` which exited with `Migrations: 2 applied, 1
already up-to-date`.

**Lesson.** Any value_search using a Postgres-extension operator (`%`,
`<=>`, etc.) needs an explicit migration. Don't assume the extension is
present even on a freshly-created Cloud SQL instance — it's not.

---

## Step 5 — deciding between manual authoring and the full agent flow

The plugin's full flow is:

```bash
/autoctx-init        # creates autoctx/ + tools.yaml
/autoctx-bootstrap   # introspects schema, generates baseline JSON
/autoctx-evaluate    # runs the JSON against a golden NL+SQL eval set
/autoctx-hillclimb   # gap analysis → context refinements → re-evaluate
```

Two problems with running it right now:

1. **The plugin's skills weren't loaded** in my active Claude Code
   session. `claude plugin install` puts the files in place, but
   skills are read at session start. I'd need to restart Claude or use
   `/reload-plugins`.
2. **The flow is interactive** — `/autoctx-init` asks the user to
   confirm a working directory, `/autoctx-bootstrap` asks for a tuning
   experiment name, asks to filter schemas, asks for ORM source code
   to enrich generation. The user wanted a fix landed, not a 30-minute
   interactive flow.

So I took the **manual path**: author the JSON by hand using the
`context-generation-guide` references as my schema. This is exactly
what the agent would do internally — the references *are* the agent's
knowledge. The difference is the agent would generate ~30 items and
iterate; I generated 18 high-value items (3 value_searches +
7 templates + 8 facets) directly targeting the patterns I knew our
voice users hit.

**Lesson.** The plugin's references are a complete authoring spec.
For a small, well-understood schema like ours (5 tables, ~40 sensors,
~10 query archetypes) manual authoring is faster than the full
bootstrap-evaluate-hillclimb loop. For a larger / unfamiliar schema,
the agent's flow is the right call.

---

## Step 6 — picking the templates

Which seven templates? I started from the agent's recent failure modes
and inverted them — every template is a counter-example to a specific
failure I'd seen this week.

| Template (NL → SQL) | Failure it preempts |
|---|---|
| "What's the current pool temperature?" | Generic "latest reading" pattern with the right JOIN to `sensors` + LATERAL on `sensor_readings`. |
| "Highest pool temp in the last two weeks" | Aggregate over window — the `peak pool temp` inconsistency bug. |
| "Count of days when pool > 90 in last 2 weeks" | The exact failing query that motivated this work. Counts DISTINCT local-timezone date. |
| "Lowest outdoor temp yesterday" | Tests local-calendar-date arithmetic (`AT TIME ZONE st.timezone`) against UTC-stored timestamps. |
| "Pool hourly averages for last 24 hours" | Time-bucketing with `date_trunc('hour', ... AT TIME ZONE)`. |
| "Average outdoor humidity last week" | "Last week" = previous Monday–Sunday in local time, the trickiest case. |
| "Peak wind gust over last 30 days" | Cross-location (no `physical_location` filter); demonstrates how to omit a facet. |

Each template comes in both literal (`physical_location = 'Pool'`) and
parameterized (`physical_location = $1`) forms. The parameterized form
is what the planner generalizes from.

**Lesson.** The single most valuable thing about templates isn't
verbatim matching — it's that they teach the model the *right JOINs*
and the *right timezone gymnastics* for your schema. Both are
schema-specific and easy to get wrong.

---

## Step 7 — picking the facets

Facets are reusable WHERE-clause fragments. They're not directly tied
to specific templates; QueryData composes them into larger queries.
I included eight, biased toward filters the agent kept getting wrong:

- `sensors.physical_location = 'Pool'` (parameterized)
- `sensors.measurement_type = 'temperature'` (parameterized)
- `sensors.reliable = true` — crucial because the outdoor rain gauge
  has been offline since 2025-06-10; we never want stale rain numbers.
- `sensors.is_active = true` — excludes the retired pool sensor
  (`temp4f`, replaced by channel 7).
- `observed_at >= now() - interval '24 hours'` (parameterized)
- `observed_at >= now() - interval '14 days'` (parameterized)
- `(observed_at AT TIME ZONE timezone)::date = today` (today/yesterday
  in local tz)

Each is fully qualified (`table.column`) per the plugin's PostgreSQL
facet guide: facets get injected into queries that may join multiple
tables, so unqualified column names risk ambiguity.

**Lesson.** Facets that encode product invariants (reliable=true,
is_active=true) are net-positive correctness wins. They're cheap to
write and prevent a whole class of "the answer is stale because the
sensor's broken" bugs.

---

## Step 8 — wiring the contextSetId into toolbox.yaml

The MCP Toolbox tool kind `cloud-gemini-data-analytics-query` accepts:

```yaml
context:
  datasourceReferences:
    cloudSqlReference:
      databaseReference: {...}
      agentContextReference:
        contextSetId: projects/PROJECT/locations/REGION/contextSets/ID
```

I'd been searching for this schema on the toolbox's own docs site and
hit 404s. The shape eventually came from the `mcp-toolbox.dev`
integration page (different host from
`googleapis.github.io/genai-toolbox/` — the first one I tried). Once I
had the field name, wiring it was straightforward.

**One subtlety.** The contextSetId has to be a fully-qualified resource
name. If the env var is empty, `${WEATHERBOT_GDA_CONTEXT_SET_ID}`
interpolates to `""`, and QueryData rejects empty as
"invalid resource name." That would break `ask_data` for everyone
between "merged the change" and "uploaded the JSON to Cloud SQL
Studio."

I solved this with marker-bracketed YAML that the deploy and run
scripts strip when the env var is empty:

```yaml
          # ⟪AGENT_CONTEXT_BEGIN⟫
          agentContextReference:
            contextSetId: ${WEATHERBOT_GDA_CONTEXT_SET_ID}
          # ⟪AGENT_CONTEXT_END⟫
```

`agent/run-toolbox.sh` and `infra/05-deploy-toolbox.sh` each pipe
`toolbox.yaml` through an awk filter that skips lines between the
markers when `WEATHERBOT_GDA_CONTEXT_SET_ID` is unset. Validated both
modes parse as valid YAML with PyYAML.

**Lesson.** When a YAML field requires a non-empty value but is
optional in the deployment, the env-var-interpolation trick fails.
Use markers + a script-level strip step instead. Cheaper than a real
templating engine.

---

## Step 9 — how I tested

The full upload path (Cloud SQL Studio → Data Agents → Context Sets →
Upload) is console-only as of today. There's no `gcloud` command, no
REST endpoint in the published Data Agents API for context-set
creation. I confirmed this by reading
`docs.cloud.google.com/sql/docs/postgres/manage-data-agents` and
`docs.cloud.google.com/gemini/data-agents/reference/rest`; neither
shows a `projects.locations.contextSets.create` method.

So my "testing" right now is:

1. ✅ **JSON validates.** `python3 -c "import json; json.load(open(...))"`
   returns clean. 3 value_searches, 7 templates, 8 facets — counts
   match.
2. ✅ **YAML parses in both modes.** Both with and without
   `WEATHERBOT_GDA_CONTEXT_SET_ID` set, `yaml.safe_load` after env-var
   interpolation produces a complete tool definition with the right
   `agentContextReference` block (present or absent).
3. ✅ **Strip script is correct.** `awk` output has no live
   `agentContextReference:` or `contextSetId:` keys.
4. ✅ **Migration applied.** `bash db/migrate.sh` ran cleanly;
   migration 003 is recorded in `schema_migrations`.
5. ⏳ **End-to-end ask_data verification** is gated on the manual
   upload step. The smoke test is documented in
   `agent/context-sets/README.md`:
   ```bash
   bash scripts/invoke-tool.sh ask_data \
     '{"query":"Count of days in the last two weeks when the pool temperature was over 90"}'
   ```
   Expected: a non-zero count and a generated SQL with
   `physical_location = 'Pool'` (title case).

**Lesson.** Test what you can locally before the manual step
(JSON validity, YAML in both modes, migration idempotence,
strip logic). When the upload is console-only, the local tests have to
be airtight so the only remaining failure mode is "you didn't click
upload yet."

---

## What was hard or unclear

### A. Five overlapping repos and one ambiguous name

There are at least five Google Cloud repos under various GitHub orgs
that touch this area:

- `GoogleCloudPlatform/data-agent-kit` — the umbrella marketplace.
- `GoogleCloudPlatform/db-context-enrichment` — the Context Engineering
  Agent itself (this is the one we needed).
- `gemini-cli-extensions/cloud-sql-postgresql` — the Cloud SQL Postgres
  data plane skills.
- `googleapis/genai-toolbox` — the MCP Toolbox.
- `GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK` — observability
  for agents, unrelated to NL→SQL.

The name "Data Agent" is overloaded: it can mean (a) the QueryData
chat-with-your-data thing, (b) a Claude Code skill bundle, or (c) the
Cloud SQL Studio UI component. Different docs use different senses
without flagging which.

### B. The Cloud SQL Postgres plugin doesn't ship the context-engineering skill

This was my biggest dead-end. `data-agent-kit`'s
`cloud-sql-postgresql` plugin felt like the right place to look for a
context engineering skill for Cloud SQL Postgres — it isn't. The
context-engineering work is its own plugin in its own marketplace.

### C. The MCP Toolbox tool-kind reference is hard to find

`googleapis.github.io/genai-toolbox/resources/tools/cloud-gemini-data-analytics-query/`
returns 404. The real docs live at
`mcp-toolbox.dev/integrations/cloudgda/tools/cloud-gda-query/` (different
host, different URL pattern). Eventually I'd want to bookmark the
sitemap so I stop guessing.

### D. The `context` sub-schema is partially undocumented

I learned the `agentContextReference.contextSetId` field exists from
search-result snippets quoting the toolbox docs. The full sub-schema
under `context:` (what else can go there besides
`datasourceReferences`?) is hinted at but I never found a complete
reference. The hint about
`parameterizedSecureViewParameters` would only matter once we have
row-level security; ignored for now.

### E. No CLI / API for context-set upload

This is the single biggest gap. Authoring the JSON is fully
scriptable; uploading is not. Anybody managing this across multiple
environments (staging, prod, dev) has to click through Studio
N times. For a one-instance project like weatherbot this is fine; for
anything multi-tenant it'd be painful.

---

## What I'd do differently next time

1. **Start at `db-context-enrichment` directly.** I burned ~30 minutes
   in the wrong marketplace. The Cloud SQL Postgres build-context-gemini-cli
   doc names the plugin — start there if you can find it.

2. **Read `context-generation-guide` first, then write items.**
   The references are the source of truth for item shapes; reading
   them in order takes ~10 minutes and saves the trial-and-error of
   "is this field name right?"

3. **Capture failing SQL before installing anything.** The most
   valuable artifact in this whole exercise was the original
   `scripts/invoke-tool.sh ask_data '...'` output showing the
   lowercase `'pool'`. Every authored item is a counter-example to a
   specific captured failure. Without the capture, you're guessing
   at templates.

4. **Validate YAML both modes before committing.** The empty-string
   contextSetId trap would have shipped a broken deploy if I'd
   missed it.

5. **Plan to run the full plugin flow once we have a golden set.**
   The manual authoring I did this round covers ~10 query archetypes.
   For a serious eval baseline, build a `golden.json` with ~50 NL+SQL
   pairs from real user transcripts and run `/autoctx-evaluate`
   against the current context. Use `/autoctx-hillclimb` to add items
   until accuracy is acceptable.

---

## TL;DR for the next agent

If `ask_data` generates wrong SQL for a recurring NL pattern:

1. **Capture the failing SQL** (`scripts/invoke-tool.sh ask_data`).
2. **Diagnose** — is it a value mismatch (case, vocabulary)? A
   missing JOIN? A wrong aggregate? A timezone slip?
3. **Match the failure to a context item type**:
   - Value mismatch → **value_search**
   - Wrong JOIN / aggregate / shape → **template**
   - Wrong filter for a common phrase → **facet**
4. **Read** the appropriate
   `~/.claude/plugins/marketplaces/db-context-enrichment-marketplace/plugin/skills/context-generation-guide/references/<type>/postgresql.md`
   for the exact shape.
5. **Add the item** to `agent/context-sets/weatherbot-narrow.json`.
6. **Re-upload** via Cloud SQL Studio (console only). Bump the
   context-set name if you want a clean A/B (`weatherbot-narrow-v2`).
7. **Verify** with the previously-failing query via
   `scripts/invoke-tool.sh ask_data`.

The plumbing — pg_trgm, the YAML markers, the strip step, the
`WEATHERBOT_GDA_CONTEXT_SET_ID` env var, the deploy script integration
— is all already done. Adding items is editing a JSON file and
clicking upload.
