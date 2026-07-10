-- Migration 001 — sensor metadata
--
-- 1. Hoist newly identified AWN fields into typed columns on observations.
--    Driven by field-stats discovery on station Home2 (see issue #1).
-- 2. Add sensor_assignments — per-station mapping of AWN field names to
--    physical sensor identities and reliability state.
-- 3. Add unmonitored_locations — places that are NOT covered by AWN, so
--    the agent doesn't fabricate readings.
--
-- Station identity: this file never hardcodes a MAC address. The seed rows
-- reference the psql variable :'station_mac', which db/migrate.sh passes in
-- from STATION_MAC (set in infra/env.sh). Running this file directly requires
--   psql -v station_mac="$STATION_MAC" -f 001_sensor_metadata.sql

BEGIN;

-- ─── 1. New typed columns on observations ────────────────────────────────

-- Outdoor PM2.5 sensor (puck on the outdoor array)
ALTER TABLE observations ADD COLUMN IF NOT EXISTS pm25           real;
ALTER TABLE observations ADD COLUMN IF NOT EXISTS pm25_24h       real;
ALTER TABLE observations ADD COLUMN IF NOT EXISTS aqi_pm25       smallint;
ALTER TABLE observations ADD COLUMN IF NOT EXISTS aqi_pm25_24h   smallint;
ALTER TABLE observations ADD COLUMN IF NOT EXISTS batt_25        smallint;

-- Outdoor station battery indicator
ALTER TABLE observations ADD COLUMN IF NOT EXISTS battout        smallint;

-- Indoor wireless probe — physically in the bedroom
ALTER TABLE observations ADD COLUMN IF NOT EXISTS dewpointin     real;
ALTER TABLE observations ADD COLUMN IF NOT EXISTS feelslikein    real;
ALTER TABLE observations ADD COLUMN IF NOT EXISTS battin         smallint;

-- Aux channel 2 — Garage (temp + humidity)
ALTER TABLE observations ADD COLUMN IF NOT EXISTS temp2f         real;
ALTER TABLE observations ADD COLUMN IF NOT EXISTS humidity2      smallint;
ALTER TABLE observations ADD COLUMN IF NOT EXISTS dewpoint2      real;
ALTER TABLE observations ADD COLUMN IF NOT EXISTS feelslike2     real;
ALTER TABLE observations ADD COLUMN IF NOT EXISTS batt2          smallint;

-- Aux channel 4 — retired Pool sensor (kept for history)
ALTER TABLE observations ADD COLUMN IF NOT EXISTS temp4f         real;
ALTER TABLE observations ADD COLUMN IF NOT EXISTS batt4          smallint;

-- Aux channel 7 — current Pool sensor (temp only)
ALTER TABLE observations ADD COLUMN IF NOT EXISTS temp7f         real;
ALTER TABLE observations ADD COLUMN IF NOT EXISTS batt7          smallint;

-- Rain event running total — survived the rain gauge's death (stuck value)
ALTER TABLE observations ADD COLUMN IF NOT EXISTS eventrainin    real;


-- ─── 2. sensor_assignments ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS sensor_assignments (
  station_id        text NOT NULL REFERENCES stations(mac_address),
  field_name        text NOT NULL,
  sensor_kind       text NOT NULL,   -- 'outdoor', 'rain', 'pm25',
                                     -- 'aux_temp_humidity', 'aux_temp_only', ...
  physical_location text,            -- 'Outdoor', 'Bedroom', 'Garage', 'Pool'
  reliable          boolean NOT NULL DEFAULT true,
  notes             text,
  PRIMARY KEY (station_id, field_name)
);

INSERT INTO sensor_assignments
  (station_id, field_name, sensor_kind, physical_location, reliable, notes)
VALUES
  -- ─── Home2 integrated outdoor weather station ───
  (:'station_mac', 'tempf',          'outdoor', 'Outdoor', true, NULL),
  (:'station_mac', 'humidity',       'outdoor', 'Outdoor', true, NULL),
  (:'station_mac', 'dewpoint',       'outdoor', 'Outdoor', true, NULL),
  (:'station_mac', 'feelslike',      'outdoor', 'Outdoor', true, NULL),
  (:'station_mac', 'windspeedmph',   'outdoor', 'Outdoor', true, NULL),
  (:'station_mac', 'windgustmph',    'outdoor', 'Outdoor', true, NULL),
  (:'station_mac', 'maxdailygust',   'outdoor', 'Outdoor', true, NULL),
  (:'station_mac', 'winddir',        'outdoor', 'Outdoor', true, NULL),
  (:'station_mac', 'baromrelin',     'outdoor', 'Outdoor', true, NULL),
  (:'station_mac', 'baromabsin',     'outdoor', 'Outdoor', true, NULL),
  (:'station_mac', 'solarradiation', 'outdoor', 'Outdoor', true, NULL),
  (:'station_mac', 'uv',             'outdoor', 'Outdoor', true, NULL),
  (:'station_mac', 'battout',        'outdoor', 'Outdoor', true, NULL),

  -- ─── Rain gauge — broken since 2025-06-10 ───
  (:'station_mac', 'hourlyrainin',   'rain', 'Outdoor', false,
   'Rain gauge reported normally 2024-08-24 → 2025-06-10; sensor offline since.'),
  (:'station_mac', 'dailyrainin',    'rain', 'Outdoor', false,
   'Rain gauge offline since 2025-06-10.'),
  (:'station_mac', 'weeklyrainin',   'rain', 'Outdoor', false,
   'Rain gauge offline since 2025-06-10.'),
  (:'station_mac', 'monthlyrainin',  'rain', 'Outdoor', false,
   'Rain gauge offline since 2025-06-10.'),
  (:'station_mac', 'yearlyrainin',   'rain', 'Outdoor', false,
   'Rain gauge offline since 2025-06-10.'),
  (:'station_mac', 'totalrainin',    'rain', 'Outdoor', false,
   'Rain gauge offline since 2025-06-10.'),
  (:'station_mac', 'eventrainin',    'rain', 'Outdoor', false,
   'Stuck at last value (≈258.01 in) since rain gauge died 2025-06-10.'),

  -- ─── Outdoor PM2.5 puck ───
  (:'station_mac', 'pm25',           'pm25', 'Outdoor', true, NULL),
  (:'station_mac', 'pm25_24h',       'pm25', 'Outdoor', true, NULL),
  (:'station_mac', 'aqi_pm25',       'pm25', 'Outdoor', true, NULL),
  (:'station_mac', 'aqi_pm25_24h',   'pm25', 'Outdoor', true, NULL),
  (:'station_mac', 'batt_25',        'pm25', 'Outdoor', true, NULL),

  -- ─── Bedroom: detached wireless probe (AWN labels it "indoor") ───
  (:'station_mac', 'tempinf',        'aux_temp_humidity', 'Bedroom', true,
   'AWN base-station "indoor" probe; physically placed in the bedroom.'),
  (:'station_mac', 'humidityin',     'aux_temp_humidity', 'Bedroom', true, NULL),
  (:'station_mac', 'dewpointin',     'aux_temp_humidity', 'Bedroom', true, NULL),
  (:'station_mac', 'feelslikein',    'aux_temp_humidity', 'Bedroom', true, NULL),
  (:'station_mac', 'battin',         'aux_temp_humidity', 'Bedroom', true, NULL),

  -- ─── Aux channel 2: Garage ───
  (:'station_mac', 'temp2f',         'aux_temp_humidity', 'Garage', true, NULL),
  (:'station_mac', 'humidity2',      'aux_temp_humidity', 'Garage', true, NULL),
  (:'station_mac', 'dewpoint2',      'aux_temp_humidity', 'Garage', true, NULL),
  (:'station_mac', 'feelslike2',     'aux_temp_humidity', 'Garage', true, NULL),
  (:'station_mac', 'batt2',          'aux_temp_humidity', 'Garage', true, NULL),

  -- ─── Aux channel 7: current Pool sensor (temp only) ───
  (:'station_mac', 'temp7f',         'aux_temp_only', 'Pool', true, NULL),
  (:'station_mac', 'batt7',          'aux_temp_only', 'Pool', true, NULL),

  -- ─── Aux channel 4: retired Pool sensor ───
  (:'station_mac', 'temp4f',         'aux_temp_only', 'Pool (retired)', false,
   'Active 2024-08-24 → 2025-08-25; replaced by channel 7 the same day.'),
  (:'station_mac', 'batt4',          'aux_temp_only', 'Pool (retired)', false,
   'Sensor retired 2025-08-25.')
ON CONFLICT (station_id, field_name) DO NOTHING;


-- ─── 3. unmonitored_locations ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS unmonitored_locations (
  station_id text NOT NULL REFERENCES stations(mac_address),
  location   text NOT NULL,
  notes      text,
  PRIMARY KEY (station_id, location)
);

INSERT INTO unmonitored_locations (station_id, location, notes)
VALUES
  (:'station_mac', 'Office',
   'AWN console (display unit) is physically in the office, but no AWN sensor measures the office. A non-AWN thermometer there reports ~72.9 F as of 2026-05-30.')
ON CONFLICT (station_id, location) DO NOTHING;

COMMIT;
