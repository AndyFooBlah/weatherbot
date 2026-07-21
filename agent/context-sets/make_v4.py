#!/usr/bin/env python3
"""Derive weatherbot-narrow-v4.json from v3.

v4 = v3 with two systematic changes:
  1. ALL template output columns revert to UTC (observed_at_utc /
     occurred_at_utc / hour_utc) — the voice agent now converts UTC to
     speech via the client-side nl2time describe_time tool, so context-set
     templates must stop pre-localizing. AT TIME ZONE stays ONLY in WHERE
     clauses, where it defines local-calendar-day boundaries.
  2. Estimate-awareness (weatherbot#13): every template that scans
     sensor_readings gains `r.is_estimated = false` so ask_data answers
     from measured data only, matching the curated tools' default. Two
     facets teach the planner the is_estimated column for free-form SQL.

Run from agent/context-sets/:  python3 make_v4.py
The script asserts every intended rewrite fired; a v3 edit that breaks an
assumption fails loudly instead of silently producing a wrong v4.
"""

import json
import re
import sys

src = json.load(open("weatherbot-narrow-v3.json"))

# ── 1. Output columns → UTC ────────────────────────────────────────────
OUTPUT_COL_REWRITES = [
    # (pattern, replacement) — applied to sql + parameterized_sql.
    (
        r"\(observed_at AT TIME ZONE 'America/Los_Angeles'\)::timestamp AS local_time",
        "observed_at AS observed_at_utc",
    ),
    (
        r"\((r?\.?observed_at) AT TIME ZONE st\.timezone\)::timestamp AS local_time",
        r"\1 AS observed_at_utc",
    ),
    (
        r"\(r\.observed_at AT TIME ZONE st\.timezone\)::timestamp AS local_time",
        "r.observed_at AS observed_at_utc",
    ),
    (
        r"\(max\(r\.observed_at\) AT TIME ZONE st\.timezone\) AS occurred_at_local",
        "max(r.observed_at) AS occurred_at_utc",
    ),
    (
        r"date_trunc\('hour', r\.observed_at AT TIME ZONE st\.timezone\) AS hour_local",
        "date_trunc('hour', r.observed_at) AS hour_utc",
    ),
]

# ── 2. Measured-only guards ────────────────────────────────────────────
MEASURED_GUARDS = [
    # LATERAL latest-reading subselects.
    (
        "FROM sensor_readings r WHERE r.sensor_id = s.sensor_id",
        "FROM sensor_readings r WHERE r.sensor_id = s.sensor_id AND r.is_estimated = false",
    ),
    # Main scans: first WHERE after a sensor_readings join starts `WHERE s.`.
    (
        " WHERE s.physical_location",
        " WHERE r.is_estimated = false AND s.physical_location",
    ),
    (
        " WHERE s.measurement_type",
        " WHERE r.is_estimated = false AND s.measurement_type",
    ),
    # Staleness template: LEFT JOIN — guard must live in the join condition
    # so never-reporting sensors keep their NULL row, and estimates can
    # never make an offline sensor look current.
    (
        "LEFT JOIN sensor_readings r ON r.sensor_id = s.sensor_id",
        "LEFT JOIN sensor_readings r ON r.sensor_id = s.sensor_id AND r.is_estimated = false",
    ),
]

col_hits = 0
guard_hits = 0


def rewrite(sql: str, is_template_scan: bool) -> str:
    global col_hits, guard_hits
    for pat, rep in OUTPUT_COL_REWRITES:
        sql, n = re.subn(pat, rep, sql)
        col_hits += n
    if is_template_scan and "sensor_readings" in sql:
        before = sql
        for pat, rep in MEASURED_GUARDS:
            if pat in sql and "is_estimated" not in sql:
                sql = sql.replace(pat, rep, 1)
        if sql != before:
            guard_hits += 1
    return sql


unguarded = []
for t in src["templates"]:
    t["sql"] = rewrite(t["sql"], True)
    t["parameterized"]["parameterized_sql"] = rewrite(
        t["parameterized"]["parameterized_sql"], True
    )
    for k in ("sql", "parameterized"):
        s = t["sql"] if k == "sql" else t["parameterized"]["parameterized_sql"]
        if "sensor_readings" in s and "is_estimated" not in s:
            unguarded.append((t["nl_query"], k))

if unguarded:
    print("FATAL: templates still scanning sensor_readings without an "
          "is_estimated guard:", file=sys.stderr)
    for q, k in unguarded:
        print(f"  [{k}] {q}", file=sys.stderr)
    sys.exit(1)

