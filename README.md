# gmail-auth

Incremental Gmail → BigQuery pipeline with a dbt transformation layer that
classifies job-search emails (applications, interviews, offers, rejections,
recruiter outreach, networking).

The pipeline runs on **Cloud Functions v2** and is triggered on demand from a
**GitHub Actions** workflow (`workflow_dispatch`). The same workflow then runs
`dbt build` against the freshly-loaded rows. All GCP resources are provisioned
by **Terraform**.

Gmail is accessed with a **user OAuth refresh token** (not service-account
domain-wide delegation), because the target mailbox is a personal `@gmail.com`
account. Consumer Gmail does not support service-account impersonation. The
refresh token is minted once locally and stored in **Secret Manager**; the
Cloud Function reads it at runtime and the google-auth library refreshes the
short-lived access token automatically.

## Architecture

```
                ┌────────────────────────┐
                │  GitHub Actions        │   workflow_dispatch
                │  ingest-and-build.yml  │
                └───────────┬────────────┘
                            │ POST + OIDC      (blocks until 200)
                            ▼
                ┌───────────────────┐
                │ Cloud Function v2 │   run_pipeline_http()
                │ gmail-bigquery-…  │
                └─────────┬─────────┘
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
      ┌──────────────┐         ┌──────────────┐
      │   Gmail API  │         │   BigQuery   │
      │ (user OAuth  │         │ gmail_data.  │
      │  refresh tok)│         │ gmail_messages│
      └──────────────┘         └──────┬───────┘
                                      │
                                      ▼
                            ┌──────────────────┐
                            │   dbt (BigQuery) │ ← same workflow,
                            │ staging → int →  │   runs after the
                            │ marts            │   function returns
                            └──────────────────┘
```

- **Watermark** — every run picks up from `MAX(sync_end) WHERE status='ok'`
  in the audit table `gmail_data.ingestion_runs` (one row per completed
  ingestion window). Cold start / migration falls back to
  `MAX(received_at)` from `gmail_data.gmail_messages`, then finally to
  `FIRST_RUN_START_DATE` (default `2026-03-01`). All timestamps are floored
  to whole seconds because Gmail's `after:` / `before:` operators only
  accept epoch seconds; sub-second overlap is deduped by `message_id` in
  `load.py`.
- **Windowed, oldest-first ingestion** — each run splits `[watermark, now)`
  into day-aligned windows (`WINDOW_DAYS`, default `1`) and processes them
  ascending. Only when a window's BigQuery inserts succeed does its
  `ingestion_runs` row land with `status='ok'`, and only `status='ok'` rows
  count toward the watermark. A crash (OOM, Gmail 5xx, etc.) therefore
  bounds lost work to the *current* window; older windows are never
  re-processed and never silently skipped.
- **Dedup** — before each insert batch, `load.py` checks which `message_id`s
  already exist in `gmail_messages` and skips them. Re-runs, overlapping
  windows, and a retried current window are all idempotent.

## Repo layout

