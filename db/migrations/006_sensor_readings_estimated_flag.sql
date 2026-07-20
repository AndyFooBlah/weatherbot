-- Copyright 2026 Andrew Brook
-- Licensed under the Apache License, Version 2.0
--
-- Migration 006: mark sensor_readings as measured vs. estimated.
--
-- Motivation: the outdoor/backyard sensors periodically lose their RF link
-- to the base station, leaving multi-hour gaps where no reading was ever
-- captured (see the 2026-07 outage cluster). Those windows drag down any
-- average/min/max/count that spans them. We want to be able to fill the
-- gaps with best-estimate values (interpolated from the readings bracketing
-- the gap, shaped by the sensor's typical daily curve) so aggregates are
-- less distorted — but such fills must NEVER be mistaken for real
-- measurements.
--
-- Design:
--   * is_estimated      — false for every real measurement (the default, so
--     all 6.35M existing rows and all future ingest rows are correctly
--     tagged with no code change). true for gap-fill rows we synthesize.
--   * estimation_method — NULL for measurements; for estimates, records how
--     the value was derived ('linear_interp', 'diurnal_pattern', …) so the
--     method is auditable and re-derivable.
--
-- Adding a NOT NULL column with a constant (non-volatile) default is a
-- metadata-only change in Postgres 11+ — no rewrite of the 6.35M-row table.
--
-- Downstream contract (handled in follow-up work, NOT here): every curated
-- tool and QueryData template that reports measurements must decide,
-- explicitly, whether to include estimates. Default posture: measured-only
-- for "what was the reading" questions; estimates allowed (and disclosed)
-- for long-window averages where gap distortion matters more than purity.
-- The `reliable` flag is about sensor trustworthiness; `is_estimated` is
-- about this specific row being synthetic — they are orthogonal.

BEGIN;

ALTER TABLE sensor_readings
  ADD COLUMN IF NOT EXISTS is_estimated boolean NOT NULL DEFAULT false;

ALTER TABLE sensor_readings
  ADD COLUMN IF NOT EXISTS estimation_method text;

COMMENT ON COLUMN sensor_readings.is_estimated IS
  'false = real measurement; true = synthesized gap-fill (see estimation_method). Never treat estimated rows as measured data.';
COMMENT ON COLUMN sensor_readings.estimation_method IS
  'NULL for measurements. For estimates: how the value was derived, e.g. linear_interp, diurnal_pattern.';

-- Partial index so "give me only estimated rows" (for review / recompute /
-- deletion) is cheap without adding overhead to the measured-row hot path.
CREATE INDEX IF NOT EXISTS sensor_readings_estimated_idx
  ON sensor_readings (sensor_id, observed_at)
  WHERE is_estimated;

COMMIT;
