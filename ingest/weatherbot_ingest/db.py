"""Cloud SQL Postgres connection helper.

Uses the Cloud SQL Python Connector so the same code works locally (via ADC)
and in Cloud Run (via the service account). No cloud-sql-proxy process needed.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator

import pg8000.dbapi
from google.cloud.sql.connector import Connector, IPTypes

from . import secrets


def _required(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} env var not set. Run `source infra/env.sh`.")
    return v


@contextlib.contextmanager
def connect() -> Iterator[pg8000.dbapi.Connection]:
    """Yield a pg8000 connection to the Cloud SQL Postgres instance.

    Caller is responsible for `conn.commit()` after writes.
    """
    connection_name = _required("CONNECTION_NAME")
    user = _required("SQL_DB_USER")
    db = _required("SQL_DB_NAME")
    password_secret = _required("SECRET_DB_PASSWORD")
    password = secrets.access(password_secret)

    connector = Connector(refresh_strategy="lazy")
    try:
        conn = connector.connect(
            connection_name,
            "pg8000",
            user=user,
            password=password,
            db=db,
            ip_type=IPTypes.PUBLIC,
        )
        try:
            yield conn
        finally:
            conn.close()
    finally:
        connector.close()
