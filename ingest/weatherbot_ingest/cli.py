"""weatherbot CLI — smoke tests and operational commands."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import typer

from . import secrets
from .awn_client import AmbientWeatherClient
from .backfill import (
    backfill_station,
    catchup_station,
    ensure_stations,
    incremental_sync,
)
from .db import connect
from .narrow_backfill import run_full_backfill
from .observations import COLUMN_SPECS, COLUMN_TYPES

app = typer.Typer(help="weatherbot ingest CLI", no_args_is_help=True)


def _build_client() -> AmbientWeatherClient:
    api_secret = os.environ.get("SECRET_AWN_API_KEY")
    app_secret = os.environ.get("SECRET_AWN_APP_KEY")
    if not api_secret or not app_secret:
        raise typer.BadParameter(
            "SECRET_AWN_API_KEY / SECRET_AWN_APP_KEY env vars not set. "
            "Run `source infra/env.sh` from the repo root."
        )
    return AmbientWeatherClient(
        api_key=secrets.access(api_secret),
        application_key=secrets.access(app_secret),
    )


def _fmt_ts_ms(ts_ms: int | None) -> str:
    if not ts_ms:
        return "—"
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()


@app.command("list-devices")
def list_devices(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """List all stations on this AWN account."""
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO)
    client = _build_client()
    devices = client.list_devices()

    if not devices:
        typer.echo("No devices found on this account.")
        raise typer.Exit(0)

    typer.echo(f"{'MAC':<20}  {'NAME':<28}  LAST OBSERVATION (UTC)")
    typer.echo("-" * 80)
    for d in devices:
        typer.echo(
            f"{d.mac_address:<20}  {(d.name or '—'):<28}  "
            f"{_fmt_ts_ms(d.last_data.get('dateutc'))}"
        )


@app.command("peek")
def peek(
    mac: str = typer.Option(None, "--mac"),
    limit: int = typer.Option(3, "--limit", "-n", min=1, max=288),
) -> None:
    """Fetch the most recent N observations for a station."""
    logging.basicConfig(level=logging.INFO)
    mac = mac or os.environ.get("STATION_MAC")
    if not mac:
        raise typer.BadParameter("Provide --mac or set STATION_MAC in env.sh.")

    client = _build_client()
    rows = client.get_device_data(mac, limit=limit)
    typer.echo(f"Got {len(rows)} record(s) for {mac}:")
    for r in rows:
        typer.echo(
            f"  {_fmt_ts_ms(r.get('dateutc'))}  "
            f"temp={r.get('tempf')}°F  hum={r.get('humidity')}%  "
            f"wind={r.get('windspeedmph')}mph  rain1h={r.get('hourlyrainin')}in"
        )


@app.command("sync-stations")
def sync_stations() -> None:
    """Upsert the stations table from AWN /devices."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    client = _build_client()
    with connect() as conn:
        devices = ensure_stations(client, conn)
    for d in devices:
        typer.echo(f"  {d.mac_address}  {d.name}")


@app.command("backfill")
def backfill(
    mac: str = typer.Option(None, "--mac", help="Single station. Default: all on account."),
    since: str = typer.Option(
        None, "--since",
        help="YYYY-MM-DD lower bound. Defaults to $STATION_BACKFILL_FROM.",
    ),
) -> None:
    """Backfill historical observations from AWN into Postgres."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")

    since_str = since or os.environ.get("STATION_BACKFILL_FROM")
    if not since_str:
        raise typer.BadParameter("Provide --since or set STATION_BACKFILL_FROM.")
    start_dt = datetime.fromisoformat(since_str).replace(tzinfo=timezone.utc)

    client = _build_client()
    with connect() as conn:
        devices = ensure_stations(client, conn)
        targets = [d.mac_address for d in devices] if mac is None else [mac]

        grand_total = 0
        for target in targets:
            typer.echo(f"\n── backfilling {target} since {start_dt.date()} ──")
            grand_total += backfill_station(client, conn, target, start_dt)

    typer.echo(f"\n✓ done. {grand_total} new rows inserted across {len(targets)} station(s).")


_FIELD_STATS_SQL = """
WITH filtered AS (
  SELECT *
    FROM observations
   WHERE (%s::text IS NULL OR station_id = %s)
),
counts AS (
  SELECT
    o.station_id,
    j.key,
    count(*)              AS n_obs,
    min(o.observed_at)    AS first_seen,
    max(o.observed_at)    AS last_seen
  FROM filtered o,
       jsonb_each(o.raw) j
 WHERE j.value IS NOT NULL
   AND j.value::text NOT IN ('null', '""')
 GROUP BY o.station_id, j.key
),
latest AS (
  SELECT DISTINCT ON (station_id) station_id, raw
    FROM filtered
   ORDER BY station_id, observed_at DESC
),
totals AS (
  SELECT station_id, count(*) AS total_obs
    FROM filtered
   GROUP BY station_id
)
SELECT
  c.station_id,
  s.name,
  c.key                            AS field_name,
  c.n_obs,
  ROUND(100.0 * c.n_obs / t.total_obs, 1) AS pct,
  c.first_seen,
  c.last_seen,
  (l.raw -> c.key)::text           AS latest_value,
  t.total_obs
