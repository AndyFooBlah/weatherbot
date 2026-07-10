-- Copyright 2026 Andrew Brook
-- Licensed under the Apache License, Version 2.0
--
-- Migration 004: backfill stations.timezone for existing stations.
--
-- Why this matters
-- ----------------
-- Our QueryData context-set templates (and the curated tools to a lesser
-- extent) convert UTC-stored timestamps to local calendar dates via
--
--   (sensor_readings.observed_at AT TIME ZONE stations.timezone)::date
--
-- When stations.timezone is NULL, `AT TIME ZONE NULL` returns NULL and
-- `COUNT(DISTINCT NULL)` is zero — every per-local-day aggregate
-- silently returns zero rows. The first symptom was the ask_data
-- count-of-days regression: after the context-set upload fixed the
-- 'pool' vs 'Pool' casing bug (migration 003 + context-sets/), the
-- generated SQL was correct but still returned 0 because timezone was
-- NULL on both stations.
--
-- The ingest upsert path (ingest/weatherbot_ingest/observations.py
-- `_upsert_station`) only sets timezone when AWN's /devices API
-- surfaces it, which it apparently never does for our account. Both
-- stations are at the same location, so America/Los_Angeles is
-- unambiguously correct.
--
-- Going forward, the right fix is deriving timezone from lat/lon at
-- ingest time (tzfpy or equivalent). Tracked separately as a follow-up.

BEGIN;

-- Set Bay Area timezone for any station that still has NULL. The
-- WHERE clause makes this safe to re-run and safe to extend later if
-- we onboard a station outside America/Los_Angeles — that station
-- would need to be set explicitly first, and this UPDATE would skip
-- it once it's non-NULL.
UPDATE stations
   SET timezone = 'America/Los_Angeles'
 WHERE timezone IS NULL;

COMMIT;
