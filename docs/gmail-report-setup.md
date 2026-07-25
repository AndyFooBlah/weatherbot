# Gmail setup for the daily outage report

One-time setup so the `weatherbot-outage-report` Cloud Run Job can send
email via the Gmail API. Rewritten 2026-07-24 against the current
console ("Google Auth Platform" — the old "OAuth consent screen" page
this doc previously described no longer exists). Almost everything is
automated by `scripts/setup-gmail-oauth.py`; the console part is two
short steps.

## Step 1 — console: consent screen + Desktop client (~3 minutes)

Open **APIs & Services → [Google Auth Platform](https://console.cloud.google.com/auth/overview?project=weatherbot-prod)**
in the `weatherbot-prod` project. (Gmail API is already enabled.)

1. **Branding** (first time only): set an app name (e.g. `weatherbot`)
   and your email; everything else can stay empty.
2. **Audience**: choose **External** if asked, then — this is the step
   that matters — set the publishing status to **"In production"**
   (there's a "Publish app" button on this tab).

   > ⚠ **Do not leave the app in "Testing".** Google expires refresh
   > tokens for Testing-status apps after **7 days**, which would
   > silently kill the daily report a week after setup. "In production"
   > without verification is fine for personal use — the only effect is
   > an "unverified app" warning screen during your one-time consent
   > click (click "Advanced → continue").
3. **Clients → Create client**: application type **Desktop app**, any
   name. Download the JSON (button on the client row / creation
   dialog) — it lands as `~/Downloads/client_secret_….json`.

## Step 2 — run the script (~1 minute)

```bash
cd ~/dev/weatherbot
python3 scripts/setup-gmail-oauth.py \
    --client-json '~/Downloads/client_secret_*.json' \
    --send-test andybrook@gmail.com     # optional sanity email
```

The script opens your browser for the consent click (the one human
step), then automatically: exchanges the code for a **refresh token**
(loopback flow with PKCE + offline access), stores all three values in
Secret Manager (`weatherbot-gmail-client-id` / `-client-secret` /
`-refresh-token`), and grants `weatherbot-sync-sa` accessor on exactly
those secrets. No third-party Python deps; secrets are passed on stdin
and never printed.

If it reports "no refresh_token", revoke the app's prior grant at
<https://myaccount.google.com/permissions> and re-run (Google only
reissues refresh tokens on a fresh consent).

## Step 3 — deploy the job

```bash
bash infra/06-deploy-report-job.sh
```

Requires `REPORT_FROM_EMAIL` / `REPORT_TO_EMAIL` in `infra/env.sh`
(already set). This creates the `weatherbot-outage-report` Cloud Run
Job and the 8:00 AM America/Los_Angeles scheduler. Manual run to
verify end-to-end:

```bash
gcloud run jobs execute weatherbot-outage-report \
    --region=us-central1 --project=weatherbot-prod --wait
```

You should receive the report email; subject escalates from "✓ all
sensors healthy" to gap counts to "N sensors OFFLINE".

## Notes

- The OAuth client credentials for a desktop app are not treated as
  confidential by Google's model, but we store them in Secret Manager
  anyway and nothing ever ships to a browser bundle.
- Token refresh happens inside the job (`outage_report.py`) using the
  google-auth library; the stored refresh token is long-lived because
  the app is in production status (see Step 1).
- To rotate: revoke at myaccount.google.com/permissions, re-run
  Step 2. To change recipients: edit REPORT_TO_EMAIL in env.sh and
  re-run Step 3.
