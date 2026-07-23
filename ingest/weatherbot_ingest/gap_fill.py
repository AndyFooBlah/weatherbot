"""Gap filling — synthesize estimated readings for measured-data outages.

When the backyard array loses its RF link, no reading is captured for the
outage window. Those gaps drag down any average/min/max that spans them.
This module fills a *closed* gap (one with a real reading on both sides)
with estimated readings at the nominal 5-minute cadence, tagged
is_estimated=true / estimation_method='diurnal_interp' so they are never
mistaken for measurements.

Method — seasonal (diurnal) interpolation:
  For a gap from t0 (value v0) to t1 (value v1), and a per-sensor daily
  profile P[tod] (average value by local time-of-day over recent history):

    linear(t)   = v0 + (v1 - v0) * frac              # straight line v0→v1
    p_linear(t) = P[tod(t0)] + (P[tod(t1)] - P[tod(t0)]) * frac
    est(t)      = linear(t) + ( P[tod(t)] - p_linear(t) )

  where frac = (t - t0) / (t1 - t0). The residual term bends the straight
  line to follow the day's shape (e.g. the overnight dip), and is zero at
  both endpoints, so est(t0) = v0 and est(t1) = v1 — continuous with the
  real data on either side.

Only closed gaps are filled. An ongoing outage (sensor offline now, no
right endpoint) can't be interpolated and is skipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import connect

GAP_THRESHOLD_SECONDS = 15 * 60
NOMINAL_INTERVAL = timedelta(minutes=5)
LOCAL_TZ = "America/Los_Angeles"
PROFILE_DAYS = 30          # history window for the diurnal profile
TOD_BUCKET_MINUTES = 5     # profile resolution


@dataclass
class ClosedGap:
    t0: datetime
    v0: float
    t1: datetime
    v1: float

    @property
    def hours(self) -> float:
        return (self.t1 - self.t0).total_seconds() / 3600.0


def _tod_bucket(dt_local: datetime) -> int:
    """Local time-of-day → 5-minute bucket index (0..287)."""
    return (dt_local.hour * 60 + dt_local.minute) // TOD_BUCKET_MINUTES


def find_closed_gaps(
    conn, sensor_id: str, since: datetime, max_gap_hours: float
) -> list[ClosedGap]:
    """Closed gaps (real reading on both sides) for one sensor since `since`."""
    cur = conn.cursor()
    cur.execute(
        """
        WITH r AS (
          SELECT observed_at, value,
                 lag(observed_at) OVER (ORDER BY observed_at) AS prev_at,
                 lag(value)       OVER (ORDER BY observed_at) AS prev_val
            FROM sensor_readings
           WHERE sensor_id = %s AND is_estimated = false
             AND observed_at >= %s
        )
        SELECT prev_at, prev_val, observed_at, value
          FROM r
         WHERE observed_at - prev_at > (%s * interval '1 second')
           AND observed_at - prev_at <= (%s * interval '1 hour')
         ORDER BY prev_at
        """,
        (sensor_id, since, GAP_THRESHOLD_SECONDS, max_gap_hours),
    )
    return [
        ClosedGap(t0=p0, v0=float(pv), t1=t1, v1=float(v1))
        for (p0, pv, t1, v1) in cur.fetchall()
        if pv is not None and v1 is not None
    ]


def diurnal_profile(conn, sensor_id: str) -> dict[int, float]:
    """Average value by local 5-min time-of-day bucket over recent history."""
    cur = conn.cursor()
    since = datetime.now(timezone.utc) - timedelta(days=PROFILE_DAYS)
    cur.execute(
        """
        SELECT floor(
                 (EXTRACT(HOUR   FROM observed_at AT TIME ZONE %s) * 60 +
                  EXTRACT(MINUTE FROM observed_at AT TIME ZONE %s)) / %s
               )::int AS bucket,
               avg(value) AS avg_value
          FROM sensor_readings
         WHERE sensor_id = %s AND is_estimated = false
           AND observed_at >= %s
         GROUP BY 1
        """,
        (LOCAL_TZ, LOCAL_TZ, TOD_BUCKET_MINUTES, sensor_id, since),
    )
    return {int(b): float(v) for b, v in cur.fetchall() if v is not None}


def estimate_gap(
    gap: ClosedGap, profile: dict[int, float], tz: ZoneInfo
) -> list[tuple[datetime, float]]:
    """Return [(observed_at, estimated_value)] at 5-min cadence inside the gap."""
    total = (gap.t1 - gap.t0).total_seconds()
    if total <= 0:
        return []

    def prof(dt_utc: datetime, fallback: float) -> float:
        return profile.get(_tod_bucket(dt_utc.astimezone(tz)), fallback)

    p_t0 = prof(gap.t0, gap.v0)
    p_t1 = prof(gap.t1, gap.v1)

    out: list[tuple[datetime, float]] = []
    t = gap.t0 + NOMINAL_INTERVAL
    while t < gap.t1 - timedelta(seconds=1):
        frac = (t - gap.t0).total_seconds() / total
        linear = gap.v0 + (gap.v1 - gap.v0) * frac
        p_linear = p_t0 + (p_t1 - p_t0) * frac
        residual = prof(t, p_linear) - p_linear
        out.append((t, round(linear + residual, 4)))
        t += NOMINAL_INTERVAL
    return out


def fill_gaps(
    sensor_id: str | None = None,
    days_back: int = 120,
    max_gap_hours: float = 48.0,
    dry_run: bool = True,
) -> dict:
    """Fill closed gaps for one sensor (or all active reliable sensors).

    Returns a summary dict. dry_run=True computes and reports without
    writing. Re-running is safe: existing estimated rows for a slot are
    left in place (ON CONFLICT DO NOTHING on the (sensor_id, observed_at)
    primary key).
    """
    tz = ZoneInfo(LOCAL_TZ)
    since = datetime.now(timezone.utc) - timedelta(days=days_back)
    summary = {"sensors": 0, "gaps": 0, "rows_estimated": 0, "samples": []}

    with connect() as conn:
        cur = conn.cursor()
        if sensor_id:
            cur.execute(
                "SELECT sensor_id, display_name FROM sensors WHERE sensor_id = %s",
                (sensor_id,),
            )
        else:
            # Diurnal interpolation only makes sense for continuous
            # measurements. Excluded: battery (a status flag — synthetic
            # battery rows are noise, and 0→0 "estimates" carry no
            # information) and wind_direction (circular 0–360°; linear
            # interpolation across the 359°→1° wrap is simply wrong).
            cur.execute(
                "SELECT sensor_id, display_name FROM sensors "
                "WHERE is_active AND reliable "
                "  AND measurement_type NOT IN ('battery', 'wind_direction') "
                "ORDER BY physical_location, display_name"
            )
        targets = cur.fetchall()

        for sid, name in targets:
            gaps = find_closed_gaps(conn, sid, since, max_gap_hours)
            if not gaps:
                continue
            profile = diurnal_profile(conn, sid)
            summary["sensors"] += 1
            for gap in gaps:
                rows = estimate_gap(gap, profile, tz)
                if not rows:
                    continue
                summary["gaps"] += 1
                summary["rows_estimated"] += len(rows)
                if len(summary["samples"]) < 6 and len(rows) >= 3:
                    mid = rows[len(rows) // 2]
                    summary["samples"].append(
                        {
                            "sensor": name,
                            "gap_hours": round(gap.hours, 1),
                            "from": f"{gap.v0} @ {gap.t0.astimezone(tz):%b %-d %-I:%M%p}",
                            "to": f"{gap.v1} @ {gap.t1.astimezone(tz):%b %-d %-I:%M%p}",
                            "midpoint_est": f"{mid[1]} @ {mid[0].astimezone(tz):%-I:%M%p}",
                            "n_rows": len(rows),
                        }
                    )
                if not dry_run:
                    cur.executemany(
                        """
                        INSERT INTO sensor_readings
                            (sensor_id, observed_at, value, is_estimated, estimation_method)
                        VALUES (%s, %s, %s, true, 'diurnal_interp')
                        ON CONFLICT (sensor_id, observed_at) DO NOTHING
                        """,
                        [(sid, t, v) for (t, v) in rows],
                    )
        if not dry_run:
            conn.commit()

    return summary
