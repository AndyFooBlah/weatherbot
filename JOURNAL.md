# weatherbot — project journal

Append-only. Every entry starts with a UTC timestamp and a tag
(`[step] [decision] [friction] [learning] [surprise] [incident] [idea]
[milestone]`). Never edit or delete a past entry; correct by appending
a new entry that names the earlier one's timestamp. Synced daily to
`andy-private-docs/journals/`.

Entries up to 2026-10-16 were backfilled on 2026-10-16 from git
history, GitHub issues, repo docs, and Claude session transcripts;
timestamps are anchored to commits/issues where available, else
day-accurate.

---

- 2026-06-17T22:00Z [incident] ask_data returned wrapped `PERMISSION_DENIED` only from Cloud Run; ~13 hours of caller-side debugging that server-side trace resolved in minutes. Root causes and rules captured in docs/postmortem-2026-06-17-ask-data-permission-denied.md (impersonation-chain IAM trap; escalate wrapped 1P errors early).
- 2026-07-10T17:09Z [milestone] Repo created: ingest pipeline (AWN → Cloud SQL Postgres), MCP Toolbox tool layer on Cloud Run, QueryData/ask_data path with context sets (cbe107f).
- 2026-07-19T20:00Z [step] Outage forensics after owner reported backyard sensors needing base-station restarts: 90.5% coverage over 90 days on outdoor temperature, 33 gaps ≈ 2,448 lost readings, six ~22h gaps in early July, recoveries clustered at dawn, Pool-array gaps co-occur → base-station/RF cause; heat ruled out (gaps start at 55–60°F). Data during disconnects is LOST, not buffered.
- 2026-07-19T21:00Z [decision] Owner commissioned three features: "First is to send me a daily report of outages. Second is to add a new column to the sensor readings which carries the actual vs estimated state… tagged explicitly as estimates rather than actual measurements. And third, we should add a new table to track misc events… settable via the weatherbot." Report delivery: "Email via Gmail API."
- 2026-07-20T05:52Z [milestone] All three features built: events table + record_event/list_events (aa7fb4e), daily outage report job (e10a60c), is_estimated column + diurnal gap-fill with estimate-aware curated tools (3135ed1). Estimate safety: curated tools measured-only by default, include_estimates opt-in with estimated_count disclosure.
- 2026-07-21T04:00Z [incident] Owner field test: "I'll refill the bird feeder at 9pm tonight" recorded a day late (LLM did the UTC offset math). Start of the time-correctness saga.
- 2026-07-21T04:07Z [decision] First fix put timezone conversion in a postgres-sql tool; owner pushback: "why are you using the database to perform timezone conversions? Can that be simply implemented in the application code?" Moved client-side (1368269). Owner's framing became the requirement: "a really robustly tested local function (no database calls) that can correctly map natural language into points in time" — the seed of nl2time.
- 2026-07-21T05:08Z [learning] Three escalating head-math failure modes, each fixed by making the data deterministic: tools return *_local speech strings (d7008b1), then (today)/(yesterday) markers (820fb29) — the model cannot be trusted with ANY date arithmetic, including absolute→relative labeling.
- 2026-07-21T19:06Z [milestone] nl2time adopted; ALL tools reverted to pure UTC; v4 context set authored by make_v4.py (UTC outputs + is_estimated guards), all 19 templates EXPLAIN-verified (8b1cda3). Conversion now happens only at the client boundary.
- 2026-07-23T01:40Z [milestone] Eval data plane (#15): weatherbot_eval DB seeded server-side via GCS export/import (~3 GB, whole-day time shift, fixtures), weatherbot-toolbox-eval deployed, write isolation proven (77393a3).
- 2026-07-23T21:26Z [step] EvalBench L2 evals live against the eval DB; v3 baseline llmrater 13/15, executable 15/15 (966c3c1). EvalBench reviewed at code level per owner: AgentEvaluator is CLI-coding-agent-only; DataAgentEvaluator/querydata is the right fit for ask_data.
- 2026-07-23T21:40Z [milestone] v4 candidate scored 13/15 = baseline → prod flipped to v4 same day; ask_data generates observed_at_utc + is_estimated=false (f209785). v5 hillclimb candidates logged in autoctx/state.md: rain vocabulary facet; is_estimated-vs-reliable facet competition.
- 2026-07-23T22:57Z [learning] Full-fleet gap-fill dry-run caught two estimation-design flaws before touching prod: battery is a status flag (synthetic rows carry no information) and wind_direction is circular (linear interpolation across 359°→1° is wrong). Both excluded (1d3cf13). Applied: 26,426 rows, outdoor temp coverage 90.5% → 99.9%.
- 2026-07-23T05:00Z [incident] Prod ingest stalled overnight Jul 22 18:15 PT → Jul 23 morning (all sensors — station/AWN side), discovered via the eval seed snapshot; recovered without intervention. Exactly the class of event the daily report exists for.
- 2026-07-25T04:47Z [friction] Gmail OAuth doc had drifted badly — Google renamed the console to "Google Auth Platform" (Branding/Audience/Clients). Replaced manual steps with scripts/setup-gmail-oauth.py (loopback flow + PKCE, Secret Manager writes, SA grants) (7081579). [learning] Testing-status apps get 7-day refresh tokens — the app must be published "In production" or the daily report silently dies a week later.
- 2026-07-25T05:00Z [decision] Owner created dedicated sender account weatherbot.mailsender@gmail.com; reports go to andybrook@gmail.com.
- 2026-07-25T06:00Z [milestone] Daily outage report live (#14 closed): 8am PT schedule, end-to-end send verified. First run immediately surfaced an ACTIVE Pool-array outage (~353 min behind) — the feature earned its keep on day one.
- 2026-09-18T23:20Z [step] License housekeeping: Apache-2.0 appendix copyright line (6baed36), per the owner's license policy (Apache-2.0 verbatim for code).
- 2026-10-16T20:30Z [step] Journal created per new global journal policy; backfilled from git history, issues #13–#15, repo docs, and session transcripts.
