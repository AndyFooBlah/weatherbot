# Gmail setup for the daily outage report

The daily outage report (`weatherbot outage-report --send`, run by the
`weatherbot-outage-report` Cloud Run Job) emails via the Gmail API using an
OAuth2 refresh token. This is the same pattern CarBot uses; weatherbot is a
separate GCP project (`weatherbot-prod`), so it needs its own OAuth client +
refresh token.

One-time setup:

## 1. Enable the Gmail API

```bash
gcloud services enable gmail.googleapis.com --project=weatherbot-prod
```

## 2. Create an OAuth 2.0 client (Desktop app)

GCP Console → **APIs & Services → Credentials → Create Credentials → OAuth
client ID → Application type: Desktop app** (in project `weatherbot-prod`).
Note the **client ID** and **client secret**. (If prompted, configure the
OAuth consent screen as "External", add yourself as a test user — no
verification needed for personal use.)

## 3. Get a refresh token

Using the client ID/secret, run a one-off consent flow for the sending Gmail
account, requesting the `gmail.send` scope. Any standard OAuth helper works;
e.g. with the Google OAuth playground (https://developers.google.com/oauthplayground):
- Gear icon → "Use your own OAuth credentials" → paste client ID + secret.
- Authorize scope `https://www.googleapis.com/auth/gmail.send`.
- Sign in as the sending account, approve.
- Exchange the authorization code → copy the **refresh token**.

(Or reuse the CarBot refresh-token acquisition script if you kept it — just
point it at this project's client ID/secret and the `gmail.send` scope.)

## 4. Store the three values in Secret Manager

```bash
printf '%s' 'CLIENT_ID_HERE'     | gcloud secrets create weatherbot-gmail-client-id     --data-file=- --project=weatherbot-prod
printf '%s' 'CLIENT_SECRET_HERE' | gcloud secrets create weatherbot-gmail-client-secret --data-file=- --project=weatherbot-prod
printf '%s' 'REFRESH_TOKEN_HERE' | gcloud secrets create weatherbot-gmail-refresh-token --data-file=- --project=weatherbot-prod
```

(If the secrets already exist, use `gcloud secrets versions add <name> --data-file=-`.)

Grant the sync service account access (it runs the report job):

```bash
for s in weatherbot-gmail-client-id weatherbot-gmail-client-secret weatherbot-gmail-refresh-token; do
  gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:weatherbot-sync-sa@weatherbot-prod.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor" --project=weatherbot-prod
done
```

## 5. Set the email addresses in `infra/env.sh`

```bash
export REPORT_FROM_EMAIL="the-sending-account@gmail.com"
export REPORT_TO_EMAIL="andybrook@gmail.com"
```

The three `SECRET_GMAIL_*` env vars default to the secret names above; only
override them in `env.sh` if you named the secrets differently.

## 6. Deploy the job + test

```bash
source infra/env.sh
bash infra/06-deploy-report-job.sh
gcloud run jobs execute weatherbot-outage-report --region=us-central1 --wait
```

Check your inbox. The job then runs automatically every day at 8am local.

## Testing without email

The report generator works without any of the above — dry-run prints to
stdout:

```bash
cd ingest && uv run weatherbot outage-report --hours 48
```
