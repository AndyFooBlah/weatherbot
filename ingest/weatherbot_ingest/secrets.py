"""Thin wrapper around Google Secret Manager.

Reads PROJECT_ID from the environment so the same code works in local dev
(after `source infra/env.sh`) and in Cloud Run (where PROJECT_ID is injected).
"""

from __future__ import annotations

import functools
import os

from google.cloud import secretmanager


@functools.lru_cache(maxsize=1)
def _client() -> secretmanager.SecretManagerServiceClient:
    return secretmanager.SecretManagerServiceClient()


def project_id() -> str:
    pid = os.environ.get("PROJECT_ID")
    if not pid:
        raise RuntimeError(
            "PROJECT_ID env var not set. Run `source infra/env.sh` from the repo root."
        )
    return pid


def access(secret_name: str, version: str = "latest") -> str:
    """Return the secret payload as a UTF-8 string."""
    name = f"projects/{project_id()}/secrets/{secret_name}/versions/{version}"
    response = _client().access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8")