```
gmail-auth/
├── ingestion/                       # Cloud Function source (Python)
│   ├── main.py                      # Cloud Function entry-point shim
│   ├── src/gmail_ingestion/
│   │   ├── auth.py                  # ADC for BigQuery + user-OAuth for Gmail
│   │   ├── fetch.py                 # Gmail API list/get/transform (skips attachment parts)
│   │   ├── load.py                  # BigQuery dedup + streaming insert
│   │   ├── runs.py                  # Append-only writes to gmail_data.ingestion_runs
│   │   ├── watermark.py             # Read MAX(sync_end) WHERE status='ok' from ingestion_runs
│   │   └── settings.py              # Pydantic settings from env vars
│   ├── scripts/run_ingestion.py     # Pipeline orchestration + local CLI
│   ├── scripts/get_user_token.py    # One-off OAuth flow to mint refresh token
│   └── requirements.txt
├── infra/                           # Terraform (GCP)
│   ├── main.tf                      # Wires bigquery / iam / function modules
│   ├── variables.tf, output.tf, versions.tf
│   ├── terraform.tfvars             # local overrides (gitignored)
│   ├── schemas/gmail_messages.json  # BQ table schema
│   └── modules/
│       ├── bigquery/                # Datasets + raw landing table
│       ├── iam/                     # SAs, IAM, OAuth-token Secret Manager
│       └── function/                # Cloud Function v2 + source GCS bucket
├── dbt/                             # dbt-bigquery project (jobs_pipeline)
│   ├── dbt_project.yml
│   ├── profiles.yml                 # gitignored
│   └── models/
│       ├── staging/                 # stg_gmail_messages (view)
│       ├── intermediate/            # int_job_email_finder + classifier
│       └── marts/                   # mart_job_pipeline (partitioned table)
├── .github/workflows/
│   ├── ci.yml                       # dbt compile/test + terraform plan
│   └── ingest-and-build.yml         # manual: invoke ingestor → dbt build
├── creds/                           # gitignored (SA keys, .env)
└── README.md
```

## BigQuery datasets

| Dataset | Layer | Created by | Notes |
|---|---|---|---|
| `gmail_data` | raw | Terraform | Landing table `gmail_messages` (partitioned by `ingested_at`, clustered on `sender, thread_id`) and run-history table `ingestion_runs` (partitioned by `started_at`, clustered on `status, run_id`) |
| `gmail_staging` | dbt staging | Terraform (empty) / dbt | Views over the raw table |
| `gmail_intermediate` | dbt intermediate | Terraform (empty) / dbt | Ephemeral models (not materialised) |
| `gmail_marts` | dbt marts | Terraform (empty) / dbt | `mart_job_pipeline` — table partitioned by `received_at`, clustered on `job_pipeline_category, sender` |

## Service accounts

All three are managed in `infra/modules/iam/main.tf`.

| Service account | Purpose | Roles |
|---|---|---|
| `terraform-runner` | Runs Terraform itself (created manually before first `apply`). Also used as the `GCP_SA_KEY` identity in GitHub Actions to invoke the Cloud Function. | `roles/iam.serviceAccountAdmin`, `roles/iam.serviceAccountUser`, `roles/resourcemanager.projectIamAdmin`, `roles/serviceusage.serviceUsageAdmin`, `roles/bigquery.admin`, `roles/storage.admin`, `roles/secretmanager.admin`, `roles/cloudfunctions.developer`, `roles/run.admin` (the `cloudfunctions.developer` + `run.admin` pair grants invoke permission used by the workflow) |
| `gmail-bq-ingestor` | Runtime identity of the Cloud Function | `roles/bigquery.dataEditor`, `roles/bigquery.jobUser`, `roles/bigquery.dataViewer`, `roles/secretmanager.secretAccessor` on `gmail-user-oauth-token` |
| `dbt-runner` | Used by CI / local dbt runs | `roles/bigquery.dataEditor`, `roles/bigquery.jobUser` |

The Gmail user OAuth refresh token is stored in **Secret Manager** as
`gmail-user-oauth-token`. The Cloud Function mounts it at runtime as the
`GMAIL_USER_TOKEN_JSON` env var — the token itself never appears in
Terraform state, plain env vars, or function logs. Terraform owns the
secret resource; the version (the actual token bytes) is added
out-of-band by the operator (`gcloud secrets versions add`).

## Prerequisites

### 1. Google Cloud project setup

1. Create a project in Google Cloud (or pick an existing one).
2. Enable the following APIs (Terraform enables most of them, but the
   bootstrap APIs are needed up-front to *run* Terraform):
   - **Cloud Resource Manager API**
   - **Identity and Access Management (IAM) API**
   - **Service Usage API**
3. Create the `terraform-runner` SA in *IAM & Admin → IAM*, grant it the roles
   in the table above, and download a JSON key to `creds/key.json`.

### 2. Gmail user OAuth setup