FROM counts c
JOIN totals t USING (station_id)
LEFT JOIN latest l USING (station_id)
LEFT JOIN stations s ON s.mac_address = c.station_id
WHERE c.n_obs >= %s
ORDER BY c.station_id, c.n_obs DESC, c.key
"""


@app.command("catchup")
def catchup(
    mac: str = typer.Option(None, "--mac", help="One station. Default: all on account."),
    max_hours: int = typer.Option(
        72, "--max-hours",
        help="Stop walking back after this many hours of data.",
    ),
) -> None:
    """Fill the gap between last stored observation and now.

    Walks back from "now" in 24h batches until a batch reports zero new
    rows (everything in that window already stored) or the cutoff is hit.
    Use after scheduler downtime or after the initial backfill.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    client = _build_client()
    with connect() as conn:
        devices = ensure_stations(client, conn)
        targets = [d.mac_address for d in devices] if mac is None else [mac]
        grand_total = 0
        for target in targets:
            typer.echo(f"\n── catchup {target} ──")
            grand_total += catchup_station(client, conn, target, max_hours=max_hours)
    typer.echo(f"\n✓ {grand_total} new row(s) across {len(targets)} station(s).")


@app.command("sync")
def sync() -> None:
    """One-shot incremental sync from AWN for every station on the account.

    The entrypoint for the scheduled Cloud Run Job. Idempotent — each row
    upserts with ON CONFLICT DO NOTHING, so re-runs (or overlapping ticks)
    just no-op for already-stored observations.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    client = _build_client()

    with connect() as conn:
        devices = ensure_stations(client, conn)
        total_new = 0
        for d in devices:
            total_new += incremental_sync(client, conn, d.mac_address)

    typer.echo(f"✓ {total_new} new row(s) across {len(devices)} station(s).")


@app.command("rehoist")
def rehoist() -> None:
    """Populate hoisted columns from raw jsonb for historical rows.

    For each typed column known to the ingest spec, walk every AWN field
    alias and set the column from raw where the column is still NULL.
    Idempotent: subsequent runs find no work.

    Use after a migration that adds new typed columns, so the new columns
    get values for the existing 100k+ rows without re-fetching from AWN.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")

    total = 0
    with connect() as conn:
        cur = conn.cursor()
        for col, aliases in COLUMN_SPECS:
            col_type = COLUMN_TYPES.get(col)
            if not col_type:
                typer.echo(f"  {col:<18}  (no type registered — skipping)")
                continue

            col_total = 0
            for alias in aliases:
                # NULLIF guards against empty strings that ::real / ::smallint
                # would otherwise blow up on. The `raw ?` test short-circuits
                # rows that lack the key entirely.
                sql = (
                    f"UPDATE observations "
                    f"SET {col} = NULLIF(raw->>'{alias}', '')::{col_type} "
                    f"WHERE {col} IS NULL "
                    f"AND raw ? '{alias}' "
                    f"AND raw->>'{alias}' IS NOT NULL "
                    f"AND raw->>'{alias}' NOT IN ('', 'null')"
                )
                cur.execute(sql)
                col_total += cur.rowcount or 0
            conn.commit()
            typer.echo(f"  {col:<18}  ({col_type:<8})  +{col_total:>8,} rows")
            total += col_total

    typer.echo(f"\n✓ {total:,} cell updates across {len(COLUMN_SPECS)} columns.")


