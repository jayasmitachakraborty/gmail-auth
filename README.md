# gmail-auth

Incremental Gmail → BigQuery pipeline with a dbt transformation layer that
classifies job-search emails (applications, interviews, offers, rejections,
recruiter outreach, networking).

The pipeline runs on **Cloud Functions v2** and is triggered on demand from a
**GitHub Actions** workflow (`workflow_dispatch`). The same workflow then runs
`dbt build` against the freshly-loaded rows. All GCP resources are provisioned
by **Terraform**. The Gmail ingestor service account impersonates the target
Gmail user via domain-wide delegation, using a service-account key stored in
Secret Manager.

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
      │ (subject     │         │ gmail_data.  │
      │  impersonat.)│         │ gmail_messages│
      └──────────────┘         └──────┬───────┘
                                      │
                                      ▼
                            ┌──────────────────┐
                            │   dbt (BigQuery) │ ← same workflow,
                            │ staging → int →  │   runs after the
                            │ marts            │   function returns
                            └──────────────────┘
```

- **Watermark** — every run picks up from `MAX(received_at)` in
  `gmail_data.gmail_messages`. First run falls back to `FIRST_RUN_START_DATE`
  (default `2026-03-01`). The watermark is floored to whole seconds because
  Gmail's `after:` operator only accepts epoch seconds; sub-second overlap is
  deduped by `message_id` in `load.py`.
- **Dedup** — before each insert batch, `load.py` checks which `message_id`s
  already exist and skips them. Re-runs and overlapping windows are safe.

## Repo layout

```
gmail-auth/
├── ingestion/                       # Cloud Function source (Python)
│   ├── src/gmail_ingestion/
│   │   ├── auth.py                  # ADC + Gmail subject impersonation
│   │   ├── fetch.py                 # Gmail API list/get/transform
│   │   ├── load.py                  # BigQuery dedup + streaming insert
│   │   ├── watermark.py             # MAX(received_at) lookup
│   │   └── settings.py              # Pydantic settings from env vars
│   ├── scripts/run_ingestion.py     # Cloud Function entry + local CLI
│   └── requirements.txt
├── infra/                           # Terraform (GCP)
│   ├── main.tf                      # Wires bigquery / iam / scheduler modules
│   ├── variables.tf, output.tf, versions.tf
│   ├── terraform.tfvars             # local overrides (gitignored)
│   ├── schemas/gmail_messages.json  # BQ table schema
│   └── modules/
│       ├── bigquery/                # Datasets + raw landing table
│       ├── iam/                     # SAs, IAM, SA key in Secret Manager
│       └── scheduler/               # Cloud Function v2 (legacy folder name)
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
| `gmail_data` | raw | Terraform | Landing table `gmail_messages`, partitioned by `ingested_at`, clustered on `sender, thread_id` |
| `gmail_staging` | dbt staging | Terraform (empty) / dbt | Views over the raw table |
| `gmail_intermediate` | dbt intermediate | Terraform (empty) / dbt | Ephemeral models (not materialised) |
| `gmail_marts` | dbt marts | Terraform (empty) / dbt | `mart_job_pipeline` — table partitioned by `received_at`, clustered on `job_pipeline_category, sender` |

## Service accounts

All three are managed in `infra/modules/iam/main.tf`.

| Service account | Purpose | Roles |
|---|---|---|
| `terraform-runner` | Runs Terraform itself (you create this manually before first `apply`). Also used as the `GCP_SA_KEY` identity in GitHub Actions, so it must be able to invoke the Cloud Function. | `roles/iam.serviceAccountAdmin`, `roles/bigquery.admin`, `roles/serviceusage.serviceUsageAdmin`, `roles/resourcemanager.projectIamAdmin`, `roles/secretmanager.admin`, `roles/cloudfunctions.admin`, `roles/run.admin`, `roles/storage.admin` (the `cloudfunctions.admin` + `run.admin` pair grants the `invoke` permission needed by the workflow) |
| `gmail-bq-ingestor` | Runtime identity of the Cloud Function | `roles/bigquery.dataEditor`, `roles/bigquery.jobUser`, `roles/bigquery.dataViewer`, `roles/secretmanager.secretAccessor` (on the Gmail SA key secret) |
| `dbt-runner` | Used by CI / local dbt runs | `roles/bigquery.dataEditor`, `roles/bigquery.jobUser` |

