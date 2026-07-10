#!/usr/bin/env python3
# entrypoint.py — Deep auth/token introspection for ask_data debugging.
#
# Designed to be diff-able: produces a structured JSON-ish report that the
# laptop and the Cloud Run Job can each generate. Same script runs in both
# places. Then we eyeball the diff.
#
# Modes (env var controlled):
#   TOKEN_MODE=metadata          (default, Cloud Run only) — use ADC from
#                                  the metadata server.
#   TOKEN_MODE=impersonate       — self-impersonate the running SA (or
#                                  SELF_SA) with the laptop-equivalent
#                                  scope set, via iamcredentials.
#   TOKEN_MODE=env               — use the literal bearer token in
#                                  EXTERNAL_TOKEN (no minting). Lets us
#                                  inject a laptop-minted token into the
#                                  Cloud Run Job to isolate token vs origin.
#   TOKEN_MODE=user              — (laptop only) use the gcloud user ADC.
#
# Other knobs:
#   PROJECT_ID, REGION, SQL_INSTANCE, SQL_DB_NAME, QUERY  — defaults
#     pulled from infra/env.sh.
#   SKIP_GRPC=1  — only run REST.
#   SKIP_REST=1  — only run gRPC.

import base64
import json
import os
import socket
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


# ─────────────────────────── env / defaults ─────────────────────────────────

ENV_FILE = Path(__file__).resolve().parent.parent / "infra" / "env.sh"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            kv = line[len("export "):]
            if "=" in kv:
                k, v = kv.split("=", 1)
                if v and v[0] in ('"', "'") and v[-1] == v[0]:
                    v = v[1:-1]
                os.environ.setdefault(k, v)

PROJECT_ID = os.environ.get("PROJECT_ID") or sys.exit(
    "PROJECT_ID not set — source infra/env.sh or export PROJECT_ID first"
)
REGION      = os.environ.get("REGION",      "us-central1")
SQL_INSTANCE = os.environ.get("SQL_INSTANCE","weatherbot-db")
SQL_DB_NAME  = os.environ.get("SQL_DB_NAME","weatherbot")
QUERY       = os.environ.get("QUERY",
    "what was the lowest pool temperature in the last week?")

TOKEN_MODE  = os.environ.get("TOKEN_MODE", "metadata")
SELF_SA     = os.environ.get("SELF_SA",
    f"weatherbot-toolbox-sa@{PROJECT_ID}.iam.gserviceaccount.com")
SKIP_GRPC   = os.environ.get("SKIP_GRPC") == "1"
SKIP_REST   = os.environ.get("SKIP_REST") == "1"

LAPTOP_SCOPE_SET = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/sqlservice.login",
    "https://www.googleapis.com/auth/compute",
    "https://www.googleapis.com/auth/appengine.admin",
]


# ─────────────────────────── helpers ────────────────────────────────────────

def banner(s):
    bar = "─" * len(s)
    print(f"\n{bar}\n{s}\n{bar}", flush=True)


def kv(k, v):
    print(f"  {k}: {v}", flush=True)


def safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa: BLE001
        return f"<error: {type(e).__name__}: {e}>"


def is_running_on_gcp():
    try:
        socket.create_connection(("metadata.google.internal", 80), timeout=1).close()
        return True
    except Exception:
        return False


def metadata_get(path):
    req = urllib.request.Request(
        f"http://metadata.google.internal/computeMetadata/v1/{path}",
        headers={"Metadata-Flavor": "Google"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.read().decode("utf-8", errors="replace").strip()


def jwt_payload(tok):
    parts = tok.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None


def tokeninfo(tok):
    try:
        url = "https://oauth2.googleapis.com/tokeninfo?" + urllib.parse.urlencode(
            {"access_token": tok}
        )
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        data.pop("access_token", None)
        return data
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode("utf-8", errors="replace")[:500]}
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)}


# ─────────────────────────── token acquisition ──────────────────────────────

