#!/usr/bin/env python3
"""One-command Gmail OAuth setup for the daily outage report (weatherbot#14).

What it does (everything except the one human step — the consent click):
  1. Runs the OAuth 2.0 loopback flow for a DESKTOP client: starts a
     localhost listener, opens your browser to Google's consent page for
     the gmail.send scope, captures the redirect, exchanges the code for
     tokens (PKCE + offline access, so a refresh token is issued).
  2. Stores client id, client secret, and refresh token in Secret
     Manager (weatherbot-gmail-client-id / -client-secret /
     -refresh-token) in the weatherbot-prod project.
  3. Grants the report job's service account (weatherbot-sync-sa)
     accessor on exactly those three secrets.

Prereqs (see docs/gmail-report-setup.md for the console clicks):
  - A Desktop-type OAuth client in Google Auth Platform → Clients.
  - Publishing status "In production" (Audience tab). CRITICAL: in
    "Testing" status Google expires refresh tokens after 7 days and the
    daily report would silently die a week later.
  - gcloud authenticated with rights on weatherbot-prod.

Usage:
  python3 scripts/setup-gmail-oauth.py --client-json ~/Downloads/client_secret_*.json
  # or, without the JSON file:
  python3 scripts/setup-gmail-oauth.py            # prompts for id + secret

  Optional: --send-test you@example.com   send a test email at the end
            --project weatherbot-prod     override the GCP project

No third-party dependencies; secrets never touch argv of subprocesses
(passed on stdin) and are never printed.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import glob
import hashlib
import http.server
import json
import os
import secrets as pysecrets
import socket
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from email.mime.text import MIMEText

SCOPE = "https://www.googleapis.com/auth/gmail.send"
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GMAIL_SEND_ENDPOINT = (
    "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
)

SECRET_NAMES = {
    "client_id": "weatherbot-gmail-client-id",
    "client_secret": "weatherbot-gmail-client-secret",
    "refresh_token": "weatherbot-gmail-refresh-token",
}
SA_NAME = "weatherbot-sync-sa"


def load_client(args) -> tuple[str, str]:
    if args.client_json:
        paths = glob.glob(os.path.expanduser(args.client_json))
        if not paths:
            sys.exit(f"✗ no file matches {args.client_json}")
        with open(paths[0]) as f:
            data = json.load(f)
        block = data.get("installed") or data.get("web")
        if not block:
            sys.exit("✗ unrecognized client JSON (no 'installed'/'web' key) — "
                     "download it from Google Auth Platform → Clients")
        if "installed" not in data:
            print("⚠ this is a WEB client. A Desktop client is preferred "
                  "(loopback redirects are always valid). If the browser "
                  "step fails with redirect_uri_mismatch, recreate the "
                  "client as type 'Desktop app'.")
        return block["client_id"], block["client_secret"]
    cid = input("OAuth client ID: ").strip()
    csec = getpass.getpass("OAuth client secret (input hidden): ").strip()
    if not cid or not csec:
        sys.exit("✗ client id and secret are required")
    return cid, csec


def run_loopback_flow(client_id: str, client_secret: str) -> dict:
    # Free port on the loopback interface.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    redirect_uri = f"http://localhost:{port}"

    verifier = base64.urlsafe_b64encode(os.urandom(64)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    state = pysecrets.token_urlsafe(24)

    auth_url = AUTH_ENDPOINT + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",   # → refresh token
        "prompt": "consent",        # → refresh token even on re-grant
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })

    result: dict = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result["code"] = q.get("code", [None])[0]
            result["state"] = q.get("state", [None])[0]
            result["error"] = q.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<h2>weatherbot: authorization received.</h2>"
                b"You can close this tab and return to the terminal.")
            done.set()

        def log_message(self, *a):  # silence request logging
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print("\n→ Opening your browser for Google consent (gmail.send scope).")
    print("  If it doesn't open, paste this URL yourself:\n")
    print(f"  {auth_url}\n")
    webbrowser.open(auth_url)

    if not done.wait(timeout=300):
        server.shutdown()
        sys.exit("✗ timed out after 5 minutes waiting for the consent redirect")
    server.shutdown()

    if result.get("error"):
        sys.exit(f"✗ consent failed: {result['error']}")
    if result.get("state") != state:
        sys.exit("✗ OAuth state mismatch — aborting (possible interference)")
    if not result.get("code"):
        sys.exit("✗ no authorization code in redirect")

    body = urllib.parse.urlencode({
        "code": result["code"],
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(TOKEN_ENDPOINT, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req) as resp:
        tokens = json.load(resp)

    if "refresh_token" not in tokens:
        sys.exit(
            "✗ Google returned no refresh_token. Usual cause: a previous "
            "grant exists. Revoke this app at "
            "https://myaccount.google.com/permissions and re-run.")
    return tokens


def gcloud(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gcloud", *args],
        input=stdin.encode() if stdin is not None else None,
        capture_output=True,
    )


def store_secret(project: str, name: str, value: str) -> None:
    exists = gcloud(["secrets", "describe", name, f"--project={project}"])
    if exists.returncode != 0:
        r = gcloud(["secrets", "create", name, f"--project={project}",
                    "--replication-policy=automatic", "--data-file=-"],
                   stdin=value)
    else:
        r = gcloud(["secrets", "versions", "add", name,
                    f"--project={project}", "--data-file=-"], stdin=value)
    if r.returncode != 0:
        sys.exit(f"✗ storing secret {name} failed:\n{r.stderr.decode()[:400]}")
    print(f"  ✓ secret {name}")


def grant_accessor(project: str, name: str, sa_email: str) -> None:
    r = gcloud(["secrets", "add-iam-policy-binding", name,
                f"--project={project}",
                f"--member=serviceAccount:{sa_email}",
                "--role=roles/secretmanager.secretAccessor", "--quiet"])
    if r.returncode != 0:
        sys.exit(f"✗ granting accessor on {name} failed:\n"
                 f"{r.stderr.decode()[:400]}")
    print(f"  ✓ accessor granted on {name}")


def send_test(tokens: dict, to_addr: str) -> None:
    msg = MIMEText("weatherbot Gmail OAuth setup succeeded. "
                   "The daily outage report can now send email.")
    msg["to"] = to_addr
    msg["subject"] = "weatherbot: Gmail setup test"
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    req = urllib.request.Request(
        GMAIL_SEND_ENDPOINT,
        data=json.dumps({"raw": raw}).encode(),
        method="POST")
    req.add_header("Authorization", f"Bearer {tokens['access_token']}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        json.load(resp)
    print(f"  ✓ test email sent to {to_addr}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--client-json",
                    help="path (or glob) to the downloaded client_secret_*.json")
    ap.add_argument("--project", default="weatherbot-prod")
    ap.add_argument("--send-test", metavar="EMAIL",
                    help="send a test email to this address when done")
    args = ap.parse_args()

    client_id, client_secret = load_client(args)
    tokens = run_loopback_flow(client_id, client_secret)
    print("✓ refresh token obtained")

    sa_email = f"{SA_NAME}@{args.project}.iam.gserviceaccount.com"
    values = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": tokens["refresh_token"],
    }
    print(f"→ storing secrets in {args.project} and granting {SA_NAME}...")
    for key, secret_name in SECRET_NAMES.items():
        store_secret(args.project, secret_name, values[key])
        grant_accessor(args.project, secret_name, sa_email)

    if args.send_test:
        print("→ sending test email...")
        send_test(tokens, args.send_test)

    print("\n✓ Done. Next: deploy the report job —\n"
          "    bash infra/06-deploy-report-job.sh\n"
          "  (or tell Claude the OAuth setup is complete).")


if __name__ == "__main__":
    main()
