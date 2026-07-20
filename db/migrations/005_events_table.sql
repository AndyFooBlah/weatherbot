-- Copyright 2026 Andrew Brook
-- Licensed under the Apache License, Version 2.0
--
-- Migration 005: events table — free-text log of things that happened in
-- the physical world that might explain or contextualize the sensor data.
--
-- Motivation: sensor readings alone can't tell you *why* a value moved.
-- A pool-temperature jump might be the cover coming off; a step change in
-- a reading might be a sensor being relocated or reset; a gap might be a
-- base-station restart. This table lets the user annotate the timeline so
-- those correlations are recoverable later.
--
-- Design:
--   * occurred_at — when the event happened (the user-meaningful time,
--     e.g. "10am this morning"). NOT necessarily when the row was written.
--   * note        — the raw free text the user dictated. This is the
--     core payload; everything else is optional metadata.
--   * category    — optional coarse tag ('pool', 'sensor', 'maintenance',
--     'weather', …) to make filtering easy. Free-form; not an enum, so the
--     agent can invent categories without a migration.
--   * source      — how the row got created ('voice' when set through
--     weatherbot, 'manual' for direct SQL, 'seed' for backfilled history).
--   * created_at  — wall-clock insert time, for audit.
--
-- Writable via the weatherbot voice agent through a parameterized
-- `record_event` MCP tool (added to agent/toolbox.yaml). The toolbox
-- service account is granted INSERT on this table specifically — it is
-- otherwise SELECT-only — so the write surface is exactly one
-- parameterized insert, not arbitrary SQL.

BEGIN;

CREATE TABLE IF NOT EXISTS events (
  event_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  occurred_at timestamptz NOT NULL,
  note        text        NOT NULL,
  category    text,
  source      text        NOT NULL DEFAULT 'manual',
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- Events are almost always queried by time ("what happened last week?"),
-- newest first.
CREATE INDEX IF NOT EXISTS events_occurred_at_idx ON events (occurred_at DESC);

-- Optional category filtering ("show me pool events").
CREATE INDEX IF NOT EXISTS events_category_idx ON events (category)
  WHERE category IS NOT NULL;

-- Seed the one event the user has already told us about: they began
-- leaving the swimming-pool cover off during the day starting 2026-07-04.
-- Time-of-day is approximate (noon local); the user can correct or refine
-- via the agent. This is the event that explains the pool-temperature
-- behavior change (more direct-sun surface warming) from early July on.
INSERT INTO events (occurred_at, note, category, source)
SELECT timestamptz '2026-07-04 12:00:00 America/Los_Angeles',
       'Began leaving the swimming pool cover off during the day (seasonal). Approximate time — correct if needed.',
       'pool',
       'seed'
WHERE NOT EXISTS (
  SELECT 1 FROM events WHERE source = 'seed' AND category = 'pool'
    AND occurred_at::date = date '2026-07-04'
);

COMMIT;
