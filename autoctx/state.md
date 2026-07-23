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
| evaldb-v3-baseline | weatherbot-narrow-v3 (uploaded) | eval_reports/86064dd6… — llmrater 13/15 (86.7%), executable 15/15 | wb_007: rain_rate vs rain-total vocabulary gap (facet candidate for v5). wb_014: empty-today artifact — snapshot seeded during a prod ingest gap (recovered); re-seed before eval runs. | baseline |

## Notes (updated 2026-07-23, weatherbot#15 L2 work)

- **Eval DB**: L2 evals run against `weatherbot_eval` (source
  `weatherbot-pg-eval` in autoctx/tools.yaml), never prod. Re-seed with
  `bash infra/07-seed-eval-db.sh` before eval runs — the snapshot is
  static and "today"-style cases go empty as it ages.
- **Dataset**: `autoctx/golden-evaldb.json` = golden-seed with
  database=weatherbot_eval and the 4 time-output goldens rewritten to
  the v4 UTC contract (observed_at_utc / hour_utc / occurred_at_utc).
  The v3 baseline is expected to diverge on those only in column
  naming/timezone representation — the llmrater tolerated 3 of 4.
- **Current prod set**: weatherbot-narrow-v3. Candidate:
  agent/context-sets/weatherbot-narrow-v4.json (UTC outputs +
  is_estimated guards), awaiting console upload; evaluate as
  experiment `evaldb-v4-candidate` immediately after upload, compare
  against evaldb-v3-baseline before flipping prod.

## Notes

- IAM auth via ADC. The MCP toolbox started by this plugin uses your
  current `gcloud auth application-default login` identity. That user
  must already be a Cloud SQL IAM user (set up by
  `infra/03-enable-gda-access.sh` during the original bootstrap).
- The legacy wide `observations` table and `sensor_assignments` table
  are NOT in scope for the narrow-schema context set. The agent should
  treat the four narrow tables (`stations`, `sensors`, `sensor_readings`,
  `unmonitored_locations`) as the universe.
