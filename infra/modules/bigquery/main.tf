variable "project_id" { type = string }
variable "dataset_id" { type = string }
variable "table_id" { type = string }
variable "location" { type = string }
variable "bigquery_location" { type = string }

resource "google_project_service" "bigquery" {
  project            = var.project_id
  service            = "bigquery.googleapis.com"
  disable_on_destroy = true
}

resource "google_bigquery_dataset" "gmail_data" {
  project                    = var.project_id
  dataset_id                 = var.dataset_id
  location                   = var.bigquery_location
  description                = "Raw Gmail messages ingested from the Gmail API"
  delete_contents_on_destroy = true

  depends_on = [google_project_service.bigquery]
}

resource "google_bigquery_dataset" "staging" {
  project                    = var.project_id
  dataset_id                 = "gmail_staging"
  location                   = var.bigquery_location
  labels                     = { layer = "staging" }
  delete_contents_on_destroy = true

  depends_on = [google_project_service.bigquery]
}

resource "google_bigquery_dataset" "intermediate" {
  project                    = var.project_id
  dataset_id                 = "gmail_intermediate"
  location                   = var.bigquery_location
  labels                     = { layer = "intermediate" }
  delete_contents_on_destroy = true

  depends_on = [google_project_service.bigquery]
}

resource "google_bigquery_dataset" "marts" {
  project                    = var.project_id
  dataset_id                 = "gmail_marts"
  location                   = var.bigquery_location
  labels                     = { layer = "marts" }
  delete_contents_on_destroy = true

  depends_on = [google_project_service.bigquery]
}

resource "google_bigquery_table" "gmail_messages" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.gmail_data.dataset_id
  table_id            = var.table_id
  schema              = file("${path.root}/schemas/gmail_messages.json")
  deletion_protection = false

  time_partitioning {
    type  = "DAY"
    field = "ingested_at"
  }

  clustering = ["sender", "thread_id"]
}

output "dataset_id" {
  value = google_bigquery_dataset.gmail_data.dataset_id
}

output "table_id" {
  value = google_bigquery_table.gmail_messages.table_id
}
