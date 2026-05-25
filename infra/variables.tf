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
  description = "Path to the terraform-runner SA key JSON. Leave empty in CI to fall back to GOOGLE_CREDENTIALS / ADC."
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

variable "first_run_start_date" {
  type        = string
  default     = "2026-03-01"
  description = "ISO-8601 date floor for the initial backfill"
}

variable "gmail_query_extra" {
  type    = string
  default = "in:inbox"
}

variable "function_memory" {
  type        = string
  default     = "2Gi"
  description = "Memory allocated to the Gmail ingestion Cloud Function"
}

variable "function_cpu" {
  type        = string
  default     = "1"
  description = "vCPU allocated to the Gmail ingestion Cloud Function (>=1 required for memory above 512Mi)"
}
