# Real questions — natural-language collection

A running list of questions in the developer's own words, captured
before they get translated into golden_sql. The list seeds Phase A
of the next-session plan (golden dataset generation).

## Category 1 — "Right now" (current sensor values)

General intent: latest reading from a specific sensor.

**Captured 2026-06-29 — initial pass:**

| # | Natural-language form | Location | Measurement | Notes |
|---|---|---|---|---|
| 1.1 | "How warm is the pool right now?" | Pool | temperature | Positive-polarity natural phrasing |
| 1.2 | "What is the humidity in the garage?" | Garage | humidity | Implicit "right now" |
| 1.3 | "Is the bedroom really cold this morning?" | Bedroom | temperature | Subjective qualifier + "this morning" — see notes |

### Variations to cover (per the developer's stated intent)

The developer said: *"I might ask for the current values for any of
the sensors."* So the context set should handle **every
(location, measurement_type) combo** as a "right now" question, not
just the literal examples above. Phrasings to support:

- **Positive temperature polarity:** "How warm is X?", "How hot is X?"
- **Negative temperature polarity:** "How cold is X?", "Is X cold?"
- **Neutral:** "What's the temperature in X?", "What's X right now?"
- **Yes/no with qualifier:** "Is the bedroom hot?", "Is the pool warm?"
- **Other measurement:** "What's the humidity in X?", "What's the wind
  speed?", "What's the AQI?", "What's the pressure?", etc.
- **Subjective qualifiers:** "really cold", "freezing", "boiling",
  "muggy", "sticky" — these need quantitative interpretation against
  the calibration ranges (see project-summary-log doc on normal ranges).
- **Time qualifier on a "latest" intent:**
  - "right now" → strictly latest reading
  - "this morning" → ambiguous: latest reading OR aggregate over
    morning hours? **Working interpretation:** treat as latest reading
    + plain factual answer; if the user wanted a morning average they'd
    say "average ... this morning." Confirm with developer if disputed.

### Coverage check vs. catalog

Locations that should resolve cleanly:
`Pool`, `Outdoor`, `Bedroom`, `Garage`.

Locations that need graceful failure:
- `Office` → "I don't have a sensor in the office."
- `Pool (retired)` → only surfaces for historical pre-2025-08-25
  queries, not for "right now."

Measurement types that should resolve at the right location:
- `temperature` — Pool, Outdoor, Bedroom, Garage
- `humidity` — Outdoor, Bedroom, Garage (NOT Pool — no pool humidity sensor)
- `dew_point`, `feels_like` — Outdoor, Bedroom, Garage (NOT Pool)
- `wind_speed`, `wind_gust`, `wind_direction`, `pressure`,
  `solar_radiation`, `uv_index`, `pm25`, `aqi` — Outdoor only

Edge case to handle gracefully:
- "What's the humidity at the pool?" → no sensor; bot should say so
  rather than guess or invent.

## Category 2 — Yesterday / a specific past day

General intent: aggregate (max / min / avg / total) over a bounded
historical window. The window definition is the entire game here —
the user explicitly raised this as the key gotcha for the category.

**Captured 2026-06-29:**