leftover = [
    t["nl_query"]
    for t in src["templates"]
    if re.search(r"AS (local_time|hour_local|occurred_at_local)", t["sql"])
    or re.search(
        r"AS (local_time|hour_local|occurred_at_local)",
        t["parameterized"]["parameterized_sql"],
    )
]
if leftover:
    print(f"FATAL: local output columns survived in: {leftover}", file=sys.stderr)
    sys.exit(1)

# The peak templates grouped by st.timezone solely for the local cast.
for t in src["templates"]:
    t["sql"] = t["sql"].replace("GROUP BY s.unit, st.timezone", "GROUP BY s.unit")
    t["parameterized"]["parameterized_sql"] = t["parameterized"][
        "parameterized_sql"
    ].replace("GROUP BY s.unit, st.timezone", "GROUP BY s.unit")

# ── 3. Facets for the estimate column ──────────────────────────────────
src["facets"].extend(
    [
        {
            "sql_snippet": "sensor_readings.is_estimated = false",
            "intent": "measured readings only — the default; excludes gap-filled estimates",
            "manifest": "restrict to actual measurements vs estimates",
            "parameterized": {
                "parameterized_sql_snippet": "sensor_readings.is_estimated = $1",
                "parameterized_intent": "readings where estimated is $1",
            },
        },
        {
            "sql_snippet": "sensor_readings.is_estimated = true",
            "intent": "estimated / gap-filled readings (synthetic values inserted for sensor outages; estimation_method says how)",
            "manifest": "restrict to actual measurements vs estimates",
            "parameterized": {
                "parameterized_sql_snippet": "sensor_readings.is_estimated = $1",
                "parameterized_intent": "readings where estimated is $1",
            },
        },
    ]
)

# ── 4. Header comment ──────────────────────────────────────────────────
src["_comment"] = [
    "weatherbot-narrow-v4 — Context Set for QueryData / Conversational",
    "Analytics API on the weatherbot narrow schema.",
    "",
    "Delta from v3 (weatherbot-narrow-v3.json):",
    "* ALL template output timestamps reverted to UTC (observed_at_utc,",
    "  occurred_at_utc, hour_utc). The voice agent now owns UTC-to-speech",
    "  via a deterministic client-side describe_time tool (nl2time), so",
    "  pre-localized output columns are no longer needed and 'all tools",
    "  operate in UTC'. AT TIME ZONE remains ONLY in WHERE clauses, where",
    "  it defines local-calendar-day boundaries ('yesterday' = local",
    "  midnight to midnight).",
    "* Estimate-awareness (weatherbot#13): every template scanning",
    "  sensor_readings now has r.is_estimated = false, so ask_data",
    "  answers from measured data only — consistent with the curated",
    "  tools' default. Two new facets teach the planner is_estimated for",
    "  free-form questions about estimates.",
    "* Fixed a latent v3 bug: the parameterized peak-wind template",
    "  referenced st.timezone with no stations join; the UTC rewrite",
    "  removes the reference.",
    "",
    "Generated from v3 by make_v4.py (same directory) — edit v3 or the",
    "script, not this file, then re-run.",
    "",
    "Empirical grounding for the swim event finder (unchanged from v2):",
    "* 30-min pool temp variation p99 = 1.2F over 17,108 windows.",
    "* Threshold 1.0F chosen because a validated swim event",
    "  (2026-06-27) showed only -1.1F.",
    "* Time-of-day gate 12:00-20:00 local avoids overnight cover-",
    "  induced sensor stability + midday sun-on-probe artifacts.",
    "* Value <= 95 filter excludes Class A artifacts (2026-05-30",
    "  had ~8h of readings in the 99-105F range).",
    "",
    "Upload workflow: Cloud SQL Studio -> Data Agents -> Context Sets ->",
    "Upload as weatherbot-narrow-v4 (NEW version, not an overwrite of v3",
    "— keeps rollback trivial). Then flip WEATHERBOT_GDA_CONTEXT_SET_ID",
    "in infra/env.sh to the v4 resource name and redeploy the toolbox",
    "(bash infra/05-deploy-toolbox.sh). Roll back by flipping the env",
    "var back to v3.",
]

json.dump(src, open("weatherbot-narrow-v4.json", "w"), indent=1)
print(f"v4 written: {col_hits} output-column rewrites, "
      f"{guard_hits} templates guarded, "
      f"{len(src['templates'])} templates, {len(src['facets'])} facets")
