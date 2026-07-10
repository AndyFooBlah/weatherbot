#!/usr/bin/env python3
# scripts/invoke-gda-grpc.py — Call Gemini Data Analytics QueryData via gRPC,
# using the *same* Python SDK family as the MCP Toolbox's Go SDK
# (cloud.google.com/go/geminidataanalytics/apiv1beta v1.2.0). Lets us
# isolate whether the toolbox's PermissionDenied regression is in the
# gRPC backend (reproduces here) or specific to Cloud Run egress
# (succeeds here).
#
# The REST equivalent (scripts/invoke-gda.sh) succeeds 100% as both the
# user and the toolbox SA. If this gRPC call fails with the same
# `PermissionDenied: Error from Cloud SQL Instance ... [PERMISSION_DENIED]
# An internal backend error occurred`, the bug is in GDA's gRPC server.
# If it succeeds, the bug is environmental to Cloud Run.
#
# Usage:
#   scripts/.gda-grpc-venv/bin/python3 \
#       scripts/invoke-gda-grpc.py ['<natural-language query>']
#
# Override identity:
#   IMPERSONATE_SA=<toolbox-sa>@<your-project-id>.iam.gserviceaccount.com \
#       ... python3 scripts/invoke-gda-grpc.py
#
# Override target:
#   PROJECT_ID, REGION, SQL_INSTANCE, SQL_DB_NAME   (default from infra/env.sh)
#
# Prereqs:
#   - Run scripts/setup-gda-grpc.sh first to create the venv.
#   - gcloud authenticated. For impersonation, the running principal
#     needs roles/iam.serviceAccountTokenCreator on the target SA.

import json
import os
import sys
import time
import traceback
from pathlib import Path

# Load env from infra/env.sh so defaults mirror the toolbox.
ENV_FILE = Path(__file__).resolve().parent.parent / "infra" / "env.sh"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            kv = line[len("export "):]
            if "=" in kv:
                k, v = kv.split("=", 1)
                # Strip outer quotes if present.
                if v and v[0] in ('"', "'") and v[-1] == v[0]:
                    v = v[1:-1]
                os.environ.setdefault(k, v)

PROJECT_ID = os.environ.get("PROJECT_ID") or sys.exit(
    "PROJECT_ID not set — source infra/env.sh or export PROJECT_ID first"
)
REGION      = os.environ.get("REGION",      "us-central1")
SQL_INSTANCE = os.environ.get("SQL_INSTANCE","weatherbot-db")
SQL_DB_NAME = os.environ.get("SQL_DB_NAME","weatherbot")
IMPERSONATE = os.environ.get("IMPERSONATE_SA")

QUERY = sys.argv[1] if len(sys.argv) > 1 else \
    "what was the lowest pool temperature in the last week?"


def get_credentials():
    """Return google-auth credentials. Impersonates target SA when set."""
    import google.auth
    from google.auth import impersonated_credentials

    source, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    if IMPERSONATE:
        return impersonated_credentials.Credentials(
            source_credentials=source,
            target_principal=IMPERSONATE,
            target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
    return source


def main():
    from google.cloud import geminidataanalytics_v1beta as gda

    creds = get_credentials()

    # Identity readback for the log.
    if IMPERSONATE:
        identity = f"{IMPERSONATE} (impersonated)"
    else:
        identity = getattr(creds, "service_account_email", None) \
            or getattr(creds, "_target_principal", None) \
            or "(application default)"

    parent = f"projects/{PROJECT_ID}/locations/{REGION}"

    db_ref = gda.CloudSqlDatabaseReference(
        engine=gda.CloudSqlDatabaseReference.Engine.POSTGRESQL,
        project_id=PROJECT_ID,
        region=REGION,
        instance_id=SQL_INSTANCE,
        database_id=SQL_DB_NAME,
    )
    sql_ref = gda.CloudSqlReference(database_reference=db_ref)
    refs = gda.DatasourceReferences(cloud_sql_reference=sql_ref)
    ctx = gda.QueryDataContext(datasource_references=refs)
    gen_opts = gda.GenerationOptions(
        generate_query_result=True,
        generate_natural_language_answer=True,
        generate_explanation=True,
    )

    request = gda.QueryDataRequest(
        parent=parent,
        prompt=QUERY,
        context=ctx,
        generation_options=gen_opts,
    )

    print(f"→ gRPC QueryData  (geminidataanalytics.googleapis.com:443)")
    print(f"  query:    {QUERY}")
    print(f"  project:  {PROJECT_ID}")
    print(f"  region:   {REGION}")
    print(f"  instance: {SQL_INSTANCE}")
    print(f"  database: {SQL_DB_NAME}")
    print(f"  identity: {identity}")
    print()
    print("── Request (proto-json) ──")
    # The proxy-class .to_dict produces the wire JSON shape.
    print(json.dumps(type(request).to_dict(request), indent=2))
    print()

    client = gda.DataChatServiceClient(credentials=creds)

    t0 = time.time()
    try:
        resp = client.query_data(request=request, timeout=60)
        dt = time.time() - t0
        print(f"← OK  ({dt*1000:.0f}ms)")
        print()
        print("── QueryDataResponse (proto-json) ──")
        print(json.dumps(type(resp).to_dict(resp), indent=2, default=str))
    except Exception as exc:  # noqa: BLE001
        dt = time.time() - t0
        print(f"← ERROR  ({dt*1000:.0f}ms)  type={type(exc).__name__}")
        print()
        print("── Exception details ──")
        print(f"  str:     {exc}")
        for attr in ("code", "details", "debug_error_string", "grpc_status_code",
                     "response", "errors", "reason", "domain"):
            v = getattr(exc, attr, None)
            if v is None:
                continue
            try:
                v_str = v() if callable(v) else v
            except Exception:
                v_str = repr(v)
            print(f"  {attr}: {v_str}")
        print()
        print("── Traceback ──")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