| # | Natural-language form | Location | Measurement | Aggregate | Window |
|---|---|---|---|---|---|
| 2.1 | "How hot did the pool get yesterday?" | Pool | temperature | MAX | yesterday (local-date midnight→midnight) |
| 2.2 | "How cold was it in the garage last night?" | Garage | temperature | MIN | **"last night" = 6pm yesterday → 6am today, local** |
| 2.3 | "What was the strongest wind gust last week Saturday?" | Outdoor | wind_gust | MAX | the Saturday of the prior week (not yesterday's Saturday if today is Sun) |

### Temporal vocabulary (developer-stated rules)

These are the rules the bot needs to apply consistently. **Default
timezone is `America/Los_Angeles`** unless explicitly overridden.

| Phrase | Definition | SQL form |
|---|---|---|
| **today** | midnight today → now, local | `(observed_at AT TIME ZONE 'America/Los_Angeles')::date = (now() AT TIME ZONE 'America/Los_Angeles')::date` |
| **yesterday** | midnight yesterday → midnight today, local | `(observed_at AT TIME ZONE 'America/Los_Angeles')::date = (now() AT TIME ZONE 'America/Los_Angeles')::date - 1` |
| **last night** | 6pm yesterday → 6am today, local | `observed_at AT TIME ZONE 'America/Los_Angeles' BETWEEN ((now() AT TIME ZONE 'America/Los_Angeles')::date - 1) + interval '18 hours' AND (now() AT TIME ZONE 'America/Los_Angeles')::date + interval '6 hours'` |
| **this morning** | midnight today → 12pm today, local _(working interpretation)_ | `observed_at AT TIME ZONE '…' BETWEEN today_local AND today_local + interval '12 hours'` |
| **this afternoon** | 12pm → 6pm today, local | `BETWEEN today_local + 12h AND today_local + 18h` |
| **this evening** | 6pm today → midnight, local | `BETWEEN today_local + 18h AND today_local + 24h` |
| **last `<DayName>`** | the most recent past `<DayName>` (could be in this week or previous week — use most recent ≤ 7 days ago) | `(observed_at AT TIME ZONE '…')::date = (most recent <DayName> ≤ today - 1)` |
| **last week `<DayName>`** | the `<DayName>` of the **previous calendar week** (always > 7 days ago) | `(observed_at AT TIME ZONE '…')::date = (the <DayName> of week starting the Sunday before last)` |
| **last week** | ⚠ **AMBIGUOUS — bot should ask which the user means:** (a) prior 7 days (rolling), or (b) the previous Sunday→Saturday week | n/a; needs disambiguation |
| **the last N days/hours/weeks** | rolling: `now() - interval 'N days/hours/weeks'` | `observed_at >= now() - interval 'N <unit>'` |

### Implementation notes

- **Stations.timezone is populated** (`America/Los_Angeles` for both
  stations after migration 004). Templates already use
  `AT TIME ZONE st.timezone`. The literal `'America/Los_Angeles'`
  fallback is for cases where station context isn't joined in.
- **Disambiguation behavior:** QueryData has a
  `generationOptions.generateDisambiguationQuestion` flag we are
  NOT currently using. Worth turning on so the bot asks the user
  "do you mean prior 7 days or last Sunday-Saturday?" rather than
  guessing. Tracked as a follow-up.
- **"Last week Saturday" disambiguation rule:** if today is Sunday
  2026-06-28, "last week Saturday" = 2026-06-20 (not the yesterday
  Saturday of 2026-06-27). The rule is "Saturday of the week before
  the most recent fully-completed week." If today IS a Saturday,
  "last week Saturday" = 7 days ago.

### Templates this category implies

Each of these needs a context-set template (parameterized):

- T-1: MAX of `<measurement>` at `<location>` **yesterday** (local-date)
- T-2: MIN of `<measurement>` at `<location>` **last night** (overnight 6pm–6am)
- T-3: MAX of `<measurement>` at `<location>` on **last week `<DayName>`**
- T-4: any aggregate over a **rolling N-day window**
- T-5: any aggregate over a **specific calendar date** (e.g. "on June 15")
- T-6: any aggregate over **this morning / afternoon / evening**

### Caveat-triggering subset

Any rain-related Category-2 question after 2025-06-10 should surface
the gauge-offline caveat, not return data. Need at least one in the
golden set: e.g. "Did it rain on Tuesday?" → bot must say "rain gauge
has been offline since 2025-06-10."

## Category 3 — Trends, deltas, anomalies, cross-period comparison

General intent: **analytical** queries. Not "what is X right now" or
"what was X yesterday" — instead, "how is X changing," "where are
the outliers," and "how do periods compare to each other." A
qualitative step up in SQL complexity (window functions, self-joins,
multi-step aggregates).

**Captured 2026-06-29:**

| # | Natural-language form | Type | What it computes |
|---|---|---|---|
| 3.1 | "What's the average change in outdoor high temperature from day to day?" | Trend / delta | Daily MAX of outdoor temp → diff between consecutive days → AVG of absolute diffs |
| 3.2 | "How many times in the past month did the daily high increase or decrease by more than 15°F?" | Trend / delta + count | Daily MAX → diff → COUNT(WHERE abs(diff) > 15) over a rolling 30-day window |
| 3.3 | "When did bedroom humidity jump by more than X% in 30 minutes? (could be a shower)" | Anomaly detection | LAG-based rate-of-change; find local jumps |
| 3.4 | "When did pool surface temp drop by more than X°F in 1 hour? (could be cover removal)" | Anomaly detection | LAG-based; find rapid drops |
| 3.5 | "How many days in May 2026 had gusts above 20 mph, and how does that compare to prior years?" | Cross-period comparison | Group by year, filter by month=May, count by threshold; multi-year output |

### Implementation challenges (these are real)

#### Window functions (LAG, LEAD, OVER PARTITION BY)
QueryData's NL→SQL planner often struggles with these. The current
context set has zero window-function templates. We will almost
certainly need to hand-author:

- **Daily-MAX-with-diff template:**
  ```sql
  WITH daily AS (
    SELECT (r.observed_at AT TIME ZONE st.timezone)::date AS local_day,
           max(r.value) AS day_max
    FROM sensor_readings r
    JOIN sensors s ON s.sensor_id = r.sensor_id
    JOIN stations st ON st.mac_address = s.station_id
    WHERE s.physical_location = $1 AND s.measurement_type = $2
      AND s.reliable = true
      AND r.observed_at >= now() - ($3 * interval '1 day')
    GROUP BY 1
  )
  SELECT local_day, day_max,
         day_max - lag(day_max) OVER (ORDER BY local_day) AS diff_from_prev
  FROM daily ORDER BY local_day
  ```

- **Rate-of-change anomaly template** (parameter: time window):
  ```sql
  WITH ranked AS (
    SELECT r.observed_at, r.value,
           r.value - lag(r.value) OVER (ORDER BY r.observed_at) AS delta,
           extract(epoch FROM r.observed_at -
                   lag(r.observed_at) OVER (ORDER BY r.observed_at))
             AS seconds_since_prev
    FROM sensor_readings r
    JOIN sensors s ON s.sensor_id = r.sensor_id
    WHERE s.physical_location = $1 AND s.measurement_type = $2
      AND r.observed_at >= now() - interval '7 days'
  )
  SELECT observed_at AT TIME ZONE 'America/Los_Angeles' AS local_time,
         value, delta
  FROM ranked
  WHERE delta IS NOT NULL
    AND seconds_since_prev <= $3   -- e.g. 1800 (30 min)
    AND ($4::text = 'jump'  AND delta >  $5
      OR $4::text = 'drop'  AND delta < -$5)
  ORDER BY observed_at DESC
  LIMIT 100
  ```

#### Cross-year comparison: data range limitation
**Data starts 2024-08-24.** So "May 2026 vs prior years":
- **May 2025** exists (full month) → comparable
- **May 2024** does NOT exist (we only have late August onward) →
  bot must say so rather than report a misleading zero
- **May 2023, 2022, …** definitely don't exist

The context set needs at least one template that **counts by year +
flags missing years.** Suggested pattern:
```sql
SELECT extract(year FROM r.observed_at AT TIME ZONE st.timezone) AS yr,
       count(DISTINCT (r.observed_at AT TIME ZONE st.timezone)::date)
         AS days_with_event
FROM sensor_readings r
JOIN sensors s ON s.sensor_id = r.sensor_id
JOIN stations st ON st.mac_address = s.station_id
WHERE extract(month FROM r.observed_at AT TIME ZONE st.timezone) = 5
  AND s.measurement_type = 'wind_gust' AND s.reliable = true
  AND r.value > 20
GROUP BY yr ORDER BY yr
```
…and then the agent's NL response should explicitly note which years
have full data vs. partial.

### Anomaly detection as event-finding

The user's intent is **causal hypothesis testing**: "do my actions
show up in the data?" This isn't a single query — it's a workflow:
1. Find the anomalies (rapid jumps/drops)
2. List them with timestamps
3. User compares timestamps to memory ("oh yeah, I took the pool
   cover off at 2pm that Sunday")

So the query interface should be **"return a list of anomaly events
with timestamps,"** not "summarize." Charts are useful here — line
chart with anomalies highlighted — but a tabular list is the
deliverable.

#### Calibration — Pool temperature 30-min variation (run 2026-06-29 over last 60 days)

| Percentile | 30-min temp range |
|---|---|
| p50 (median) | 0.20°F |
| avg | 0.32°F |
| p90 | 0.70°F |
| p95 | 0.90°F |
| p99 | 1.20°F |
| max | 13.50°F (sensor-in-sun artifact, see below) |

**Implication:** 99% of pool 30-min windows show ≤1.2°F variation.
Natural anomaly-detection threshold is **≥1.5°F over 30 min**.

#### Three distinct outlier classes (calibration + memory-check, 2026-06-29)

**Class A — extreme sensor-in-sun artifacts (sustained).** May 30, 2026
had ~8 hours of readings between 99–105°F. The pool isn't a hot tub;
the probe was in direct sun. We should **caveat or filter pool
readings above ~95°F** in the context set — they're noise, not signal.
The 13.5°F max swing in the calibration is from this artifact, not
real.

**Class B — daily sun-on-probe artifact (recurring, ~12:30–14:00).**
The floating thermometer sits in a fixed spot; each day, as the sun
angle shifts, it hits the probe and causes a +2–4°F jump in a
30-min window. **Developer-confirmed 2026-06-29: this is the origin
of the June 12, 13, 14 noon jumps** (originally hypothesized as
"cover-off" events but memory + photos say no cover removal
happened at those times). This is a *recurring daily artifact*, not
an event.

- **Do NOT ship a "cover-off" anomaly template.** Any +N°F midday
  jump would be dominated by this false positive.
- **Do** caveat the surface-temperature reading during the ~12:30–
  14:00 local window: "readings then may reflect the sun hitting
  the surface probe."

**Class C — swim events (validated).** Developer-confirmed against
family-photo evidence, 2026-06-29:

| Local time | Signed Δ (30-min) | Confirmed? |
|---|---|---|
| 2026-06-14 Sun ~16:35 | −2.3°F | ✅ swam |
| 2026-06-21 Sun 15:55 (family-photo says 3–4pm) | −3.4°F range | ✅ swam |
| 2026-06-27 Sat ~16:30 (family-photo says ~4pm) | −1.1°F | ✅ swam |
| 2026-05-31 Sun ~16:35 | −2.5°F | unmentioned; likely also swim |

**Swim-signature template can ship with high confidence:**

| Pattern | Direction | Threshold | Time-of-day |
|---|---|---|---|
| Swim event | drop | ≥1.0°F over 30 min | 12 PM – 8 PM local |

The 1.0°F threshold (rather than 1.5°F) is chosen because the
confirmed June 27 event was only −1.1°F — a tighter threshold
would miss it. False-positive rate check: 30-min windows dropping
≥1.0°F outside the swim time-of-day should be rare (previous
run's p99 was 1.2°F).

For other sensors (uncalibrated; **needs same exercise**):

| Hypothesis | Sensor | Initial-guess threshold | Default time window | Calibration status |
|---|---|---|---|---|
| Shower in adjoining bathroom | Bedroom humidity | +10% | 30 min | **uncalibrated** — run 30-min humidity range distribution to confirm threshold |
| HVAC kicked on | Bedroom or Garage temperature | ±3°F | 30 min | **uncalibrated** |
| Door opened on a cold day | Bedroom temperature | -2°F | 15 min | **uncalibrated** |

> **Process pattern:** before encoding any anomaly template, run
> the same calibration exercise (distribution of N-min ranges,
> identify outlier classes, separate artifacts from events). Don't
> guess at thresholds.

### Templates this category implies

- T-7: **Daily aggregate time series** (max/min/avg per day) with
  optional `diff_from_prev` column
- T-8: **Count of days where a daily aggregate crossed a threshold**
  (rolling window)
- T-9: **Rate-of-change anomaly finder** (param: location, measurement,
  threshold, time window, direction)
- T-10: **Cross-year same-month comparison** (with year-coverage notes)
- T-11: **Cross-period count comparison** ("X this month vs same month
  last year")

### Caveats / data coverage notes the bot must respect

- Anything spanning **August 2024 ← (earlier dates)** has no data.
- **Rain** comparisons across years break at 2025-06-10 (gauge died).
- **Pool** comparisons across years break at 2025-08-25 (sensor
  channel changed; old sensor was `Pool (retired)`).

## Category 4 — Diagnostic ("are my sensors OK?")

General intent: ask the data about its **own quality** — sensor
liveness, missing data, suspect readings, sensors flagged unreliable.

**Captured 2026-06-29 (developer-confirmed: "I do like the
diagnostic case"):**

| # | Natural-language form | What it computes |
|---|---|---|
| 4.1 | "Are any of my sensors offline or acting weird?" | Each sensor: time since last reading + recent stability metric |
| 4.2 | "When was the last reading from the rain gauge?" | latest observed_at per sensor — should surface the 2025-06-10 cutoff |
| 4.3 | "Are there any suspicious readings I should look at?" | rolling N-min range > threshold; lists outliers |
| 4.4 | "Which sensors are flagged unreliable?" | sensors.reliable = false |
| 4.5 | "What's the data coverage for the pool — any gaps?" | gap detection (max(observed_at) - min(observed_at) vs expected at 5-min cadence) |

### Templates this category implies

- T-12: **Sensor liveness:** `max(observed_at) per sensor` + flag if
  > 30 min behind now()
- T-13: **Unreliable-sensor catalog:** `SELECT … FROM sensors WHERE
  reliable = false`
- T-14: **Stuck-value detector:** sensors whose last N readings are all
  identical (catches the rain gauge stuck at 258.01 inches)
- T-15: **Gap detector:** for a chosen sensor, find runs of consecutive
  expected-5-min slots with no reading

## Category 5 — Summary ("give me the big picture")

General intent: a **one-paragraph or short-list synthesis** across
multiple sensors / multiple aggregates / a chosen time window.

**Captured 2026-06-29 (developer-confirmed: "I do like the
summary case"):**

| # | Natural-language form | What it produces |
|---|---|---|
| 5.1 | "Give me a one-paragraph summary of this week's weather." | Outdoor high/low/avg + rain (with caveat) + notable wind + AQI mention |
| 5.2 | "How was last weekend?" | Sat+Sun aggregated; outdoor + pool |
| 5.3 | "What's changed indoors in the last 24 hours?" | Bedroom + garage temp + humidity deltas |
| 5.4 | "Anything unusual today?" | scans all sensors for anomalies (uses Category 4 logic) and summarizes |

### Why summaries are tricky in QueryData

QueryData returns ONE SQL result per call. A summary needs **multiple
sub-queries assembled into prose**. Two strategies:

- **(a)** Single big query with multiple CTEs that produces a wide row
  (cols: outdoor_max, outdoor_min, outdoor_avg, indoor_max, …) and
  let the natural-language-answer step write the prose. Risk:
  generation may drop fields or invent numbers.

- **(b)** Have the agent call multiple curated tools
  (`summarize_period` × N) and synthesize the prose itself, **not
  through QueryData.** Cleaner separation: curated tools are
  deterministic; agent-side prose generation is a known LLM job.

  > **Recommendation:** strategy (b). Add no summary template to
  > QueryData. Instead, add agent-prompt guidance that recognizes
  > "summary" intents and chains 3–5 curated calls. Voice UX wins
  > too — summary is naturally narrative.

## Global behavior rules (developer-stated 2026-06-29)

- **Honest about data gaps.** When data isn't available, *say so
  explicitly*. Examples:
  - Rain after 2025-06-10 → "the rain gauge has been offline since
    June 10, 2025."
  - Forecasts / predictions → "I don't make predictions; I only
    have historical readings."
  - Office temperature → "I don't have a sensor in the office."
  - Pre-August 2024 anything → "I don't have data from before
    August 24, 2024."
- **Don't fake plausible-looking results.** If a query would return
  zero or NULL because of missing data, the bot must distinguish
  that from a real zero. E.g., "no rain this week" vs. "we can't
  measure rain right now" — these mean different things.
- **For "last week" and other ambiguous phrases, ask before
  guessing** (see Category 2 vocabulary table).

These rules live in the **system prompt**, not in QueryData
templates. Track as a follow-up: rewrite the relevant section of
`instructionBuilder.ts` once the v2 context lands.
