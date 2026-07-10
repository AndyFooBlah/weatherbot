"""Walk backward through AWN history for one or more stations.

Strategy: start at the oldest observation we already have for the station
(so re-runs auto-resume), or "now" if the table is empty. Page backward in
288-record windows using `endDate = oldest_record_in_batch`. Stop once we
receive no records or cross the `start_dt` boundary. Idempotent at the row
level via ON CONFLICT DO NOTHING.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pg8000.dbapi

from .awn_client import AmbientWeatherClient, Device
from .observations import (
    get_oldest_observed_at,
    upsert_observations,
    upsert_station,
)

logger = logging.getLogger(__name__)


def catchup_station(
    client: AmbientWeatherClient,
    conn: pg8000.dbapi.Connection,
    mac: str,
    max_hours: int = 72,
) -> int:
    """Walk back from "now" until we hit an already-stored batch or the cutoff.

    Use after an extended scheduler outage (or a manual gap, like the time
    between initial backfill and scheduled-sync deployment). Stops when a
    batch returns zero new rows (everything in the window was already stored)
    or when we exceed `max_hours` of walking back.

    Returns count of new rows inserted.
    """
    from datetime import timedelta

    end_dt: datetime | None = None
    total_inserted = 0
    batch_num = 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_hours)

    while True:
        batch_num += 1
        rows = client.get_device_data(mac, end_date=end_dt, limit=288)
        if not rows:
            logger.info("[%s] empty response — done", mac)
            break

        timestamps = [
            datetime.fromtimestamp(r["dateutc"] / 1000, tz=timezone.utc)
            for r in rows if r.get("dateutc")
        ]
        if not timestamps:
            break

        oldest = min(timestamps)
        newest = max(timestamps)
        inserted = upsert_observations(conn, mac, rows)
        total_inserted += inserted

        logger.info(
            "[%s] catchup batch %d: fetched=%d new=%d window=%s → %s",
            mac, batch_num, len(rows), inserted,
            oldest.isoformat(), newest.isoformat(),
        )

        # Walk back until we cross the cutoff. Don't stop on inserted=0 alone —
        # the first batch is always the just-synced 24h, but real gaps may
        # exist further back. Track consecutive zero-new batches as a hint
        # we've crossed into a fully-stored stretch.
        if oldest < cutoff:
            logger.info("[%s] reached %dh cutoff", mac, max_hours)
            break

        end_dt = oldest

    return total_inserted


def incremental_sync(
    client: AmbientWeatherClient,
    conn: pg8000.dbapi.Connection,
    mac: str,
    limit: int = 288,
) -> int:
    """Pull the most recent observations for one station and upsert them.

    Designed to be called every few minutes from a scheduled job. No paging,
    no resume bookkeeping — a single AWN call returns the last `limit`
    records, ON CONFLICT DO NOTHING dedupes anything we already have. The
    default 288 (24h window) gives free recovery from up to a day of
    skipped scheduler ticks without any extra logic.

    Returns the number of new rows inserted.
    """
    rows = client.get_device_data(mac, limit=limit)
    if not rows:
        logger.info("[%s] AWN returned no records", mac)
        return 0
    inserted = upsert_observations(conn, mac, rows)
    logger.info("[%s] fetched=%d  new=%d", mac, len(rows), inserted)
    return inserted


def ensure_stations(
    client: AmbientWeatherClient, conn: pg8000.dbapi.Connection
) -> list[Device]:
    """Refresh the stations table from AWN. Returns devices."""
    devices = client.list_devices()
    for d in devices:
        upsert_station(conn, d.mac_address, d.info, d.last_data)
    logger.info("Upserted %d station(s).", len(devices))
    return devices


def backfill_station(
    client: AmbientWeatherClient,
    conn: pg8000.dbapi.Connection,
    mac: str,
    start_dt: datetime,
) -> int:
    """Backfill observations for one station back to start_dt. Returns rows inserted."""
    end_dt = get_oldest_observed_at(conn, mac)
    if end_dt is None:
        logger.info("[%s] no existing data — starting from now", mac)
    else:
        logger.info("[%s] resuming from oldest stored row: %s", mac, end_dt.isoformat())

    total_inserted = 0
    batch_num = 0

    while True:
        batch_num += 1
        rows = client.get_device_data(mac, end_date=end_dt, limit=288)
        if not rows:
            logger.info("[%s] batch %d: empty response — done.", mac, batch_num)
            break

        timestamps = [
            datetime.fromtimestamp(r["dateutc"] / 1000, tz=timezone.utc)
            for r in rows if r.get("dateutc")
        ]
        if not timestamps:
            logger.warning("[%s] batch %d: %d records, none with dateutc", mac, batch_num, len(rows))
            break

        oldest = min(timestamps)
        newest = max(timestamps)
        inserted = upsert_observations(conn, mac, rows)
        total_inserted += inserted

        logger.info(
            "[%s] batch %d: fetched=%d new=%d window=%s → %s",
            mac, batch_num, len(rows), inserted,
            oldest.isoformat(), newest.isoformat(),
        )

        if oldest <= start_dt:
            logger.info("[%s] reached backfill floor %s — done.", mac, start_dt.isoformat())
            break

        # Next iteration: AWN returns records strictly before endDate, so passing
        # oldest as endDate excludes that one row (which we just stored).
        end_dt = oldest

    logger.info("[%s] total new rows: %d (across %d batches)", mac, total_inserted, batch_num)
    return total_inserted
