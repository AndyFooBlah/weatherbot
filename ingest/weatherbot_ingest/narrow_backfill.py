"""Backfill the narrow schema (sensors / sensor_readings / polls) from the
legacy wide `observations` table.

Idempotent — safe to re-run. Uses ON CONFLICT DO NOTHING on natural PKs.

Strategy:
  1. polls           ← SELECT station_id, observed_at, raw FROM observations.
  2. sensors         ← derived from sensor_assignments (one row per
                       station × assigned field, with measurement_type +
                       unit + display_name from sensors_catalog).
  3. sensor_readings ← for each AWN field present in the catalog, project
                       the wide column to (sensor_id, observed_at, value)
                       rows. NULL values are skipped (no fabricated zero).

Run from the CLI:
    weatherbot-ingest narrow-backfill
"""

from __future__ import annotations

import logging
from typing import Iterable

import pg8000.dbapi

from .sensors_catalog import SENSOR_CATALOG, sensor_id_for

logger = logging.getLogger(__name__)


def backfill_polls(conn: pg8000.dbapi.Connection) -> int:
    """Copy (station_id, observed_at, raw) from observations into polls."""
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO polls (station_id, observed_at, raw)
            SELECT station_id, observed_at, raw FROM observations
            ON CONFLICT (station_id, observed_at) DO NOTHING
            """
        )
        n = cur.rowcount or 0
    finally:
        cur.close()
    conn.commit()
    return n


def backfill_sensors(conn: pg8000.dbapi.Connection) -> int:
    """Create one sensors row per (station_id, awn_field) in sensor_assignments
    whose AWN field is known to the catalog.

    `is_active` defaults to True for every assignment (operator can flip
    later via UPDATE — e.g. set temp4f.is_active=false to mark the retired
    pool sensor as superseded). first_seen_at/last_seen_at filled from
    observations via subquery.
    """
    cur = conn.cursor()
    try:
        # We can't directly INSERT … SELECT from sensor_assignments + the
        # catalog in pure SQL because the catalog lives in Python. Build a
        # VALUES list instead.
        cur.execute(
            "SELECT station_id, field_name, physical_location, sensor_kind, "
            "reliable, notes FROM sensor_assignments"
        )
        rows = cur.fetchall()

        prepared: list[tuple] = []
        for station_id, field_name, location, sensor_kind, reliable, notes in rows:
            cat = SENSOR_CATALOG.get(field_name)
            if cat is None:
                logger.warning(
                    "[narrow-backfill] sensor_assignments has %s on %s but "
                    "catalog doesn't — skipping",
                    field_name, station_id,
                )
                continue
            measurement_type, unit, display_suffix = cat
            display_name = f"{location} {display_suffix}"
            prepared.append((
                sensor_id_for(station_id, field_name),
                station_id,
                field_name,
                measurement_type,
                location,
                display_name,
                unit,
                sensor_kind,
                reliable,
                notes,
            ))

        if not prepared:
            return 0

        cur.execute(
            "INSERT INTO sensors ("
            "  sensor_id, station_id, awn_field_name, measurement_type, "
            "  physical_location, display_name, unit, sensor_kind, "
            "  reliable, notes"
            ") VALUES "
            + ", ".join(["(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"] * len(prepared))
            + " ON CONFLICT (sensor_id) DO UPDATE SET "
            "  measurement_type  = EXCLUDED.measurement_type, "
            "  physical_location = EXCLUDED.physical_location, "
            "  display_name      = EXCLUDED.display_name, "
            "  unit              = EXCLUDED.unit, "
            "  sensor_kind       = EXCLUDED.sensor_kind, "
            "  reliable          = EXCLUDED.reliable, "
            "  notes             = EXCLUDED.notes",
            [v for row in prepared for v in row],
        )
        n = len(prepared)

        # Fill first_seen_at / last_seen_at from sensor_readings AFTER it
        # populates (caller runs backfill_readings next).
    finally:
        cur.close()
    conn.commit()
    return n


def backfill_readings(
    conn: pg8000.dbapi.Connection,
    awn_fields: Iterable[str] | None = None,
) -> dict[str, int]:
    """For each AWN field with a hoisted column in `observations`, project
    that column into sensor_readings as (sensor_id, observed_at, value).

    Returns: {awn_field: rows_inserted}
    """
    fields = list(awn_fields) if awn_fields is not None else list(SENSOR_CATALOG.keys())
    inserted: dict[str, int] = {}

    cur = conn.cursor()
    try:
        for awn_field in fields:
            # Confirm the column exists on observations — some catalog
            # entries (future sensors) may not be hoisted as wide columns.
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'observations' AND column_name = %s",
                (awn_field,),
            )
            if not cur.fetchone():
                logger.info(
                    "[narrow-backfill] no observations.%s column — skipping",
                    awn_field,
                )
                continue

            # sensor_id_for() is "<mac>:<awn_field>" — we can build that in
            # SQL via concat. Skip NULLs and let ON CONFLICT swallow dupes.
            sql = (
                f"INSERT INTO sensor_readings (sensor_id, observed_at, value) "
                f"SELECT station_id || ':{awn_field}' AS sensor_id, "
                f"       observed_at, "
                f"       {awn_field}::double precision AS value "
                f"  FROM observations "
                f" WHERE {awn_field} IS NOT NULL "
                f"ON CONFLICT (sensor_id, observed_at) DO NOTHING"
            )
            cur.execute(sql)
            n = cur.rowcount or 0
            inserted[awn_field] = n
            conn.commit()
            logger.info("[narrow-backfill] %-16s +%9d readings", awn_field, n)
    finally:
        cur.close()
    return inserted


def fill_sensor_time_bounds(conn: pg8000.dbapi.Connection) -> int:
    """Populate sensors.first_seen_at / last_seen_at from sensor_readings."""
    cur = conn.cursor()
    try:
        cur.execute(
            """
            UPDATE sensors s
               SET first_seen_at = b.first_obs,
                   last_seen_at  = b.last_obs
              FROM (
                SELECT sensor_id,
                       min(observed_at) AS first_obs,
                       max(observed_at) AS last_obs
                  FROM sensor_readings
                 GROUP BY sensor_id
              ) b
             WHERE s.sensor_id = b.sensor_id
            """
        )
        n = cur.rowcount or 0
    finally:
        cur.close()
    conn.commit()
    return n


def run_full_backfill(conn: pg8000.dbapi.Connection) -> dict[str, int]:
    """Run all four backfill steps in order. Returns per-step counts."""
    results: dict[str, int] = {}
    logger.info("[narrow-backfill] step 1/4: polls")
    results["polls"] = backfill_polls(conn)
    logger.info("[narrow-backfill] step 2/4: sensors")
    results["sensors"] = backfill_sensors(conn)
    logger.info("[narrow-backfill] step 3/4: sensor_readings")
    per_field = backfill_readings(conn)
    results["sensor_readings_total"] = sum(per_field.values())
    results["sensor_readings_by_field"] = per_field  # type: ignore[assignment]
    logger.info("[narrow-backfill] step 4/4: fill sensor time bounds")
    results["sensor_time_bounds_updated"] = fill_sensor_time_bounds(conn)
    return results
