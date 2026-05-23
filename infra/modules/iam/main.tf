variable "project_id" { type = string }
variable "dbt_sa_account_id" { type = string }
variable "ingestion_sa_account_id" { type = string }
variable "bq_dataset_id" { type = string }

# terraform-runner is created manually (see README) — its bindings are
# declared here so they're auditable in code rather than only in the console.
variable "terraform_runner_sa_account_id" {
  type    = string
  default = "terraform-runner"
}

locals {
  terraform_runner_sa_email = "${var.terraform_runner_sa_account_id}@${var.project_id}.iam.gserviceaccount.com"
}

# ── Required APIs (all disabled on destroy so the project fully tears down) ──

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

resource "google_project_service" "cloudbuild" {
  project            = var.project_id
  service            = "cloudbuild.googleapis.com"
  disable_on_destroy = true
}

# Cloud Functions v2 implicitly enables Artifact Registry; declare it so it's
# also disabled on destroy.
resource "google_project_service" "artifactregistry" {
  project            = var.project_id
  service            = "artifactregistry.googleapis.com"
  disable_on_destroy = true
}

# ── dbt runner SA ────────────────────────────────────────────────────────────

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

# ── Gmail ingestor SA (Cloud Function runtime identity) ──────────────────────
# Used for BigQuery via ADC and for reading the OAuth-token secret.
# Gmail itself is accessed with a user OAuth refresh token (see auth.py).

resource "google_service_account" "gmail_ingestor" {
  account_id   = var.ingestion_sa_account_id
  display_name = "Gmail to BigQuery Ingestion Service Account"
  project      = var.project_id
  depends_on   = [google_project_service.iam]
}

resource "google_project_iam_member" "gmail_ingestor_bq_data_editor" {
  project = var.project_id
  role    = "roles/bigquery.dataEditor"
  member  = "serviceAccount:${google_service_account.gmail_ingestor.email}"
}

resource "google_project_iam_member" "gmail_ingestor_bq_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.gmail_ingestor.email}"
}

# Read table data for the dedup guard
resource "google_project_iam_member" "gmail_ingestor_bq_data_viewer" {
  project = var.project_id
  role    = "roles/bigquery.dataViewer"
  member  = "serviceAccount:${google_service_account.gmail_ingestor.email}"
}

resource "google_secret_manager_secret_iam_member" "ingestor_gmail_token_access" {
  project    = var.project_id
  secret_id  = google_secret_manager_secret.gmail_user_oauth_token.secret_id
  role       = "roles/secretmanager.secretAccessor"
  member     = "serviceAccount:${google_service_account.gmail_ingestor.email}"
  depends_on = [google_secret_manager_secret.gmail_user_oauth_token]
}

# ── Gmail user OAuth token secret ────────────────────────────────────────────
# Versions (the actual token bytes) are added out-of-band by the operator
# (`gcloud secrets versions add`) so refresh-token material never enters TF state.

resource "google_secret_manager_secret" "gmail_user_oauth_token" {
  project   = var.project_id
  secret_id = "gmail-user-oauth-token"

  replication {
    auto {}
  }

  labels = {
    managed-by = "terraform"
    purpose    = "gmail-ingestion"
  }

  depends_on = [google_project_service.secretmanager]
}

# ── terraform-runner project-level roles ─────────────────────────────────────
# Bootstrap order: the very first apply needs these granted manually
# (gcloud add-iam-policy-binding) because Terraform cannot grant itself
# permissions it does not yet have.

locals {
  terraform_runner_project_roles = toset([
    "roles/iam.serviceAccountAdmin",
    "roles/iam.serviceAccountUser",
    "roles/resourcemanager.projectIamAdmin",
    "roles/serviceusage.serviceUsageAdmin",
    "roles/bigquery.admin",
    "roles/storage.admin",
    "roles/secretmanager.admin",
    "roles/cloudfunctions.developer",
    "roles/run.admin",
  ])
}

resource "google_project_iam_member" "terraform_runner" {
  for_each = local.terraform_runner_project_roles
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${local.terraform_runner_sa_email}"
}

# Allow terraform-runner to attach the ingestor SA to the Cloud Function.
resource "google_service_account_iam_member" "terraform_runner_actas_ingestor" {
  service_account_id = google_service_account.gmail_ingestor.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${local.terraform_runner_sa_email}"
}

# ── Outputs ──────────────────────────────────────────────────────────────────

output "ingestor_sa_email" {
  value = google_service_account.gmail_ingestor.email
}

output "ingestor_sa_name" {
  value = google_service_account.gmail_ingestor.name
}

output "gmail_user_token_secret_id" {
  value = google_secret_manager_secret.gmail_user_oauth_token.secret_id
}