The mailbox is a personal `@gmail.com` account, so the function authenticates
to Gmail with a **user OAuth refresh token** rather than service-account
delegation.

1. In Google Cloud Console → **APIs & Services → Credentials**, create an
   **OAuth 2.0 Client ID** of type *Desktop app*. Download its JSON to
   `creds/credentials.json`.
2. On the **OAuth consent screen**, add the target Gmail address as a *Test
   user*. (See the “Token lifetime” caveat below — published apps get
   non-expiring refresh tokens, test apps refresh every 7 days.)
3. Add the Gmail readonly scope:
   `https://www.googleapis.com/auth/gmail.readonly`.
4. Mint the refresh token locally:

   ```bash
   .venv/bin/python ingestion/scripts/get_user_token.py
   ```

   A browser will open. Sign in as the target Gmail user and approve the
   read-only Gmail scope. The script writes the resulting JSON to
   `creds/tokens.json`.
5. After running `terraform apply`, upload the token as a secret version:

   ```bash
   gcloud secrets versions add gmail-user-oauth-token \
       --project=<your-project> \
       --data-file=creds/tokens.json
   ```

Set the operator-facing values in `infra/terraform.tfvars`:

```hcl
project_id              = "<your-project>"
google_credentials_file = "/abs/path/to/creds/key.json"
```

**Token lifetime.** Google issues *non-expiring* refresh tokens only when the
OAuth consent screen is in *Production* and (for sensitive scopes like
Gmail) the app has been verified. For an unverified app in *Testing*, the
refresh token expires after 7 days and you have to re-mint it. Re-running
`get_user_token.py` plus a fresh `gcloud secrets versions add` is enough —
the function reads the `latest` version on every cold start.

## Provision infrastructure

```bash
cd infra
terraform init
terraform plan
terraform apply
```

Useful outputs (after apply):

```bash
terraform output cloud_function_url    # HTTPS trigger URL
terraform output bigquery_table        # <project>.gmail_data.gmail_messages
```

The `function` module zips `ingestion/` at plan time via the `archive_file`
data source and uploads it to a GCS staging bucket (`<project>-gcf-source`),
then deploys the Cloud Function. Subsequent `terraform apply` runs re-zip
and re-deploy whenever ingestion code changes.

## Running the ingestion

### End-to-end via GitHub Actions (recommended)

The `Ingest + dbt build` workflow (`.github/workflows/ingest-and-build.yml`)
is the production trigger. It is `workflow_dispatch` only — no automatic
schedule yet.

1. GitHub repo → **Actions** → **Ingest + dbt build** → **Run workflow**.
2. Optionally tick **full_backfill** to ignore the watermark and re-fetch
   from `FIRST_RUN_START_DATE`.

What it does:

1. Resolves the function URL via `gcloud functions describe --gen2`.
2. POSTs to it with an OIDC token (`--max-time 3600`, matching the function
   timeout). The function is synchronous, so HTTP 200 IS the completion
   signal — no polling required.
3. Runs `dbt build --target prod` against the freshly-loaded rows.

The workflow uses the `GCP_SA_KEY` secret (same key already used by `ci.yml`).
The underlying SA needs `cloudfunctions.invoker` + `run.invoker` on the
function — the `terraform-runner` role bundle already provides this.

### Manually invoke the Cloud Function from your shell

Useful for re-running with custom args or when iterating on the function code:

```bash
cd ingestion
export FUNCTION_URL=$(cd ../infra && terraform output -raw cloud_function_url)
python scripts/run_ingestion.py --trigger
```

This POSTs to the function URL with an OIDC token minted from your ADC
identity (so your user/SA needs `roles/cloudfunctions.invoker` +
`roles/run.invoker` on the function).

Inspect recent runs:

```bash
gcloud logging read \
  'resource.type=cloud_function AND resource.labels.function_name=gmail-bigquery-ingestor' \
  --limit=50 --format='value(timestamp,severity,textPayload)'
```

### Run locally against real Gmail + BigQuery

