"""Daily sensor-outage report.

Finds gaps in each active, reliable sensor's readings over a lookback
window and renders a summary. Can print to stdout (dry run) or email via
the Gmail API.

The backyard sensor array periodically loses its RF link to the base
station, leaving multi-hour gaps where no reading was captured. This
report surfaces those outages within a day instead of weeks later.

Gmail sending reuses the OAuth2 refresh-token pattern (client id/secret +
refresh token in Secret Manager). See docs/gmail-report-setup.md for the
one-time setup that produces the refresh token.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

from . import secrets
from .db import connect

# A "gap" is any interval between consecutive readings longer than this.
# Normal cadence is ~5 min, so 15 min = 3 missed slots is a real outage,
# not jitter.
GAP_THRESHOLD_MINUTES = 15

# Assumed nominal sampling interval, used to estimate "readings lost".
NOMINAL_INTERVAL_SECONDS = 300

LOCAL_TZ = "America/Los_Angeles"


@dataclass
class Gap:
    sensor_id: str
    display_name: str
    physical_location: str
    start_local: datetime
    end_local: datetime
    minutes: float
    readings_lost: int
    ongoing: bool  # True if this "gap" is the sensor being stale right now


@dataclass
class SensorCoverage:
    display_name: str
    physical_location: str
    readings: int
    pct_of_expected: float
    minutes_behind: float  # how stale the newest reading is, in minutes


def find_gaps(hours_back: int = 24) -> tuple[list[Gap], list[SensorCoverage]]:
    """Return (gaps, coverage) for all active reliable sensors over the window.

    `gaps` are intervals > GAP_THRESHOLD_MINUTES between consecutive
    readings, plus one synthetic "ongoing" gap per sensor whose newest
    reading is already older than the threshold. `coverage` is a per-sensor
    summary line.
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours_back)
    tz = ZoneInfo(LOCAL_TZ)

    gaps: list[Gap] = []
    coverage: list[SensorCoverage] = []

    with connect() as conn:
        cur = conn.cursor()

        # Per-sensor gap detection over the window using LAG.
        cur.execute(
            """
            WITH active AS (
              SELECT sensor_id, display_name, physical_location
                FROM sensors
               WHERE is_active AND reliable
            ),
            r AS (
              SELECT sr.sensor_id, sr.observed_at,
                     lag(sr.observed_at) OVER (
                       PARTITION BY sr.sensor_id ORDER BY sr.observed_at
                     ) AS prev_at
                FROM sensor_readings sr
                JOIN active a ON a.sensor_id = sr.sensor_id
               WHERE sr.observed_at >= %s
                 AND sr.is_estimated = false
            )
            SELECT r.sensor_id, a.display_name, a.physical_location,
                   r.prev_at, r.observed_at,
                   EXTRACT(EPOCH FROM (r.observed_at - r.prev_at)) AS gap_seconds
              FROM r JOIN active a ON a.sensor_id = r.sensor_id
             WHERE r.observed_at - r.prev_at > (%s * interval '1 minute')
             ORDER BY (r.observed_at - r.prev_at) DESC
            """,
            (since, GAP_THRESHOLD_MINUTES),
        )
        for sid, name, loc, prev_at, obs_at, gap_seconds in cur.fetchall():
            minutes = float(gap_seconds) / 60.0
            gaps.append(
                Gap(
                    sensor_id=sid,
                    display_name=name,
                    physical_location=loc,
                    start_local=prev_at.astimezone(tz),
                    end_local=obs_at.astimezone(tz),
                    minutes=minutes,
                    readings_lost=max(int(gap_seconds / NOMINAL_INTERVAL_SECONDS) - 1, 0),
                    ongoing=False,
                )
            )

        # Per-sensor coverage + current staleness.
        cur.execute(
            """
            SELECT a.display_name, a.physical_location,
                   count(sr.*) AS readings,
                   max(sr.observed_at) AS last_at
              FROM sensors a
              LEFT JOIN sensor_readings sr
                     ON sr.sensor_id = a.sensor_id
                    AND sr.observed_at >= %s
                    AND sr.is_estimated = false
             WHERE a.is_active AND a.reliable
             GROUP BY a.display_name, a.physical_location
             ORDER BY a.physical_location, a.display_name
            """,
            (since,),
        )
        expected = max(hours_back * 3600 / NOMINAL_INTERVAL_SECONDS, 1)
        for name, loc, readings, last_at in cur.fetchall():
            minutes_behind = (
                (now - last_at).total_seconds() / 60.0 if last_at else float("inf")
            )
            coverage.append(
                SensorCoverage(
                    display_name=name,
                    physical_location=loc,
                    readings=readings or 0,
                    pct_of_expected=round((readings or 0) / expected * 100, 1),
                    minutes_behind=round(minutes_behind, 0),
                )
            )
            # Synthetic "ongoing outage" gap for a currently-stale sensor.
            if last_at and minutes_behind > GAP_THRESHOLD_MINUTES:
                gaps.append(
                    Gap(
                        sensor_id="",
                        display_name=name,
                        physical_location=loc,
                        start_local=last_at.astimezone(tz),
                        end_local=now.astimezone(tz),
                        minutes=minutes_behind,
                        readings_lost=max(
                            int(minutes_behind * 60 / NOMINAL_INTERVAL_SECONDS) - 1, 0
                        ),
                        ongoing=True,
                    )
                )

    return gaps, coverage