def acquire_token():
    """Return (creds, identity_label) per TOKEN_MODE."""
    import google.auth
    from google.auth import impersonated_credentials

    if TOKEN_MODE == "env":
        # No google-auth creds — we have a literal token. Wrap it in a
        # minimal Credentials shim that just hands it back.
        from google.auth.credentials import Credentials

        raw = os.environ.get("EXTERNAL_TOKEN", "").strip()
        if not raw:
            raise SystemExit("TOKEN_MODE=env requires EXTERNAL_TOKEN env var")

        class StaticCreds(Credentials):
            def __init__(self, token):
                super().__init__()
                self.token = token

            def refresh(self, request):  # noqa: ARG002
                pass

            @property
            def expired(self):
                return False

            @property
            def valid(self):
                return True

        return StaticCreds(raw), f"EXTERNAL_TOKEN (len={len(raw)})"

    source, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )

    if TOKEN_MODE == "impersonate":
        creds = impersonated_credentials.Credentials(
            source_credentials=source,
            target_principal=SELF_SA,
            target_scopes=LAPTOP_SCOPE_SET,
        )
        return creds, f"impersonate→{SELF_SA}"

    if TOKEN_MODE == "user":
        # Same as default ADC; just label it differently.
        return source, "user ADC"

    # default: metadata
    return source, "metadata-server ADC"


# ─────────────────────────── reports ────────────────────────────────────────

def report_environment():
    banner("Environment")
    kv("script_pid", os.getpid())
    kv("hostname", safe(socket.gethostname))
    kv("python", sys.version.replace("\n", " "))
    kv("running_on_gcp", is_running_on_gcp())
    kv("TOKEN_MODE", TOKEN_MODE)
    kv("PROJECT_ID", PROJECT_ID)
    kv("REGION", REGION)
    kv("SQL_INSTANCE", SQL_INSTANCE)
    kv("SQL_DB_NAME", SQL_DB_NAME)
    # Outbound IP (best-effort)
    try:
        with urllib.request.urlopen("https://ifconfig.me/ip", timeout=5) as r:
            kv("outbound_ip", r.read().decode().strip())
    except Exception as e:  # noqa: BLE001
        kv("outbound_ip", f"<{e}>")


def report_metadata_server():
    if not is_running_on_gcp():
        return
    banner("Metadata server raw dump")
    for path in (
        "instance/service-accounts/default/email",
        "instance/service-accounts/default/scopes",
        "instance/service-accounts/default/aliases",
        "instance/zone",
        "project/project-id",
        "project/numeric-project-id",
        "instance/region",
    ):
        kv(path, safe(metadata_get, path))
    # OIDC identity JWT for the SA — fully decoded.
    aud = "https://geminidataanalytics.googleapis.com"
    raw = safe(metadata_get,
               f"instance/service-accounts/default/identity?audience={urllib.parse.quote(aud)}")
    if isinstance(raw, str) and raw.count(".") == 2:
        payload = jwt_payload(raw)
        kv("OIDC identity JWT payload", json.dumps(payload, indent=2))
    else:
        kv("OIDC identity (raw)", str(raw)[:300])


def report_token(creds, identity_label):
    banner("Token introspection")
    kv("identity_label", identity_label)
    kv("creds_class", f"{type(creds).__module__}.{type(creds).__name__}")
    for attr in ("service_account_email", "_target_principal",
                 "scopes", "_scopes", "_target_scopes",
                 "quota_project_id", "_quota_project_id",
                 "_subject", "signer", "expiry"):
        v = getattr(creds, attr, "<not set>")
        kv(attr, repr(v) if v != "<not set>" else v)

    # Refresh + dump
    from google.auth.transport.requests import Request as AuthRequest
    try:
        creds.refresh(AuthRequest())
    except Exception as e:  # noqa: BLE001
        kv("refresh_error", f"{type(e).__name__}: {e}")
    tok = getattr(creds, "token", None) or ""

    kv("token_present", bool(tok))
    kv("token_length", len(tok))
    kv("token_prefix",
       f"{tok[:24]!r}...{tok[-12:]!r}" if tok else "<empty>")
    kv("token_expiry", getattr(creds, "expiry", None))
    kv("token_is_jwt", tok.count(".") == 2)

    if tok.count(".") == 2:
        kv("token_jwt_payload",
           json.dumps(jwt_payload(tok), indent=2))

    ti = tokeninfo(tok) if tok else {}
    kv("tokeninfo_response", json.dumps(ti, indent=2))


