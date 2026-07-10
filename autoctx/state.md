# context authoring experiment state tracking

Workspace for the Context Engineering Agent
(`db-context-engineering@db-context-enrichment-marketplace` v0.6.0).

This file is the persistent memory the agent's `autoctx-*` skills
read/write across hill-climbing iterations. The header above is the
literal string the `autoctx-init` skill expects to find.

## Existing context as baseline

The current production context set is at
`$HOME/dev/weatherbot/agent/context-sets/weatherbot-narrow.json`,
uploaded to Cloud SQL Studio as `weatherbot-narrow-v1`
(`projects/<P>/locations/<R>/contextSets/weatherbot-narrow-v1`).
Use that as the v1 base context for the first hillclimb loop —
no need to re-run `/autoctx-bootstrap` from scratch.

## Experiments

(Each `/autoctx-evaluate` run will append an experiment row below.
The first one will be `accuracy-v1-baseline`.)

| Experiment | Base context | Eval report | Gap analysis | Loop |
|---|---|---|---|---|
| _(empty — populated by /autoctx-evaluate)_ | | | | |

## Notes

- IAM auth via ADC. The MCP toolbox started by this plugin uses your
  current `gcloud auth application-default login` identity. That user
  must already be a Cloud SQL IAM user (set up by
  `infra/03-enable-gda-access.sh` during the original bootstrap).
- The legacy wide `observations` table and `sensor_assignments` table
  are NOT in scope for the narrow-schema context set. The agent should
  treat the four narrow tables (`stations`, `sensors`, `sensor_readings`,
  `unmonitored_locations`) as the universe.
