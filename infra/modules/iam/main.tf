variable "project_id" { type = string }
variable "dbt_sa_account_id" { type = string }
variable "ingestion_sa_account_id" { type = string }
variable "bq_dataset_id" { type = string }

# ── Enable required APIs ──────────────────────────────────────────────────────

# All APIs are disabled on destroy so the project is left fully torn down.
# This is safe because the project is dedicated to the gmail-auth pipeline.

resource "google_project_service" "iam" {
  project            = var.project_id
  service            = "iam.googleapis.com"
  disable_on_destroy = true
}

resource "google_project_service" "secretmanager" {
  project            = var.project_id
  service            = "secretmanager.googleapis.com"
  disable_on_destroy = true
}

resource "google_project_service" "cloudfunctions" {
  project            = var.project_id
  service            = "cloudfunctions.googleapis.com"
  disable_on_destroy = true
}

resource "google_project_service" "run" {
  project            = var.project_id
  service            = "run.googleapis.com"
  disable_on_destroy = true
}

resource "google_project_service" "cloudscheduler" {
  project            = var.project_id
  service            = "cloudscheduler.googleapis.com"
  disable_on_destroy = true
}

resource "google_project_service" "cloudbuild" {
  project            = var.project_id
  service            = "cloudbuild.googleapis.com"
  disable_on_destroy = true
}

# Cloud Functions v2 implicitly enables Artifact Registry to store build
# images. Declare it explicitly so it is also disabled on destroy.
resource "google_project_service" "artifactregistry" {
  project            = var.project_id
  service            = "artifactregistry.googleapis.com"
  disable_on_destroy = true
}

# ── dbt runner SA ─────────────────────────────────────────────────────────────

resource "google_service_account" "dbt_runner" {
  account_id   = var.dbt_sa_account_id
  display_name = "dbt runner"
  project      = var.project_id
  depends_on   = [google_project_service.iam]
}

resource "google_project_iam_member" "dbt_bq_editor" {
  project = var.project_id
  role    = "roles/bigquery.dataEditor"
  member  = "serviceAccount:${google_service_account.dbt_runner.email}"
}

resource "google_project_iam_member" "dbt_bq_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.dbt_runner.email}"
}

# ── Gmail ingestor SA ─────────────────────────────────────────────────────────
# This SA is the runtime identity of the Cloud Function.
# It also impersonates the Gmail user via domain-wide delegation using
# a key stored in Secret Manager

resource "google_service_account" "gmail_ingestor" {
  account_id   = var.ingestion_sa_account_id
  display_name = "Gmail to BigQuery Ingestion Service Account"
  project      = var.project_id
  depends_on   = [google_project_service.iam]
}

# BigQuery: write rows
resource "google_project_iam_member" "gmail_ingestor_bq_data_editor" {
  project = var.project_id
  role    = "roles/bigquery.dataEditor"
  member  = "serviceAccount:${google_service_account.gmail_ingestor.email}"
}

# BigQuery: run query jobs (watermark SELECT, dedup SELECT)
resource "google_project_iam_member" "gmail_ingestor_bq_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.gmail_ingestor.email}"
}

# BigQuery: read table data for dedup guard
resource "google_project_iam_member" "gmail_ingestor_bq_data_viewer" {
  project = var.project_id
  role    = "roles/bigquery.dataViewer"
  member  = "serviceAccount:${google_service_account.gmail_ingestor.email}"
}

# Secret Manager: read the Gmail SA key at function runtime
resource "google_secret_manager_secret_iam_member" "ingestor_gmail_key_access" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.gmail_sa_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.gmail_ingestor.email}"

  depends_on = [google_secret_manager_secret.gmail_sa_key]
}

# ── SA key → Secret Manager ───────────────────────────────────────────────────
# The key is used for Gmail subject impersonation (domain-wide delegation).
# It is stored in Secret Manager so it never appears in plaintext in env vars
# or Terraform plan output.

resource "google_service_account_key" "gmail_ingestor_key" {
  service_account_id = google_service_account.gmail_ingestor.name
}

resource "google_secret_manager_secret" "gmail_sa_key" {
  project   = var.project_id
  secret_id = "gmail-ingestor-sa-key"

  replication {
    auto {}
  }

  labels = {
    managed-by = "terraform"
    purpose    = "gmail-ingestion"
  }

  depends_on = [google_project_service.secretmanager]
}

resource "google_secret_manager_secret_version" "gmail_sa_key_v1" {
  secret      = google_secret_manager_secret.gmail_sa_key.id
  secret_data = base64decode(google_service_account_key.gmail_ingestor_key.private_key)
}

# ── Outputs ───────────────────────────────────────────────────────────────────

output "ingestor_sa_email" {
  value = google_service_account.gmail_ingestor.email
}

output "ingestor_sa_name" {
  value = google_service_account.gmail_ingestor.name
}

output "gmail_sa_key_secret_id" {
  value = google_secret_manager_secret.gmail_sa_key.secret_id
}
