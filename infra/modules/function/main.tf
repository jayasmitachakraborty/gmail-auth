# Cloud Function v2 that runs the Gmail → BigQuery ingestion pipeline.
# Invoked over authenticated HTTPS from .github/workflows/ingest-and-build.yml.

variable "project_id" { type = string }
variable "region" { type = string }
variable "ingestor_sa_email" { type = string }
variable "gmail_user_token_secret_id" { type = string }
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

variable "function_timeout_seconds" {
  type        = number
  default     = 3600
  description = "Max seconds for a single function invocation (backfill may need ~1h)"
}

resource "google_storage_bucket" "function_source" {
  name                        = "${var.project_id}-gcf-source"
  location                    = var.region
  project                     = var.project_id
  uniform_bucket_level_access = true
  force_destroy               = true

  lifecycle_rule {
    action { type = "Delete" }
    condition { age = 30 }
  }
}

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

resource "google_cloudfunctions2_function" "gmail_ingestor" {
  name        = "gmail-bigquery-ingestor"
  location    = var.region
  project     = var.project_id
  description = "Incremental Gmail → BigQuery sync, invoked on demand via authenticated HTTPS"

  build_config {
    runtime     = "python311"
    entry_point = "run_pipeline_http"

    source {
      storage_source {
        bucket = google_storage_bucket.function_source.name
        object = google_storage_bucket_object.function_source_zip.name
      }
    }

    environment_variables = {
      PYTHONPATH = "src"
    }
  }

  service_config {
    service_account_email = var.ingestor_sa_email
    timeout_seconds       = var.function_timeout_seconds
    available_memory      = "512M"
    max_instance_count    = 1 # serialise runs

    environment_variables = {
      GCP_PROJECT_ID       = var.project_id
      BQ_DATASET_ID        = var.bq_dataset_id
      BQ_TABLE_ID          = var.bq_table_id
      FIRST_RUN_START_DATE = var.first_run_start_date
      GMAIL_QUERY_EXTRA    = var.gmail_query_extra
    }

    # GMAIL_USER_TOKEN_JSON is mounted from Secret Manager — never as plain env.
    secret_environment_variables {
      key        = "GMAIL_USER_TOKEN_JSON"
      project_id = var.project_id
      secret     = var.gmail_user_token_secret_id
      version    = "latest"
    }
  }
}

# Cloud Functions v2 implicitly creates an Artifact Registry repo
# (gcf-artifacts) and gcf-v2-{sources,uploads}-* GCS buckets that aren't in
# Terraform state. Clean them up on destroy so the project tears down fully.
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

output "cloud_function_name" {
  value = google_cloudfunctions2_function.gmail_ingestor.name
}

output "cloud_function_url" {
  value = google_cloudfunctions2_function.gmail_ingestor.service_config[0].uri
}
