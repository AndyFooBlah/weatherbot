-- Migration 002 — narrow / normalized observations schema.
--
-- The wide observations table (one row per poll, one column per hoisted
-- AWN field) makes "what is the garage temperature right now" require
-- hardcoded per-sensor SQL — and silently drops any column the toolbox
-- tool forgets to list (see incident: latest_observation returning
-- outdoor temp when asked about the garage).
--
-- This migration introduces a narrow, normalized shape that the agent
-- can query uniformly without knowing AWN field names:
--
--   sensors          — one row per physical sensor measurement
--                      (e.g. "Garage temperature" → temp2f on station X)
--   sensor_readings  — one row per (sensor, timestamp, value)
--   polls            — one row per (station, timestamp) with the raw
--                      AWN payload, so we never lose a field we
--                      forgot to extract
--
-- The wide `observations` table and `sensor_assignments` table are NOT
-- dropped here — they continue to exist as a safety net. The ingest
-- path will dual-write to both for some period before we drop them
-- in a future migration.

BEGIN;

-- ─── sensors ──────────────────────────────────────────────────────────
-- One row per (station, awn_field_name) — describes WHAT the sensor
-- measures and WHERE it lives. Supersedes sensor_assignments.

CREATE TABLE IF NOT EXISTS sensors (
  sensor_id          text PRIMARY KEY,                  -- "<mac>:<awn_field_name>"
  station_id         text NOT NULL REFERENCES stations(mac_address),
  awn_field_name     text NOT NULL,                     -- "temp2f", "humidity", …
  measurement_type   text NOT NULL,                     -- canonical kind: see below
  physical_location  text NOT NULL,                     -- "Outdoor", "Bedroom", "Garage", "Pool"
  display_name       text NOT NULL,                     -- "Garage temperature"
  unit               text NOT NULL,                     -- "°F", "%", "mph", "inHg", "in", "W/m²", "µg/m³", "AQI", "battery_ok"
  sensor_kind        text,                              -- carry-over from sensor_assignments ("outdoor", "aux_temp_humidity", …)
  reliable           boolean NOT NULL DEFAULT true,
  is_active          boolean NOT NULL DEFAULT true,     -- false when superseded (e.g. temp4f → temp7f for pool)
  retired_at         timestamptz,                       -- set when is_active flips to false
  notes              text,
  first_seen_at      timestamptz,
  last_seen_at       timestamptz,
  UNIQUE (station_id, awn_field_name)
);

COMMENT ON TABLE sensors IS
  'One row per physical sensor measurement on a station. Replaces sensor_assignments + hoisted observations columns.';
COMMENT ON COLUMN sensors.measurement_type IS
  'Canonical kind. One of: temperature, humidity, dew_point, feels_like, wind_speed, wind_gust, wind_direction, pressure, rain_rate, rain_total, solar_radiation, uv_index, pm25, aqi, battery.';
COMMENT ON COLUMN sensors.is_active IS
  'False when the sensor has been retired or superseded. Tools should default to active sensors. Historical readings stay in sensor_readings.';

CREATE INDEX IF NOT EXISTS sensors_station_active_idx
  ON sensors (station_id, is_active) WHERE is_active;

CREATE INDEX IF NOT EXISTS sensors_location_idx
  ON sensors (lower(physical_location));

-- ─── polls ────────────────────────────────────────────────────────────
-- Per-poll metadata: the raw AWN payload, indexed by (station, time).
-- Splits "what did the station report" (polls) from "what value did each
-- sensor read" (sensor_readings).

CREATE TABLE IF NOT EXISTS polls (
  station_id   text NOT NULL REFERENCES stations(mac_address),
  observed_at  timestamptz NOT NULL,
  raw          jsonb NOT NULL,
  PRIMARY KEY (station_id, observed_at)
);

CREATE INDEX IF NOT EXISTS polls_observed_at_brin
  ON polls USING brin (observed_at);

-- ─── sensor_readings ──────────────────────────────────────────────────
-- The hot path. One row per (sensor, time, value). NULL AWN fields do
-- NOT generate rows here — only actually-reported values.

CREATE TABLE IF NOT EXISTS sensor_readings (
  sensor_id    text NOT NULL REFERENCES sensors(sensor_id),
  observed_at  timestamptz NOT NULL,
  value        double precision NOT NULL,
  PRIMARY KEY (sensor_id, observed_at)
);

CREATE INDEX IF NOT EXISTS sensor_readings_observed_at_brin
  ON sensor_readings USING brin (observed_at);

COMMENT ON TABLE sensor_readings IS
  'Narrow time-series of sensor values. Join to sensors to resolve location / unit / display name.';

-- ─── Convenience view: latest reading per sensor ──────────────────────
-- Lets curated tools answer "current value for sensor X" without
-- writing a window function each time.

CREATE OR REPLACE VIEW latest_sensor_readings AS
SELECT s.sensor_id, r.observed_at, r.value
  FROM sensors s
  CROSS JOIN LATERAL (
    SELECT observed_at, value
      FROM sensor_readings
     WHERE sensor_id = s.sensor_id
     ORDER BY observed_at DESC
     LIMIT 1
  ) r;

COMMENT ON VIEW latest_sensor_readings IS
  'Most recent reading for each sensor. LATERAL form forces an Index Scan '
  'Backward on sensor_readings_pkey per sensor (≈1ms total for ~40 sensors). '
  'Do NOT use the DISTINCT ON form — Postgres''s planner ignores the index '
  'and does a 280MB external merge sort, taking ~2 minutes.';

COMMIT;