The `gmail-bq-ingestor` SA key is generated by Terraform and immediately
written to **Secret Manager** as `gmail-ingestor-sa-key`. The Cloud Function
mounts it as `GMAIL_SA_KEY_JSON` — the raw key is never exposed as a plain env
var.

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

### 2. Gmail domain-wide delegation

The `gmail-bq-ingestor` SA cannot read Gmail without **subject impersonation**.

For a **personal Gmail** account: in the Google Cloud Console, create an OAuth
consent screen, then on the SA's *Keys* page enable *domain-wide delegation*
and grant the scope `https://www.googleapis.com/auth/gmail.readonly` to the
SA's client ID. For **Google Workspace**, the workspace admin does this in
*Admin Console → Security → API controls → Domain-wide delegation*.

Set the target Gmail address in `infra/terraform.tfvars`:

```hcl
gmail_user_email = "you@example.com"
google_credentials_file = "/abs/path/to/creds/key.json"
```

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

The `scheduler` module (legacy name — it no longer creates Cloud Scheduler
resources, only the Cloud Function) zips `ingestion/` at plan time via the
`archive_file` data source and uploads it to a GCS staging bucket
(`<project>-gcf-source`), then deploys the Cloud Function. Subsequent
`terraform apply` runs re-zip and re-deploy whenever ingestion code changes.

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
xs
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

export GOOGLE_APPLICATION_CREDENTIALS=$(pwd)/../creds/key.json
export GMAIL_USER_EMAIL=you@example.com
export GMAIL_SA_KEY_JSON="$(cat ../creds/gmail-bq-ingestor-key.json)"

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
| `GMAIL_USER_EMAIL` | *(required)* | Address the SA impersonates |
| `GMAIL_SA_KEY_JSON` | *(required)* | SA key JSON string (Secret Manager-injected on GCF) |
| `FIRST_RUN_START_DATE` | `2026-03-01` | Watermark floor for the first run |
| `GMAIL_QUERY_EXTRA` | `in:inbox` | Extra Gmail search operators |
| `MAX_MESSAGES_PER_RUN` | `0` | Safety cap; `0` = unlimited |

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
- The Gmail SA key is stored only in **Secret Manager** in production. The
  local copy under `creds/` is for dev only.
- `dbt/profiles.yml` is gitignored — only `profiles.yml.example` is committed.
- Terraform state is local (`infra/.terraform/`) and gitignored. Move it to a
  GCS backend before sharing the project with multiple operators.

## Troubleshooting

- **`GMAIL_SA_KEY_JSON env var is not set`** — locally, export the SA key JSON
  string before running. On Cloud Function, ensure Terraform applied the
  `secret_environment_variables` block.
- **`403 Domain-Wide Delegation … not authorized`** — the SA's client ID is
  not whitelisted for `gmail.readonly`. Re-do the delegation step in the
  Workspace / personal account admin UI.
- **First run inserts 0 rows** — check `FIRST_RUN_START_DATE`; it gates how
  far back the initial backfill goes. Use `--backfill` to force.
- **Watermark not advancing** — the watermark is derived from
  `MAX(received_at)` in BigQuery, not from anything Terraform-managed. If the
  table is empty after a failed first run, the next run will re-start from
  `FIRST_RUN_START_DATE`.

## Useful console links

- BigQuery dataset: <https://console.cloud.google.com/bigquery>
- Cloud Function: <https://console.cloud.google.com/functions>
- Secret Manager: <https://console.cloud.google.com/security/secret-manager>
- Vertex AI / BQML (optional, for downstream ML on the marts):
  enable the Vertex AI API at
  <https://console.developers.google.com/apis/api/aiplatform.googleapis.com/overview>