```bash
cd ingestion
bash scripts/setup_venv.sh
source .venv/bin/activate

# BigQuery: use any SA key with bigquery.dataEditor + jobUser
export GOOGLE_APPLICATION_CREDENTIALS=$(pwd)/../creds/key.json

# Gmail: point at the locally-minted user OAuth token (alternative to
# uploading it to Secret Manager).
export GMAIL_USER_TOKEN_PATH=$(pwd)/../creds/tokens.json

python scripts/run_ingestion.py              # incremental
python scripts/run_ingestion.py --backfill   # ignore watermark, full re-sync
```

## Configuration (ingestion)

Settings live in `ingestion/src/gmail_ingestion/settings.py` (Pydantic) and
are read from env vars / `.env`.

| Variable | Default | Notes |
|---|---|---|
| `GCP_PROJECT_ID` / `PROJECT_ID` | `jobs-and-career-494813` | GCP project |
| `BQ_DATASET_ID` / `DATASET_ID` | `gmail_data` | Raw landing dataset |
| `BQ_TABLE_ID` / `TABLE_ID` | `gmail_messages` | Raw landing table |
| `BQ_RUNS_TABLE_ID` / `RUNS_TABLE_ID` | `ingestion_runs` | Run-history audit table that owns the watermark |
| `GMAIL_USER_TOKEN_JSON` | *(required on GCF)* | User OAuth refresh-token JSON. Mounted from the `gmail-user-oauth-token` Secret Manager secret. |
| `GMAIL_USER_TOKEN_PATH` | *(local dev only)* | Filesystem path to the same JSON, used instead of inlining via `GMAIL_USER_TOKEN_JSON`. |
| `FIRST_RUN_START_DATE` | `2026-03-01` | Watermark floor for the cold start (no run rows yet) |
| `GMAIL_QUERY_EXTRA` | `in:inbox` | Extra Gmail search operators |
| `MAX_MESSAGES_PER_RUN` | `0` | Safety cap *per window*; `0` = unlimited |
| `INGEST_BATCH_SIZE` | `50` | Number of full Gmail messages buffered before each BigQuery insert |
| `MAX_BODY_CHARS` | `512000` | Per-message body truncation (`plain_body` + `html_body`). `0` disables. |
| `WINDOW_DAYS` | `1` | Width of each ingestion window. Smaller = finer crash-recovery granularity. |

## dbt models

```
sources(gmail_data.gmail_messages)
        │
        ▼
stg_gmail_messages          # view in gmail_staging
        │
        ├──▶ int_job_email_finder       # ephemeral: filters job-related mail
        └──▶ int_job_email_classifier   # ephemeral: regex-based category
                  │
                  ▼
        mart_job_pipeline   # table in gmail_marts
        # partitioned by received_at (day), clustered by category + sender
```

Categories produced by `int_job_email_classifier`: `offer`, `rejection`,
`interview scheduling`, `applications submitted`, `recruiter outreach`,
`networking`, `other`.

### Data quality

`dbt/models/staging/sources.yml` declares the assertions enforced by
`dbt build` / `dbt test`:

- `gmail_data.gmail_messages` — `message_id` is `unique` + `not_null`,
  `received_at` is `not_null`. Source freshness warns at 24h and errors
  at 72h since the most recent `received_at` (run `dbt source freshness`).
- `gmail_data.ingestion_runs` — `run_id`, `status`, `sync_start`,
  `sync_end` are `not_null`; `status` must be `ok` or `error`.
- `stg_gmail_messages` — column not-null/unique tests plus
  `dbt_utils.recency` on `received_at` (72h).

`dbt build` is the wire in `.github/workflows/ingest-and-build.yml` and
fails the workflow on any test failure (default dbt behaviour).

### Run dbt locally

`dbt/profiles.yml` is **gitignored**. Create it from `profiles.yml.example`
(or copy the snippet below) and point `keyfile` at a key for the
`dbt-runner` SA — or your own ADC key for dev:

