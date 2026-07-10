-- weatherbot schema v1
-- One row per weather observation from an Ambient Weather station.
-- Designed for time-range queries from an LLM agent.

CREATE TABLE IF NOT EXISTS stations (
  mac_address    text PRIMARY KEY,
  name           text,
  latitude       double precision,
  longitude      double precision,
  timezone       text,
  first_seen_at  timestamptz,
  last_seen_at   timestamptz,
  metadata       jsonb
);

CREATE TABLE IF NOT EXISTS observations (
  station_id     text NOT NULL REFERENCES stations(mac_address),
  observed_at    timestamptz NOT NULL,

  -- Common AWN fields hoisted for query speed / agent SQL generation.
  -- All optional — sensors and station models report different subsets.
  tempf            real,
  feelslike        real,
  dewpoint         real,
  humidity         smallint,
  baromrelin       real,
  baromabsin       real,
  windspeedmph     real,
  windgustmph      real,
  maxdailygust     real,
  winddir          smallint,
  hourlyrainin     real,
  dailyrainin      real,
  weeklyrainin     real,
  monthlyrainin    real,
  yearlyrainin     real,
  totalrainin      real,
  solarradiation   real,
  uv               smallint,
  tempinf          real,
  humidityin       smallint,

  -- Full original payload, so we never lose a field we forgot to hoist.
  raw              jsonb NOT NULL,

  PRIMARY KEY (station_id, observed_at)
);

-- BRIN index is cheap and ideal for monotonically-ish increasing timestamps.
CREATE INDEX IF NOT EXISTS observations_observed_at_brin
  ON observations USING brin (observed_at);

-- Backfill / sync bookkeeping so we can resume after failures.
CREATE TABLE IF NOT EXISTS sync_runs (
  id            bigserial PRIMARY KEY,
  station_id    text NOT NULL REFERENCES stations(mac_address),
  kind          text NOT NULL CHECK (kind IN ('backfill', 'incremental')),
  started_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz,
  window_start  timestamptz,
  window_end    timestamptz,
  rows_inserted integer,
  ok            boolean,
  error         text
);

CREATE INDEX IF NOT EXISTS sync_runs_station_started_idx
  ON sync_runs (station_id, started_at DESC);
