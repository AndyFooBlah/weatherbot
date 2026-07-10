# autoctx/ — Context Engineering Agent workspace

Workspace for the `db-context-engineering` Claude Code plugin
(v0.6.0). The plugin's `autoctx-*` skills read and write here as
they bootstrap, evaluate, and hill-climb the QueryData context set.

## What's pre-populated

| File | Purpose | Created by |
|---|---|---|
| `tools.yaml` | MCP-toolbox config — Cloud SQL Postgres source + `list-schemas` + `execute-sql` tools, pointed at our DB via `infra/env.sh` interpolation | prior session prep |
| `state.md` | The persistent state tracker the agent reads/writes across iterations. Initialized with the header `/autoctx-init` expects | prior session prep |
| `golden-seed.json` | 12 hand-authored NL+SQL evaluation pairs covering the failure modes we know about (case sensitivity, timezone arithmetic, latest-per-sensor, reliable-flag respect, etc.) — every SQL was validated against the live DB before commit | prior session prep |
| `experiments/` | Empty dir where `/autoctx-evaluate` will drop per-run reports | prior session prep |

## What's gitignored

`experiments/` contents are generated artifacts — eval reports,
hillclimb iteration JSONs, gap analyses. Only the directory itself
is tracked.

## Required before running any `/autoctx-*` skill

```bash
source infra/env.sh                         # PROJECT_ID, REGION, etc.
gcloud auth application-default login       # ADC for IAM-auth Cloud SQL
```

The MCP toolbox the plugin starts (via `uvx toolbox-server@1.4.0
--config autoctx/tools.yaml --stdio`) authenticates as your current
ADC identity. That identity must already exist as a Cloud SQL IAM
user with `SELECT` on the public schema — set up for the developer's
user identity by `infra/03-enable-gda-access.sh`.

## Sanity check

To verify the toolbox config can connect without invoking Claude:

```bash
source infra/env.sh
uvx toolbox-server@1.4.0 --config autoctx/tools.yaml \
    invoke weatherbot-pg-list-schemas
```

Should print the schema of the four narrow tables (`stations`,
`sensors`, `sensor_readings`, `unmonitored_locations`) plus the
legacy ones we'll be telling the agent to ignore.

## What to do next

See [`docs/project-summary-log/next-session-context-engineering-plan.md`](../docs/project-summary-log/next-session-context-engineering-plan.md)
for the step-by-step plan, expected interactive prompts, and what
to enter at each.