@app.command("field-stats")
def field_stats(
    station: str = typer.Option(None, "--station", "-s", help="Filter to one MAC."),
    min_count: int = typer.Option(
        1, "--min-count",
        help="Skip fields seen in fewer than this many observations.",
    ),
) -> None:
    """List every AWN field that has ever appeared in observations.raw, per station.

    For each (station, field) pair: total observation count, % coverage,
    first/last seen, and the value from the most recent row that station
    reported (NULL if the latest obs didn't include that field).
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    typer.echo("→ Scanning observations.raw (this can take 30–60s on a large table)...")

    with connect() as conn:
        cur = conn.cursor()
        cur.execute(_FIELD_STATS_SQL, (station, station, min_count))
        rows = cur.fetchall()

    if not rows:
        typer.echo("No fields found.")
        return

    current_station: str | None = None
    for (
        station_id, name, field, n_obs, pct, first_seen, last_seen, latest_value, total_obs
    ) in rows:
        if station_id != current_station:
            typer.echo("")
            typer.echo(
                f"━━━ {station_id}  {name or '(unnamed)'}  "
                f"(total obs: {total_obs:,})  ━━━"
            )
            typer.echo(
                f"  {'FIELD':<24}  {'COUNT':>9}  {'%':>6}  "
                f"{'FIRST':<11}  {'LAST':<11}  LATEST"
            )
            typer.echo("  " + "─" * 96)
            current_station = station_id

        latest_str = latest_value if latest_value is not None else "—"
        # Truncate verbose JSON values so the table stays readable.
        if len(latest_str) > 24:
            latest_str = latest_str[:21] + "..."

        typer.echo(
            f"  {field:<24}  {n_obs:>9,}  {float(pct):>5.1f}%  "
            f"{first_seen.date()!s:<11}  {last_seen.date()!s:<11}  {latest_str}"
        )


@app.command("narrow-backfill")
def narrow_backfill() -> None:
    """Populate the narrow schema (polls / sensors / sensor_readings) from
    the legacy wide observations table.

    Idempotent. Safe to re-run after partial failures or to pick up new
    sensor_assignments that didn't exist on the first pass.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    with connect() as conn:
        results = run_full_backfill(conn)

    typer.echo("")
    typer.echo("┌─ narrow backfill complete ─")
    typer.echo(f"│  polls:                       {results['polls']:>10,}")
    typer.echo(f"│  sensors:                     {results['sensors']:>10,}")
    typer.echo(f"│  sensor_readings (total):     {results['sensor_readings_total']:>10,}")
    typer.echo(f"│  sensor time bounds updated:  {results['sensor_time_bounds_updated']:>10,}")
    typer.echo("│")
    typer.echo("│  by AWN field:")
    by_field = results.get("sensor_readings_by_field", {})
    if isinstance(by_field, dict):
        for f, n in sorted(by_field.items(), key=lambda kv: -kv[1]):
            if n > 0:
                typer.echo(f"│    {f:<18}  +{n:>10,}")
    typer.echo("└─")


@app.command("outage-report")
def outage_report(
    hours: int = typer.Option(24, "--hours", help="Lookback window in hours."),
    send: bool = typer.Option(
        False, "--send", help="Email the report (else print to stdout)."
    ),
) -> None:
    """Report sensor gaps/outages over the lookback window.

    Dry run (default) prints the report. `--send` emails it via the Gmail
    API (requires the GMAIL_* secrets + REPORT_*_EMAIL env vars; see
    docs/gmail-report-setup.md). This command is what the daily Cloud Run
    Job runs with `--send`.
    """
    from . import outage_report as report

    out = report.run(hours_back=hours, send=send)
    typer.echo(out)
    if send:
        typer.echo("\n(emailed)")


if __name__ == "__main__":
    app()
