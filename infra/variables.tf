variable "project_id" {
  type        = string
  description = "Google Cloud project ID"
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "google_credentials_file" {
  type        = string
  default     = ""
  description = "Path to the terraform-runner SA key JSON (used only by Terraform itself). Leave empty in CI and rely on GOOGLE_CREDENTIALS / ADC instead."
}

variable "location" {
  type    = string
  default = "US"
}

variable "dataset_id" {
  type    = string
  default = "gmail_data"
}

variable "table_id" {
  type    = string
  default = "gmail_messages"
}

# ── Ingestion pipeline ────────────────────────────────────────────────────────

variable "gmail_user_email" {
  type        = string
  description = "The Gmail address the ingestor SA will impersonate (subject delegation)"
}

variable "first_run_start_date" {
  type        = string
  default     = "2026-03-01"
  description = "ISO-8601 date floor for the initial backfill"
}

variable "gmail_query_extra" {
  type    = string
  default = "in:inbox"
}

variable "schedule_cron" {
  type        = string
  default     = "0 6 * * *"
  description = "Cron expression for the daily Cloud Scheduler job (UTC)"
}