def report_egress():
    banner("Egress sanity")
    for host, port in (("geminidataanalytics.googleapis.com", 443),
                       ("oauth2.googleapis.com", 443),
                       ("iamcredentials.googleapis.com", 443)):
        try:
            t0 = time.time()
            sock = socket.create_connection((host, port), timeout=5)
            dt = (time.time() - t0) * 1000
            peer = sock.getpeername()
            sock.close()
            kv(f"{host}:{port}", f"OK in {dt:.0f}ms peer={peer}")
        except Exception as e:  # noqa: BLE001
            kv(f"{host}:{port}", f"FAIL {e}")


def report_rest_call(tok):
    banner("REST :queryData probe")
    url = (f"https://geminidataanalytics.googleapis.com/v1beta/"
           f"projects/{PROJECT_ID}/locations/{REGION}:queryData")
    body = json.dumps({
        "parent": f"projects/{PROJECT_ID}/locations/{REGION}",
        "prompt": QUERY,
        "context": {
            "datasourceReferences": {
                "cloudSqlReference": {
                    "databaseReference": {
                        "engine": "POSTGRESQL",
                        "projectId": PROJECT_ID,
                        "region": REGION,
                        "instanceId": SQL_INSTANCE,
                        "databaseId": SQL_DB_NAME,
                    },
                },
            },
        },
        "generationOptions": {
            "generateQueryResult": True,
            "generateNaturalLanguageAnswer": True,
            "generateExplanation": True,
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": f"Bearer {tok}",
                 "Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            dt = (time.time() - t0) * 1000
            raw = resp.read().decode("utf-8")
        kv("status", f"HTTP {resp.status} in {dt:.0f}ms")
        # Pluck headers GDA may set for trace correlation.
        for h in ("x-debug-tracking-id", "x-request-id",
                  "x-google-request-id", "server-timing"):
            v = resp.headers.get(h)
            if v:
                kv(f"header[{h}]", v)
        parsed = safe(json.loads, raw)
        if isinstance(parsed, dict):
            for k in ("naturalLanguageAnswer", "generatedQuery"):
                if k in parsed:
                    val = parsed[k]
                    if isinstance(val, str) and len(val) > 200:
                        val = val[:200] + "..."
                    kv(k, val)
    except urllib.error.HTTPError as e:
        dt = (time.time() - t0) * 1000
        body_text = e.read().decode("utf-8", errors="replace")
        kv("status", f"HTTP {e.code} in {dt:.0f}ms")
        for h in ("x-debug-tracking-id", "x-request-id",
                  "x-google-request-id", "server-timing"):
            v = e.headers.get(h)
            if v:
                kv(f"header[{h}]", v)
        kv("body", body_text[:2000])
    except Exception as e:  # noqa: BLE001
        dt = (time.time() - t0) * 1000
        kv("status", f"EXCEPTION in {dt:.0f}ms: {type(e).__name__}: {e}")


def report_grpc_call(creds):
    banner("gRPC QueryData probe")
    from google.cloud import geminidataanalytics_v1beta as gda

    db_ref = gda.CloudSqlDatabaseReference(
        engine=gda.CloudSqlDatabaseReference.Engine.POSTGRESQL,
        project_id=PROJECT_ID, region=REGION,
        instance_id=SQL_INSTANCE, database_id=SQL_DB_NAME,
    )
    request = gda.QueryDataRequest(
        parent=f"projects/{PROJECT_ID}/locations/{REGION}",
        prompt=QUERY,
        context=gda.QueryDataContext(datasource_references=gda.DatasourceReferences(
            cloud_sql_reference=gda.CloudSqlReference(database_reference=db_ref),
        )),
        generation_options=gda.GenerationOptions(
            generate_query_result=True,
            generate_natural_language_answer=True,
            generate_explanation=True,
        ),
    )

    client = gda.DataChatServiceClient(credentials=creds)
    t0 = time.time()
    try:
        resp = client.query_data(request=request, timeout=60)
        dt = (time.time() - t0) * 1000
        kv("status", f"OK in {dt:.0f}ms")
        kv("natural_language_answer", resp.natural_language_answer)
        kv("generated_query", resp.generated_query[:200] + "...")
    except Exception as exc:  # noqa: BLE001
        dt = (time.time() - t0) * 1000
        kv("status", f"ERROR in {dt:.0f}ms type={type(exc).__name__}")
        kv("str", str(exc))
        for attr in ("code", "details", "debug_error_string",
                     "grpc_status_code", "trailing_metadata"):
            v = getattr(exc, attr, None)
            if v is None:
                continue
            try:
                v_str = v() if callable(v) else v
            except Exception:
                v_str = repr(v)
            kv(attr, v_str)


# ─────────────────────────── main ───────────────────────────────────────────

def main():
    report_environment()
    report_metadata_server()
    report_egress()

    creds, label = acquire_token()
    report_token(creds, label)

    tok = getattr(creds, "token", None) or ""
    if not SKIP_REST:
        report_rest_call(tok)
    if not SKIP_GRPC:
        try:
            report_grpc_call(creds)
        except Exception:
            traceback.print_exc()

    # Header tap + header-injection probe. Goal: see if any
    # Cloud-Run-injected header (or any header the laptop call carries
    # implicitly) differs in a way that flips the response.
    banner("Header tap — outgoing REST request to :queryData")
    import http.client
    # Briefly bump http.client's debuglevel so urllib prints the wire
    # request line + outgoing headers to stdout.
    prev_dl = http.client.HTTPConnection.debuglevel
    http.client.HTTPConnection.debuglevel = 1
    try:
        # No-op GET to capture what an HTTPS conn from this process sends.
        try:
            with urllib.request.urlopen(
                urllib.request.Request(
                    f"https://geminidataanalytics.googleapis.com/v1beta/"
                    f"projects/{PROJECT_ID}/locations/{REGION}:queryData",
                    data=b"{}", method="POST",
                    headers={"Authorization": f"Bearer {tok}",
                             "Content-Type": "application/json"},
                ),
                timeout=10,
            ) as r:
                pass
        except Exception:
            pass
    finally:
        http.client.HTTPConnection.debuglevel = prev_dl

    banner("Header-injection probe — REST :queryData with assorted extra headers")
    base_body = json.dumps({
        "parent": f"projects/{PROJECT_ID}/locations/{REGION}",
        "prompt": QUERY,
        "context": {
            "datasourceReferences": {
                "cloudSqlReference": {
                    "databaseReference": {
                        "engine": "POSTGRESQL",
                        "projectId": PROJECT_ID,
                        "region": REGION,
                        "instanceId": SQL_INSTANCE,
                        "databaseId": SQL_DB_NAME,
                    },
                },
            },
        },
        "generationOptions": {
            "generateQueryResult": True,
            "generateNaturalLanguageAnswer": True,
            "generateExplanation": True,
        },
    }).encode("utf-8")

    header_variants = [
        ("baseline", {}),
        (f"with X-Goog-User-Project={PROJECT_ID}",
         {"X-Goog-User-Project": PROJECT_ID}),
        (f"with X-Goog-Quota-Project={PROJECT_ID}",
         {"X-Goog-Quota-Project": PROJECT_ID}),
        ("with gcloud User-Agent + X-Goog-Api-Client=gccl",
         {"User-Agent": "google-cloud-sdk/527.0.0 gcloud/527.0.0",
          "X-Goog-Api-Client": "gccl/527.0.0 gl-python/3.12.13 grpc/1.62.1"}),
        ("with all of the above combined",
         {"X-Goog-User-Project": PROJECT_ID,
          "X-Goog-Quota-Project": PROJECT_ID,
          "User-Agent": "google-cloud-sdk/527.0.0 gcloud/527.0.0",
          "X-Goog-Api-Client": "gccl/527.0.0 gl-python/3.12.13 grpc/1.62.1"}),
    ]

    for label, extra in header_variants:
        hdrs = {"Authorization": f"Bearer {tok}",
                "Content-Type": "application/json"}
        hdrs.update(extra)
        req = urllib.request.Request(
            f"https://geminidataanalytics.googleapis.com/v1beta/"
            f"projects/{PROJECT_ID}/locations/{REGION}:queryData",
            data=base_body, method="POST", headers=hdrs,
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                dt = (time.time() - t0) * 1000
                body = resp.read().decode("utf-8")
            parsed = safe(json.loads, body)
            nl = parsed.get("naturalLanguageAnswer", "") if isinstance(parsed, dict) else ""
            kv(label,
               f"HTTP {resp.status} in {dt:.0f}ms  →  {nl[:80]!r}")
        except urllib.error.HTTPError as e:
            dt = (time.time() - t0) * 1000
            err_body = e.read().decode("utf-8", errors="replace")
            err_parsed = safe(json.loads, err_body)
            err_msg = (err_parsed.get("error", {}).get("message", err_body[:80])
                       if isinstance(err_parsed, dict) else err_body[:80])
            kv(label, f"HTTP {e.code} in {dt:.0f}ms  →  {err_msg[:120]!r}")
        except Exception as e:  # noqa: BLE001
            kv(label, f"EXC: {type(e).__name__}: {e}")

    # Direct Cloud SQL Data API executeSql probe — this is the actual
    # downstream path GDA's QueryData uses to talk to Cloud SQL.
    banner("Cloud SQL Data API executeSql probe (the path GDA uses)")
    sql_body = json.dumps({
        "sqlStatement": "SELECT count(*) AS row_count FROM observations",
        "database": SQL_DB_NAME,
        "user": f"weatherbot-toolbox-sa@{PROJECT_ID}.iam",
        "autoIamAuthn": True,
    }).encode("utf-8")
    sql_url = (
        f"https://sqladmin.googleapis.com/sql/v1beta4/projects/"
        f"{PROJECT_ID}/instances/{SQL_INSTANCE}/executeSql"
    )
    sql_req = urllib.request.Request(
        sql_url, data=sql_body, method="POST",
        headers={"Authorization": f"Bearer {tok}",
                 "Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(sql_req, timeout=30) as resp:
            dt = (time.time() - t0) * 1000
            raw = resp.read().decode("utf-8")
        kv("status", f"HTTP {resp.status} in {dt:.0f}ms")
        for h in ("x-debug-tracking-id", "x-request-id",
                  "x-google-request-id"):
            v = resp.headers.get(h)
            if v:
                kv(f"header[{h}]", v)
        parsed = safe(json.loads, raw)
        if isinstance(parsed, dict):
            results = parsed.get("results", [])
            if results and results[0].get("rows"):
                kv("first_row_first_value",
                   results[0]["rows"][0].get("values", [{}])[0].get("value"))
            msgs = parsed.get("messages", [])
            for m in msgs[:3]:
                kv("message", m.get("message"))
    except urllib.error.HTTPError as e:
        dt = (time.time() - t0) * 1000
        body_text = e.read().decode("utf-8", errors="replace")
        kv("status", f"HTTP {e.code} in {dt:.0f}ms")
        for h in ("x-debug-tracking-id", "x-request-id",
                  "x-google-request-id"):
            v = e.headers.get(h)
            if v:
                kv(f"header[{h}]", v)
        kv("body", body_text[:2000])
    except Exception as e:  # noqa: BLE001
        dt = (time.time() - t0) * 1000
        kv("status", f"EXCEPTION in {dt:.0f}ms: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
