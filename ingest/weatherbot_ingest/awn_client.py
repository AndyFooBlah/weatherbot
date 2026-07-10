"""Ambient Weather Network REST API client.

Canonical spec: https://github.com/ambient-weather/api-docs/blob/master/apiary.apib

Rate limits (per AWN):
  - 1 request/second per apiKey
  - 3 requests/second per applicationKey

We throttle to just over 1.0s between calls, which keeps us safe on both.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://rt.ambientweather.net/v1"
DEFAULT_TIMEOUT_S = 30
MIN_SECONDS_BETWEEN_CALLS = 1.05
MAX_RECORDS_PER_CALL = 288  # AWN hard cap on /devices/{mac} `limit`


@dataclass(frozen=True)
class Device:
    mac_address: str
    info: dict[str, Any]
    last_data: dict[str, Any]

    @property
    def name(self) -> str | None:
        return self.info.get("name")

    @property
    def location(self) -> str | None:
        coords = self.info.get("coords") or {}
        return coords.get("location") or self.info.get("location")


class AmbientWeatherClient:
    def __init__(
        self,
        api_key: str,
        application_key: str,
        *,
        base_url: str = BASE_URL,
    ):
        if not api_key or not application_key:
            raise ValueError("Both api_key and application_key are required.")
        self._api_key = api_key
        self._app_key = application_key
        self._base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._last_call_ts = 0.0

    def _throttle(self) -> None:
        delta = time.monotonic() - self._last_call_ts
        if delta < MIN_SECONDS_BETWEEN_CALLS:
            time.sleep(MIN_SECONDS_BETWEEN_CALLS - delta)

    def _get(self, path: str, **params: Any) -> Any:
        self._throttle()
        url = f"{self._base_url}{path}"
        full_params = {
            **params,
            "apiKey": self._api_key,
            "applicationKey": self._app_key,
        }
        logger.debug("GET %s params=%s", path, params)  # never logs secrets
        resp = self._session.get(url, params=full_params, timeout=DEFAULT_TIMEOUT_S)
        self._last_call_ts = time.monotonic()
        if not resp.ok:
            # requests.HTTPError's message embeds the full request URL, and the
            # apiKey/applicationKey ride in the query string — raising the stock
            # error would put both keys in whatever log captures the traceback.
            raise requests.HTTPError(
                f"{resp.status_code} {resp.reason} for {self._base_url}{path} "
                "(query params redacted)",
                response=resp,
            )
        return resp.json()

    def list_devices(self) -> list[Device]:
        raw = self._get("/devices")
        return [
            Device(
                mac_address=d["macAddress"],
                info=d.get("info", {}) or {},
                last_data=d.get("lastData", {}) or {},
            )
            for d in raw
        ]

    def get_device_data(
        self,
        mac_address: str,
        *,
        end_date: datetime | None = None,
        limit: int = MAX_RECORDS_PER_CALL,
    ) -> list[dict[str, Any]]:
        """Fetch up to `limit` observations for `mac_address`, newest first.

        AWN returns records ending strictly before `end_date` (or "now" if omitted).
        `end_date` is sent as milliseconds since epoch (UTC) per the spec.
        """
        if limit > MAX_RECORDS_PER_CALL:
            raise ValueError(f"limit must be ≤ {MAX_RECORDS_PER_CALL}")

        params: dict[str, Any] = {"limit": limit}
        if end_date is not None:
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)
            params["endDate"] = int(end_date.timestamp() * 1000)

        return self._get(f"/devices/{mac_address}", **params)