```yaml
jobs_pipeline:
  target: dev
  outputs:
    dev:
      type: bigquery
      method: service-account
      project: <your-project>
      dataset: gmail_staging
      keyfile: /abs/path/to/creds/key.json
      location: US
      threads: 4
      timeout_seconds: 300
```

Then:

```bash
cd dbt
pip install -r requirements.txt    # dbt-bigquery
dbt deps
dbt build --target dev             # run + test all models
```

## CI / CD workflows

Two workflows under `.github/workflows/`:

- **`ci.yml`** — runs on every PR touching `dbt/`, `ingestion/`, or `infra/`:
  1. `dbt-test` — installs `dbt-bigquery`, writes `GCP_SA_KEY` from secrets
     to a keyfile, runs `dbt compile + dbt test` against the `prod` target.
  2. `terraform-plan` — runs `terraform init && terraform plan` in `infra/`
     using `GOOGLE_CREDENTIALS` from secrets.

- **`ingest-and-build.yml`** — manual `workflow_dispatch`: POSTs to the
  Cloud Function (blocks until ingestion completes), then `dbt build --target prod`.

Required GitHub repo secret: **`GCP_SA_KEY`** — JSON key for a SA with
`dbt-runner` + `terraform-runner` permissions, including
`cloudfunctions.admin` / `run.admin` (used by `ingest-and-build.yml` to invoke
the function).

## Security

- **Never commit `creds/`.** The repo's `.gitignore` excludes `creds/`,
  `*-key.json`, `service-account*.json`, `client_secret*.json`,
  `tokens.json`, `.env*`, `*.tfvars`, and `*.tfstate*`.
- The Gmail user OAuth token is stored only in **Secret Manager** in
  production. The local copy under `creds/tokens.json` is for dev only and
  must not be committed.
- The OAuth refresh token grants read-only Gmail access for the consenting
  user. Treat it like a password. To revoke, delete the secret version (or
  remove the consent at <https://myaccount.google.com/permissions>).
- `dbt/profiles.yml` is gitignored — only `profiles.yml.example` (local-dev
  starter) and `profiles.ci.yml` (CI template, secret-free, uses `env_var()`)
  are committed.
- Terraform state is local (`infra/.terraform/`) and gitignored. Move it to a
  GCS backend before sharing the project with multiple operators.

## Troubleshooting

- **`No Gmail user OAuth token found`** — locally, set `GMAIL_USER_TOKEN_PATH`
  (or `GMAIL_USER_TOKEN_JSON`) before running. On Cloud Functions, ensure
  Terraform applied the `secret_environment_variables` block and that you
  added a version to `gmail-user-oauth-token` with `gcloud secrets versions
  add`. The function reads `latest` on each cold start.
- **`unauthorized_client` from Google's OAuth token endpoint** — the OAuth
  client ID baked into the refresh token has been deleted/disabled, or the
  Gmail readonly scope was removed from the consent screen. Re-mint the
  token with `scripts/get_user_token.py` and upload a new secret version.
- **`invalid_grant` on refresh** — the refresh token was revoked or expired.
  Most often this is the 7-day expiry for tokens minted from a *Testing*
  consent screen (see “Token lifetime” above). Re-mint and re-upload.
- **First run inserts 0 rows** — check `FIRST_RUN_START_DATE`; it gates how
  far back the initial backfill goes. Use `--backfill` to force.
- **Watermark not advancing** — the watermark is derived from
  `MAX(received_at)` in BigQuery, not from anything Terraform-managed. If
  the table is empty after a failed first run, the next run will re-start
  from `FIRST_RUN_START_DATE`.

## Useful console links

- BigQuery dataset: <https://console.cloud.google.com/bigquery>
- Cloud Function: <https://console.cloud.google.com/functions>
- Secret Manager: <https://console.cloud.google.com/security/secret-manager>
- Vertex AI / BQML (optional, for downstream ML on the marts):
  enable the Vertex AI API at
  <https://console.developers.google.com/apis/api/aiplatform.googleapis.com/overview>