@dataclass
class GapGroup:
    """One outage window affecting one or more sensors at a location.

    Sensors on the same station array drop together (same RF link), so a
    whole-array outage produces many identical Gap rows. We collapse them
    into one line ("Outdoor array — 11 sensors") to keep the report
    readable.
    """

    physical_location: str
    start_local: datetime
    end_local: datetime
    minutes: float
    readings_lost: int
    ongoing: bool
    sensor_count: int
    sensor_names: list[str]


def group_gaps(gaps: list[Gap]) -> list[GapGroup]:
    from collections import defaultdict

    buckets: dict[tuple, list[Gap]] = defaultdict(list)
    for g in gaps:
        buckets[(g.physical_location, g.start_local, g.end_local, g.ongoing)].append(g)
    groups = [
        GapGroup(
            physical_location=loc,
            start_local=start,
            end_local=end,
            ongoing=ongoing,
            minutes=gs[0].minutes,
            readings_lost=gs[0].readings_lost,
            sensor_count=len(gs),
            sensor_names=sorted(g.display_name for g in gs),
        )
        for (loc, start, end, ongoing), gs in buckets.items()
    ]
    return sorted(groups, key=lambda x: -x.minutes)


def build_report(
    gaps: list[Gap], coverage: list[SensorCoverage], hours_back: int
) -> tuple[str, str, str]:
    """Return (subject, text_body, html_body)."""
    groups = group_gaps(gaps)
    n_gaps = len(groups)  # count distinct outage windows, not per-sensor rows
    total_lost = sum(g.readings_lost * g.sensor_count for g in groups)
    ongoing = [g for g in groups if g.ongoing]

    if n_gaps == 0:
        subject = "weatherbot: all sensors healthy ✓"
    elif ongoing:
        subject = f"weatherbot: {len(ongoing)} sensor(s) OFFLINE now, {n_gaps} gap(s) in {hours_back}h"
    else:
        subject = f"weatherbot: {n_gaps} sensor gap(s) in last {hours_back}h"

    def fmt(dt: datetime) -> str:
        return dt.strftime("%a %b %-d, %-I:%M %p")

    def who(g: GapGroup) -> str:
        # "Outdoor array (11 sensors)" when a whole array drops; the single
        # sensor name when it's just one.
        if g.sensor_count == 1:
            return g.sensor_names[0]
        return f"{g.physical_location} array ({g.sensor_count} sensors)"

    # ---- text ----
    lines = [f"Weatherbot sensor-outage report — last {hours_back} hours", ""]
    if not groups:
        lines.append("No gaps over 15 minutes on any active reliable sensor. ✓")
    else:
        lines.append(f"{n_gaps} outage window(s), ~{total_lost} readings lost.")
        if ongoing:
            lines.append("")
            lines.append("⚠ CURRENTLY OFFLINE (no recent reading):")
            for g in ongoing:
                lines.append(
                    f"  • {who(g)}: last seen {fmt(g.start_local)} "
                    f"({g.minutes/60:.1f}h ago)"
                )
        past = [g for g in groups if not g.ongoing]
        if past:
            lines.append("")
            lines.append("Outage windows (resolved):")
            for g in past:
                lines.append(
                    f"  • {who(g)}: {fmt(g.start_local)} → {fmt(g.end_local)} "
                    f"({g.minutes/60:.1f}h, ~{g.readings_lost} lost per sensor)"
                )
    lines += ["", "Coverage (readings vs expected in window):"]
    for c in coverage:
        flag = " ⚠" if c.pct_of_expected < 90 else ""
        lines.append(
            f"  {c.display_name:32s} {c.pct_of_expected:5.1f}%  "
            f"(behind {c.minutes_behind:.0f} min){flag}"
        )
    text_body = "\n".join(lines)

    # ---- html ----
    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    html = [f"<h2>Weatherbot outage report — last {hours_back}h</h2>"]
    if not groups:
        html.append("<p>✅ No gaps over 15 minutes on any active reliable sensor.</p>")
    else:
        html.append(
            f"<p><b>{n_gaps} outage window(s)</b>, ~{total_lost} readings lost.</p>"
        )
        if ongoing:
            html.append("<p style='color:#b00'><b>⚠ Currently offline:</b></p><ul>")
            for g in ongoing:
                html.append(
                    f"<li>{esc(who(g))} — last seen {fmt(g.start_local)} "
                    f"({g.minutes/60:.1f}h ago)</li>"
                )
            html.append("</ul>")
        past = [g for g in groups if not g.ongoing]
        if past:
            html.append("<p><b>Outage windows (resolved):</b></p><ul>")
            for g in past:
                html.append(
                    f"<li>{esc(who(g))}: {fmt(g.start_local)} → "
                    f"{fmt(g.end_local)} ({g.minutes/60:.1f}h, ~{g.readings_lost} lost/sensor)</li>"
                )
            html.append("</ul>")
    html.append("<p><b>Coverage:</b></p><table cellpadding=4>")
    for c in coverage:
        color = "#b00" if c.pct_of_expected < 90 else "#060"
        html.append(
            f"<tr><td>{esc(c.display_name)}</td>"
            f"<td style='color:{color};text-align:right'>{c.pct_of_expected:.1f}%</td>"
            f"<td style='text-align:right'>behind {c.minutes_behind:.0f} min</td></tr>"
        )
    html.append("</table>")
    html_body = "\n".join(html)

    return subject, text_body, html_body


