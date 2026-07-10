"""Canonical sensor catalog — AWN field → measurement_type, unit, display name.

Used by:
  - upsert_sensors() to seed/refresh the `sensors` table per station
  - upsert_sensor_readings() to fan out a wide AWN record into narrow
    (sensor_id, observed_at, value) rows
  - backfill scripts to populate `sensors` + `sensor_readings` from the
    legacy wide `observations` table

The shape `(awn_field, measurement_type, unit)` is single-source-of-truth.
`physical_location`, `reliable`, and `notes` are NOT here — those live
on the per-station `sensor_assignments` table (and get copied into
`sensors` row-by-row at upsert time). This keeps the catalog purely
about "what AWN field means," and per-station context stays per-station.
"""

from __future__ import annotations

# (awn_field, measurement_type, unit, default_display_name_suffix)
#
# measurement_type values are canonical and intentionally constrained so
# the agent can filter on a small known set. Adding a new one requires
# updating both this table and any agent guidance that enumerates them.
SENSOR_CATALOG: dict[str, tuple[str, str, str]] = {
    # awn_field        measurement_type      unit        display-suffix
    # ────────────── outdoor block (single outdoor array per station) ───
    "tempf":           ("temperature",        "°F",       "temperature"),
    "feelslike":       ("feels_like",         "°F",       "feels-like"),
    "dewpoint":        ("dew_point",          "°F",       "dew point"),
    "humidity":        ("humidity",           "%",        "humidity"),
    "baromrelin":      ("pressure",           "inHg",     "pressure (relative)"),
    "baromabsin":      ("pressure",           "inHg",     "pressure (absolute)"),
    "windspeedmph":    ("wind_speed",         "mph",      "wind speed"),
    "windgustmph":     ("wind_gust",          "mph",      "wind gust"),
    "maxdailygust":    ("wind_gust",          "mph",      "wind gust (daily max)"),
    "winddir":         ("wind_direction",     "°",        "wind direction"),
    "hourlyrainin":    ("rain_rate",          "in/hr",    "rain (hourly)"),
    "dailyrainin":     ("rain_total",         "in",       "rain (daily total)"),
    "weeklyrainin":    ("rain_total",         "in",       "rain (weekly total)"),
    "monthlyrainin":   ("rain_total",         "in",       "rain (monthly total)"),
    "yearlyrainin":    ("rain_total",         "in",       "rain (yearly total)"),
    "totalrainin":     ("rain_total",         "in",       "rain (cumulative)"),
    "eventrainin":     ("rain_total",         "in",       "rain (current event)"),
    "solarradiation":  ("solar_radiation",    "W/m²",     "solar radiation"),
    "uv":              ("uv_index",           "UV",       "UV index"),
    "battout":         ("battery",            "ok",       "battery (outdoor)"),

    # ────────────── PM2.5 puck (outdoor) ───────────────────────────────
    "pm25":            ("pm25",               "µg/m³",    "PM2.5"),
    "pm25_24h":        ("pm25",               "µg/m³",    "PM2.5 (24h avg)"),
    "aqi_pm25":        ("aqi",                "AQI",      "AQI"),
    "aqi_pm25_24h":    ("aqi",                "AQI",      "AQI (24h avg)"),
    "batt_25":         ("battery",            "ok",       "battery (PM2.5)"),

    # ────────────── Indoor probe (bedroom, on most setups) ─────────────
    "tempinf":         ("temperature",        "°F",       "temperature"),
    "humidityin":      ("humidity",           "%",        "humidity"),
    "dewpointin":      ("dew_point",          "°F",       "dew point"),
    "feelslikein":     ("feels_like",         "°F",       "feels-like"),
    "battin":          ("battery",            "ok",       "battery (indoor probe)"),

    # ────────────── Aux channel 2 (garage, on Andy's setup) ────────────
    "temp2f":          ("temperature",        "°F",       "temperature"),
    "humidity2":       ("humidity",           "%",        "humidity"),
    "dewpoint2":       ("dew_point",          "°F",       "dew point"),
    "feelslike2":      ("feels_like",         "°F",       "feels-like"),
    "batt2":           ("battery",            "ok",       "battery (aux 2)"),

    # ────────────── Aux channel 4 (retired pool sensor) ────────────────
    "temp4f":          ("temperature",        "°F",       "temperature (legacy)"),
    "batt4":           ("battery",            "ok",       "battery (aux 4)"),

    # ────────────── Aux channel 7 (current pool sensor) ────────────────
    "temp7f":          ("temperature",        "°F",       "temperature"),
    "batt7":           ("battery",            "ok",       "battery (aux 7)"),
}


def sensor_id_for(station_id: str, awn_field: str) -> str:
    """Stable composite key. Human-readable in SQL output."""
    return f"{station_id}:{awn_field}"


def is_known_field(awn_field: str) -> bool:
    return awn_field in SENSOR_CATALOG


def split_known_unknown(record: dict) -> tuple[dict[str, float], list[str]]:
    """Partition an AWN record's keys into (known sensor readings, unknown keys).

    Returns:
        readings: {awn_field: float_value} for every catalog key whose value
                  in `record` parses as a finite number.
        unknown:  list of keys in `record` we don't have a catalog entry
                  for (informational — they still land in polls.raw).
    """
    readings: dict[str, float] = {}
    unknown: list[str] = []
    for k, v in record.items():
        if k in SENSOR_CATALOG:
            try:
                fv = float(v) if v is not None else None
            except (TypeError, ValueError):
                fv = None
            if fv is not None:
                readings[k] = fv
        else:
            # Skip the bookkeeping fields that aren't sensors.
            if k in ("dateutc", "date", "lastRain", "tz"):
                continue
            unknown.append(k)
    return readings, unknown
