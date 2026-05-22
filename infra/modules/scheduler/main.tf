# infra/modules/scheduler/main.tf
#
# Replaces the Cloud Run Job approach with:
#   Cloud Function v2 (HTTP trigger, runs the ingestion code)
#   Cloud Scheduler  (invokes the function daily via authenticated HTTP POST)
#
# Execution flow:
#   Cloud Scheduler ──POST──▶ Cloud Function HTTPS URL
#                                    │
#                                    ▼
#                            run_pipeline.main()
#                                    │
#                            Gmail API (subject impersonation)
#                            BigQuery  (ADC via function SA)
#
# The function source is zipped from ingestion/ at plan time via the
# archive_file data source.  CI/CD can re-deploy by running terraform apply
# or by pushing a new zip to the GCS staging bucket and running apply.

variable "project_id" { type = string }
variable "region" { type = string }
variable "ingestor_sa_email" { type = string }
variable "gmail_sa_key_secret_id" { type = string }
variable "gmail_user_email" { type = string }
variable "bq_dataset_id" { type = string }
variable "bq_table_id" { type = string }

variable "first_run_start_date" {
  type    = string
  default = "2026-03-01"
}

variable "gmail_query_extra" {
  type    = string
  default = "in:inbox"
}

variable "schedule_cron" {
  type        = string
  default     = "0 6 * * *"
  description = "Cron expression. Only fires automatically when schedule_paused = false."
}

variable "schedule_paused" {
  type        = bool
  default     = true
  description = <<-EOT
    If true, the scheduler job is created in PAUSED state and never fires on
    its own. Trigger manually with:
      gcloud scheduler jobs run gmail-ingestor-daily --location=<region>
    Set to false to resume the daily cron.
  EOT
}

variable "function_timeout_seconds" {
  type        = number
  default     = 3600
  description = "Max seconds for a single function invocation (backfill may need ~1 h)"
}

# ── GCS bucket for function source ───────────────────────────────────────────

resource "google_storage_bucket" "function_source" {
  name                        = "${var.project_id}-gcf-source"
  location                    = var.region
  project                     = var.project_id
  uniform_bucket_level_access = true
  force_destroy               = true

  lifecycle_rule {
    action { type = "Delete" }
    condition { age = 30 } # keep only last 30 days of deploys
  }
}

# Zip the ingestion/ directory at plan time.
# Requires the `archive` provider — add to versions.tf:
#   archive = { source = "hashicorp/archive", version = "~> 2.4" }
data "archive_file" "ingestion_source" {
  type        = "zip"
  source_dir  = "${path.root}/../ingestion"
  output_path = "${path.module}/.build/ingestion_source.zip"

  excludes = [
    "logs",
    "__pycache__",
    ".venv",
    "*.pyc",
    ".env",
    "creds",
  ]
}

resource "google_storage_bucket_object" "function_source_zip" {
  name   = "ingestion-${data.archive_file.ingestion_source.output_md5}.zip"
  bucket = google_storage_bucket.function_source.name
  source = data.archive_file.ingestion_source.output_path
}

# ── Cloud Function v2 ─────────────────────────────────────────────────────────

resource "google_cloudfunctions2_function" "gmail_ingestor" {
  name     = "gmail-bigquery-ingestor"
  location = var.region
  project  = var.project_id

  description = "Incremental Gmail → BigQuery sync, triggered daily by Cloud Scheduler"

  build_config {
    runtime     = "python311"
    entry_point = "run_pipeline_http" # HTTP wrapper in run_pipeline.py

    source {
      storage_source {
        bucket = google_storage_bucket.function_source.name
        object = google_storage_bucket_object.function_source_zip.name
      }
    }

    environment_variables = {
      # Tell pip where to find the local package during build
      PYTHONPATH = "src"
    }
  }

  service_config {
    service_account_email = var.ingestor_sa_email
    timeout_seconds       = var.function_timeout_seconds
    available_memory      = "512M"
    max_instance_count    = 1 # serialise runs — no concurrent ingestion

    environment_variables = {
      GCP_PROJECT_ID       = var.project_id
      BQ_DATASET_ID        = var.bq_dataset_id
      BQ_TABLE_ID          = var.bq_table_id
      GMAIL_USER_EMAIL     = var.gmail_user_email
      FIRST_RUN_START_DATE = var.first_run_start_date
      GMAIL_QUERY_EXTRA    = var.gmail_query_extra
    }

    # GMAIL_SA_KEY_JSON is mounted from Secret Manager — never plain env var.
    secret_environment_variables {
      key        = "GMAIL_SA_KEY_JSON"
      project_id = var.project_id
      secret     = var.gmail_sa_key_secret_id
      version    = "latest"
    }
  }
}

