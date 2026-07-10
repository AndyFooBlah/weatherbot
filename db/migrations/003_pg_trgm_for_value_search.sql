-- Copyright 2026 Andrew Brook
-- Licensed under the Apache License, Version 2.0
--
-- Migration 003: enable pg_trgm and add GiST trigram indexes for QueryData
-- "value search" — the Conversational Analytics API's mechanism for mapping
-- a user-spoken value ("pool", "Poul", "garrage") to the actual stored
-- enum value (`Pool`, `Garage`, …).
--
-- Why this matters
-- ----------------
-- QueryData's NL→SQL planner doesn't know that `sensors.physical_location`
-- is stored title-cased. When the user asks "count days when the pool was
-- over 90", the model confidently writes
--     WHERE sensors.physical_location = 'pool'
-- which returns zero rows because the column actually holds 'Pool'.
--
-- The Context Engineering Agent's recommended fix is a *value_search*:
-- a parameterized SELECT that the planner runs against the user phrase
-- ($value) to canonicalize it before slotting into the WHERE clause.
-- The canonical PostgreSQL value_search template uses pg_trgm's `%`
-- similarity operator (which is case-insensitive) and `<->` distance,
-- backed by a GiST trigram index for performance.
--
-- See:
--   db-context-engineering plugin v0.6.0,
--   skills/context-generation-guide/references/value_search/postgresql.md
--   (TRIGRAM_STRING_MATCH template)
--
-- The extension is per-database; only needed in the weatherbot DB.
-- The indexes are cheap (a few KB) — sensors.physical_location has ~40 rows.

BEGIN;

-- Required for the `%` similarity operator and `<->` distance used by
-- the trigram value_search template.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- GiST index lets `WHERE T."physical_location" % $value::text` use the
-- index and stay fast even if the sensors catalog grows. We index
-- physical_location and measurement_type because both appear as
-- user-spoken values in NL→SQL ("the pool", "the temperature").
CREATE INDEX IF NOT EXISTS idx_sensors_physical_location_trgm
  ON sensors USING gist (physical_location gist_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_sensors_measurement_type_trgm
  ON sensors USING gist (measurement_type gist_trgm_ops);

-- Display name is also user-vocabulary-adjacent ("the outdoor temp",
-- "the bedroom dew point"). Worth indexing for fuzzy lookup.
CREATE INDEX IF NOT EXISTS idx_sensors_display_name_trgm
  ON sensors USING gist (display_name gist_trgm_ops);

COMMIT;