def send_email(subject: str, text_body: str, html_body: str) -> None:
    """Send the report via the Gmail API using an OAuth2 refresh token.

    Requires secrets GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET /
    GMAIL_REFRESH_TOKEN and env vars REPORT_FROM_EMAIL / REPORT_TO_EMAIL.
    Imports are local so the report generator (and its tests) don't need
    the Gmail libraries unless actually sending.
    """
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    client_id = secrets.access(os.environ["SECRET_GMAIL_CLIENT_ID"])
    client_secret = secrets.access(os.environ["SECRET_GMAIL_CLIENT_SECRET"])
    refresh_token = secrets.access(os.environ["SECRET_GMAIL_REFRESH_TOKEN"])
    from_addr = os.environ["REPORT_FROM_EMAIL"]
    to_addr = os.environ["REPORT_TO_EMAIL"]

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/gmail.send"],
    )
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    msg = MIMEMultipart("alternative")
    msg["To"] = to_addr
    msg["From"] = from_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


def run(hours_back: int = 24, send: bool = False) -> str:
    """Generate the report; email it when send=True, else return the text."""
    gaps, coverage = find_gaps(hours_back)
    subject, text_body, html_body = build_report(gaps, coverage, hours_back)
    if send:
        send_email(subject, text_body, html_body)
    return f"{subject}\n\n{text_body}"
