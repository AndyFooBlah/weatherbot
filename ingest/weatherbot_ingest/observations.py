"""Mapping AWN records → observations rows, and idempotent upsert.

AWN field names are mostly lowercase but a few are camelCase (`feelsLike`,
`dewPoint`). The hoisted-column spec accepts a tuple of aliases so we tolerate
either form. Anything we don't hoist is preserved verbatim in `raw jsonb`.

After migration 002 we ALSO dual-write to the narrow schema (polls /
sensors / sensor_readings). The wide observations table stays as a
safety net for now; tools have been switched to read from the narrow
schema. See `sensors_catalog.py` for the canonical AWN-field map.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import pg8000.dbapi

from . import sensors_catalog
from .sensors_catalog import SENSOR_CATALOG, sensor_id_for

logger = logging.getLogger(__name__)


# AWN-field alias map for the canonical-name normalization step below.
# Same source-of-truth as COLUMN_SPECS but keyed by canonical (db) name.
_AWN_ALIASES: dict[str, tuple[str, ...]] = {
    "feelslike":    ("feelsLike", "feelslike"),
    "dewpoint":     ("dewPoint", "dewpoint"),
    "dewpointin":   ("dewPointin", "dewpointin"),
    "feelslikein":  ("feelsLikein", "feelslikein"),
    "dewpoint2":    ("dewPoint2", "dewpoint2"),
    "feelslike2":   ("feelsLike2", "feelslike2"),
}


def _normalize_awn_keys(record: dict[str, Any]) -> dict[str, Any]:
    """Rewrite camelCase AWN keys to their canonical lowercase form.

    Lets the narrow upsert path use sensors_catalog (which only knows
    canonical keys) without re-implementing the alias dance from
    COLUMN_SPECS. The dict is shallow-copied — the original AWN record
    is left intact for raw-payload storage.
    """
    out = dict(record)
    for canonical, aliases in _AWN_ALIASES.items():
        if canonical in out:
            continue
        for a in aliases:
            if a in out:
                out[canonical] = out[a]
                break
    return out


# (db_column, awn_field_aliases) — first alias found wins.
COLUMN_SPECS: list[tuple[str, tuple[str, ...]]] = [
    ("tempf",          ("tempf",)),
    ("feelslike",      ("feelsLike", "feelslike")),
    ("dewpoint",       ("dewPoint", "dewpoint")),
    ("humidity",       ("humidity",)),
    ("baromrelin",     ("baromrelin",)),
    ("baromabsin",     ("baromabsin",)),
    ("windspeedmph",   ("windspeedmph",)),
    ("windgustmph",    ("windgustmph",)),
    ("maxdailygust",   ("maxdailygust",)),
    ("winddir",        ("winddir",)),
    ("hourlyrainin",   ("hourlyrainin",)),
    ("dailyrainin",    ("dailyrainin",)),
    ("weeklyrainin",   ("weeklyrainin",)),
    ("monthlyrainin",  ("monthlyrainin",)),
    ("yearlyrainin",   ("yearlyrainin",)),
    ("totalrainin",    ("totalrainin",)),
    ("solarradiation", ("solarradiation",)),
    ("uv",             ("uv",)),
    ("eventrainin",    ("eventrainin",)),
    ("battout",        ("battout",)),
    # Bedroom (AWN "indoor" probe)
    ("tempinf",        ("tempinf",)),
    ("humidityin",     ("humidityin",)),
    ("dewpointin",     ("dewPointin", "dewpointin")),
    ("feelslikein",    ("feelsLikein", "feelslikein")),
    ("battin",         ("battin",)),
    # Outdoor PM2.5 puck
    ("pm25",           ("pm25",)),
    ("pm25_24h",       ("pm25_24h",)),
    ("aqi_pm25",       ("aqi_pm25",)),
    ("aqi_pm25_24h",   ("aqi_pm25_24h",)),
    ("batt_25",        ("batt_25",)),
    # Garage — aux channel 2
    ("temp2f",         ("temp2f",)),
    ("humidity2",      ("humidity2",)),
    ("dewpoint2",      ("dewPoint2", "dewpoint2")),
    ("feelslike2",     ("feelsLike2", "feelslike2")),
    ("batt2",          ("batt2",)),
    # Retired Pool sensor — aux channel 4
    ("temp4f",         ("temp4f",)),
    ("batt4",          ("batt4",)),
    # Current Pool sensor — aux channel 7
    ("temp7f",         ("temp7f",)),
    ("batt7",          ("batt7",)),
]

_ALL_COLUMNS = ["station_id", "observed_at"] + [c for c, _ in COLUMN_SPECS] + ["raw"]
_COLUMN_LIST = ", ".join(_ALL_COLUMNS)
_ROW_PLACEHOLDER = "(" + ", ".join(["%s"] * len(_ALL_COLUMNS)) + ")"

# Postgres types for each hoisted column. Used by the `rehoist` CLI command
# to materialize NULL columns from `raw jsonb` for historical rows. The
# upsert path doesn't need this — pg8000 converts Python values directly.
COLUMN_TYPES: dict[str, str] = {
    # Floats — temperatures, pressures, wind speeds, rain amounts, solar W/m².
    "tempf": "real", "feelslike": "real", "dewpoint": "real",
    "tempinf": "real", "dewpointin": "real", "feelslikein": "real",
    "temp2f": "real", "dewpoint2": "real", "feelslike2": "real",
    "temp4f": "real", "temp7f": "real",
    "baromrelin": "real", "baromabsin": "real",
    "windspeedmph": "real", "windgustmph": "real", "maxdailygust": "real",
    "hourlyrainin": "real", "dailyrainin": "real", "weeklyrainin": "real",
    "monthlyrainin": "real", "yearlyrainin": "real", "totalrainin": "real",
    "eventrainin": "real",
    "solarradiation": "real",
    "pm25": "real", "pm25_24h": "real",
    # Small ints — humidity %, wind direction °, UV index, AQI, battery 0/1.
    "humidity": "smallint", "humidityin": "smallint", "humidity2": "smallint",
    "winddir": "smallint", "uv": "smallint",
    "aqi_pm25": "smallint", "aqi_pm25_24h": "smallint",
    "batt2": "smallint", "batt4": "smallint", "batt7": "smallint",
    "batt_25": "smallint", "battin": "smallint", "battout": "smallint",
}


def _first(record: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    for a in aliases:
        if a in record:
            return record[a]
    return None


def _row_tuple(station_id: str, record: dict[str, Any]) -> tuple | None:
    ts_ms = record.get("dateutc")
    if ts_ms is None:
        return None
    observed_at = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)

    values: list[Any] = [station_id, observed_at]
    values.extend(_first(record, aliases) for _, aliases in COLUMN_SPECS)
    values.append(json.dumps(record))  # raw jsonb — pg8000 wants a string
    return tuple(values)


def upsert_observations(
    conn: pg8000.dbapi.Connection,
    station_id: str,
    records: list[dict[str, Any]],
) -> int:
    """Insert records with ON CONFLICT DO NOTHING. Returns count of new rows.

    Uses a single multi-row INSERT to avoid one DB round-trip per record
    (pg8000's executemany isn't pipelined). RETURNING + fetchall() length
    gives an exact count of rows that won the conflict.

    Dual-writes the same records to the narrow schema (polls / sensors /
    sensor_readings) in the same transaction so downstream tools that have
    cut over to the narrow tables stay current. Wide-table behaviour is
    unchanged.
    """
    rows = [r for r in (_row_tuple(station_id, rec) for rec in records) if r is not None]
    if not rows:
        return 0

    values_clause = ", ".join([_ROW_PLACEHOLDER] * len(rows))
    sql = (
        f"INSERT INTO observations ({_COLUMN_LIST}) "
        f"VALUES {values_clause} "
        f"ON CONFLICT (station_id, observed_at) DO NOTHING "
        f"RETURNING 1"
    )
    flat_params: list[Any] = [v for row in rows for v in row]

    cur = conn.cursor()
    try:
        cur.execute(sql, flat_params)
        inserted = len(cur.fetchall())
    finally:
        cur.close()

    # Commit the wide insert before attempting the narrow dual-write: the
    # wide table is canonical, and committing first guarantees a narrow-schema
    # failure can only ever lose narrow rows, never the wide ones.
    conn.commit()

    # Narrow-schema dual-write (best-effort). Errors roll back only the
    # narrow inserts — the wide insert is already committed above.
    try:
        _dual_write_narrow(conn, station_id, records)
    except Exception:
        logger.exception(
            "[observations] narrow-schema dual-write failed for station=%s "
            "(wide insert is already committed)", station_id,
        )
        conn.rollback()
    else:
        conn.commit()
    return inserted


# ─────────────────────────────────────────────────────────────────────────
# Narrow-schema (polls / sensors / sensor_readings) upsert path.
# ─────────────────────────────────────────────────────────────────────────

def _dual_write_narrow(
    conn: pg8000.dbapi.Connection,
    station_id: str,
    records: list[dict[str, Any]],
) -> None:
    """Fan out the same AWN records into polls + sensor_readings.

    Idempotent (ON CONFLICT DO NOTHING on the natural PKs). `sensors`
    rows for any new AWN field seen are auto-upserted from the catalog
    plus any matching sensor_assignments metadata.
    """
    if not records:
        return

    # 1. polls — one row per (station, observed_at) carrying the raw payload.
    poll_rows: list[tuple] = []
    for rec in records:
        ts_ms = rec.get("dateutc")
        if ts_ms is None:
            continue
        observed_at = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        poll_rows.append((station_id, observed_at, json.dumps(rec)))

    if poll_rows:
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO polls (station_id, observed_at, raw) VALUES "
                + ", ".join(["(%s, %s, %s)"] * len(poll_rows))
                + " ON CONFLICT (station_id, observed_at) DO NOTHING",
                [v for row in poll_rows for v in row],
            )
        finally:
            cur.close()

    # 2. Determine the full set of AWN fields present across the batch.
    # Auto-upsert sensors rows for any new ones (idempotent).
    seen_fields: set[str] = set()
    for rec in records:
        norm = _normalize_awn_keys(rec)
        readings, _unknown = sensors_catalog.split_known_unknown(norm)
        seen_fields.update(readings.keys())
    if seen_fields:
        _upsert_sensors(conn, station_id, sorted(seen_fields))

    # 3. sensor_readings — one row per (sensor_id, observed_at, value).
    reading_rows: list[tuple] = []
    for rec in records:
        ts_ms = rec.get("dateutc")
        if ts_ms is None:
            continue
        observed_at = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        norm = _normalize_awn_keys(rec)
        readings, _unknown = sensors_catalog.split_known_unknown(norm)
        for awn_field, value in readings.items():
            sid = sensor_id_for(station_id, awn_field)
            reading_rows.append((sid, observed_at, value))

    if reading_rows:
        # Multi-row INSERT in batches of 1000 to keep param count under
        # pg8000's default limit (Postgres allows up to 65535 placeholders;
        # 3 per row → ~21k rows per batch is the hard ceiling, but 1k is
        # a comfortable working size).
        BATCH = 1000
        cur = conn.cursor()
        try:
            for i in range(0, len(reading_rows), BATCH):
                chunk = reading_rows[i : i + BATCH]
                cur.execute(
                    "INSERT INTO sensor_readings (sensor_id, observed_at, value) VALUES "
                    + ", ".join(["(%s, %s, %s)"] * len(chunk))
                    + " ON CONFLICT (sensor_id, observed_at) DO NOTHING",
                    [v for row in chunk for v in row],
                )
        finally:
            cur.close()


def _upsert_sensors(
    conn: pg8000.dbapi.Connection,
    station_id: str,
    awn_fields: list[str],
) -> None:
    """Ensure a `sensors` row exists for each (station, awn_field) seen.

    Pulls physical_location / sensor_kind / reliable / notes from the
    parallel `sensor_assignments` table (still maintained for the legacy
    tools); if no assignment exists, falls back to "Unassigned" so the
    pipeline keeps moving and the operator can fix it later.
    """
    if not awn_fields:
        return

    # Pull all assignments for the station in one round-trip.
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT field_name, physical_location, sensor_kind, reliable, notes "
            "FROM sensor_assignments WHERE station_id = %s",
            (station_id,),
        )
        assignments: dict[str, tuple] = {
            row[0]: (row[1], row[2], row[3], row[4]) for row in cur.fetchall()
        }
    finally:
        cur.close()

    rows: list[tuple] = []
    for awn_field in awn_fields:
        cat = SENSOR_CATALOG.get(awn_field)
        if cat is None:
            continue
        measurement_type, unit, display_suffix = cat
        location, sensor_kind, reliable, notes = assignments.get(
            awn_field, ("Unassigned", None, True, None),
        )
        display_name = f"{location} {display_suffix}"
        rows.append((
            sensor_id_for(station_id, awn_field),
            station_id,
            awn_field,
            measurement_type,
            location,
            display_name,
            unit,
            sensor_kind,
            reliable,
            notes,
        ))

    if not rows:
        return

    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO sensors ("
            "  sensor_id, station_id, awn_field_name, measurement_type, "
            "  physical_location, display_name, unit, sensor_kind, "
            "  reliable, notes"
            ") VALUES "
            + ", ".join(["(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"] * len(rows))
            + " ON CONFLICT (sensor_id) DO UPDATE SET "
            "  measurement_type  = EXCLUDED.measurement_type, "
            "  physical_location = EXCLUDED.physical_location, "
            "  display_name      = EXCLUDED.display_name, "
            "  unit              = EXCLUDED.unit, "
            "  sensor_kind       = EXCLUDED.sensor_kind, "
            "  reliable          = EXCLUDED.reliable, "
            "  notes             = EXCLUDED.notes, "
            "  last_seen_at      = now(), "
            "  first_seen_at     = COALESCE(sensors.first_seen_at, now())",
            [v for row in rows for v in row],
        )
    finally:
        cur.close()


def upsert_station(
    conn: pg8000.dbapi.Connection,
    mac_address: str,
    info: dict[str, Any],
    last_data: dict[str, Any],
) -> None:
    """Insert or update the stations row from an AWN /devices response."""
    coords_obj = info.get("coords") or {}

    # Primary: info.coords.coords.{lat,lon}
    inner = coords_obj.get("coords") or {}
    latitude = inner.get("lat") if isinstance(inner, dict) else None
    longitude = inner.get("lon") if isinstance(inner, dict) else None

    # Fallback: info.coords.geo.coordinates = [lon, lat] (GeoJSON)
    if latitude is None or longitude is None:
        geo = coords_obj.get("geo") or {}
        gc = geo.get("coordinates") if isinstance(geo, dict) else None
        if isinstance(gc, list) and len(gc) >= 2:
            longitude = longitude if longitude is not None else gc[0]
            latitude = latitude if latitude is not None else gc[1]

    # AWN doesn't reliably surface timezone in /devices; leave NULL if absent.
    tz_str = info.get("timezone") if isinstance(info.get("timezone"), str) else None

    last_ts_ms = last_data.get("dateutc")
    last_seen = (
        datetime.fromtimestamp(last_ts_ms / 1000, tz=timezone.utc)
        if last_ts_ms else None
    )

    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO stations (
              mac_address, name, latitude, longitude, timezone,
              first_seen_at, last_seen_at, metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (mac_address) DO UPDATE SET
              name          = EXCLUDED.name,
              latitude      = COALESCE(EXCLUDED.latitude,  stations.latitude),
              longitude     = COALESCE(EXCLUDED.longitude, stations.longitude),
              timezone      = COALESCE(EXCLUDED.timezone,  stations.timezone),
              last_seen_at  = GREATEST(stations.last_seen_at, EXCLUDED.last_seen_at),
              metadata      = EXCLUDED.metadata
            """,
            (
                mac_address,
                info.get("name"),
                latitude,
                longitude,
                tz_str,
                last_seen,
                last_seen,
                json.dumps(info),
            ),
        )
    finally:
        cur.close()
    conn.commit()


def get_oldest_observed_at(
    conn: pg8000.dbapi.Connection, station_id: str
) -> datetime | None:
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT min(observed_at) FROM observations WHERE station_id = %s",
            (station_id,),
        )
        row = cur.fetchone()
        return row[0] if row and row[0] else None
    finally:
        cur.close()
