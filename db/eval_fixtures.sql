-- Synthetic fixtures for the eval database (weatherbot_eval ONLY —
-- applied by infra/07-seed-eval-db.sh after the time-shifted prod
-- snapshot lands). Everything here is defined RELATIVE to now() so the
-- fixtures stay meaningful no matter when the DB is re-seeded, and the
-- eval harness can compute ground truth with the same expressions.
--
-- Fixture inventory (keep this list in sync with evals/ cases):
--   F1  known sensor GAP: the busiest outdoor temperature sensor loses
--       readings from 3 days ago 12:00 local to 3 days ago 18:00 local
--       (6h afternoon hole → gap-detection and estimate cases).
--   F2  three known events at fixed offsets (event-recall cases with
--       exact expected answers).
--   F3  eval_fixtures manifest table recording what F1 deleted, so
--       ground-truth SQL never has to re-derive it.

BEGIN;

CREATE TABLE IF NOT EXISTS eval_fixtures (
  fixture_id text PRIMARY KEY,
  detail     jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- F1: carve a 6-hour afternoon gap, 3 days ago, into the outdoor
-- temperature sensor with the most readings.
DO $$
DECLARE
  target_sensor text;
  gap_start timestamptz;
  gap_end   timestamptz;
  removed   integer;
BEGIN
  SELECT r.sensor_id INTO target_sensor
    FROM sensor_readings r
    JOIN sensors s ON s.sensor_id = r.sensor_id
   WHERE s.physical_location = 'Outdoor'
     AND s.measurement_type = 'temperature'
     AND s.is_active AND s.reliable
   GROUP BY r.sensor_id
   ORDER BY count(*) DESC
   LIMIT 1;

  IF target_sensor IS NULL THEN
    RAISE EXCEPTION 'no outdoor temperature sensor found for fixture F1';
  END IF;

  gap_start := date_trunc('day', now() AT TIME ZONE 'America/Los_Angeles')::timestamp
                 AT TIME ZONE 'America/Los_Angeles'
               - interval '3 days' + interval '12 hours';
  gap_end := gap_start + interval '6 hours';

  DELETE FROM sensor_readings
   WHERE sensor_id = target_sensor
     AND observed_at >= gap_start
     AND observed_at <  gap_end;
  GET DIAGNOSTICS removed = ROW_COUNT;

  INSERT INTO eval_fixtures (fixture_id, detail)
  VALUES ('F1_outdoor_gap', jsonb_build_object(
            'sensor_id', target_sensor,
            'gap_start_utc', gap_start,
            'gap_end_utc', gap_end,
            'rows_removed', removed))
  ON CONFLICT (fixture_id) DO UPDATE SET detail = EXCLUDED.detail;

  RAISE NOTICE 'F1: removed % rows from % between % and %',
    removed, target_sensor, gap_start, gap_end;
END $$;

-- F2: known events at fixed offsets from now (all source='eval_fixture'
-- so cases can filter them from organic seeded events).
DELETE FROM events WHERE source = 'eval_fixture';
INSERT INTO events (occurred_at, note, category, source) VALUES
  (date_trunc('hour', now()) - interval '2 days 3 hours',
   'Replaced the batteries in the outdoor sensor array',
   'sensor', 'eval_fixture'),
  (date_trunc('hour', now()) - interval '5 days 8 hours',
   'Cleaned the pool filter',
   'pool', 'eval_fixture'),
  (date_trunc('hour', now()) - interval '9 days 1 hour',
   'Power outage for about twenty minutes',
   'power', 'eval_fixture');

INSERT INTO eval_fixtures (fixture_id, detail)
VALUES ('F2_known_events', jsonb_build_object(
          'count', 3,
          'offsets', jsonb_build_array('2d3h', '5d8h', '9d1h')))
ON CONFLICT (fixture_id) DO UPDATE SET detail = EXCLUDED.detail;

COMMIT;