# ── Cloud Scheduler ───────────────────────────────────────────────────────────
# A dedicated invoker SA is used so the scheduler identity is separate from
# the function's runtime identity (principle of least privilege).

resource "google_service_account" "scheduler_invoker" {
  account_id   = "gmail-ingestor-invoker"
  display_name = "Cloud Scheduler invoker — gmail ingestor"
  project      = var.project_id
}

# Allow the invoker SA to call the Cloud Function
resource "google_cloudfunctions2_function_iam_member" "scheduler_invoker" {
  project        = var.project_id
  location       = var.region
  cloud_function = google_cloudfunctions2_function.gmail_ingestor.name
  role           = "roles/cloudfunctions.invoker"
  member         = "serviceAccount:${google_service_account.scheduler_invoker.email}"
}

# Also needs run.routes.invoke on the underlying Cloud Run service that
# backs Cloud Functions v2.
resource "google_cloud_run_service_iam_member" "scheduler_run_invoker" {
  project  = var.project_id
  location = var.region
  service  = google_cloudfunctions2_function.gmail_ingestor.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_invoker.email}"
}

# ── Destroy-time cleanup for resources auto-created by Cloud Functions v2 ─────
# Cloud Functions v2 implicitly creates:
#   - an Artifact Registry repo named `gcf-artifacts` (per region)
#   - GCS buckets `gcf-v2-sources-<project_number>-<region>` and
#     `gcf-v2-uploads-<project_number>-<region>` (managed by Cloud Build)
# None of these are in Terraform state, so `terraform destroy` would leave
# them behind and they would keep accruing storage cost. This null_resource
# shells out at destroy time to remove them. Errors are tolerated (|| true)
# so a missing repo/bucket doesn't fail the destroy.

resource "null_resource" "gcf_artifacts_cleanup" {
  triggers = {
    project_id = var.project_id
    region     = var.region
  }

  provisioner "local-exec" {
    when    = destroy
    command = <<-EOT
      set +e
      gcloud artifacts repositories delete gcf-artifacts \
        --location=${self.triggers.region} \
        --project=${self.triggers.project_id} \
        --quiet || true
      for b in $(gcloud storage buckets list \
        --project=${self.triggers.project_id} \
        --filter="name~^gcf-v2-(sources|uploads)-.*-${self.triggers.region}$" \
        --format="value(name)"); do
        gcloud storage rm -r "gs://$b" --quiet || true
      done
      exit 0
    EOT
  }

  depends_on = [google_cloudfunctions2_function.gmail_ingestor]
}

resource "google_cloud_scheduler_job" "daily_ingest" {
  name             = "gmail-ingestor-daily"
  description      = "Manual-trigger job for gmail-bigquery-ingestor (paused by default; run with `gcloud scheduler jobs run`)"
  schedule         = var.schedule_cron
  time_zone        = "UTC"
  project          = var.project_id
  region           = var.region
  attempt_deadline = "1800s"
  paused           = var.schedule_paused

  retry_config {
    retry_count = 2
  }

  http_target {
    http_method = "POST"
    uri         = google_cloudfunctions2_function.gmail_ingestor.service_config[0].uri

    # Pass body so run_pipeline_http can distinguish scheduler vs manual calls
    body = base64encode(jsonencode({ source = "cloud-scheduler" }))

    headers = {
      "Content-Type" = "application/json"
    }

    oidc_token {
      service_account_email = google_service_account.scheduler_invoker.email
      audience              = google_cloudfunctions2_function.gmail_ingestor.service_config[0].uri
    }
  }

  depends_on = [google_cloudfunctions2_function_iam_member.scheduler_invoker]
}

# ── Outputs ───────────────────────────────────────────────────────────────────

output "cloud_function_name" {
  value = google_cloudfunctions2_function.gmail_ingestor.name
}

output "cloud_function_url" {
  value = google_cloudfunctions2_function.gmail_ingestor.service_config[0].uri
}

output "scheduler_job_name" {
  value = google_cloud_scheduler_job.daily_ingest.name
}
