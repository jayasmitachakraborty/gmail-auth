provider "google" {
  project = var.project_id
  region  = var.region
  # When google_credentials_file is set (local dev), read the key from disk.
  # When empty (CI), fall back to the provider's env-var resolution
  # (GOOGLE_CREDENTIALS / GOOGLE_APPLICATION_CREDENTIALS / ADC).
  credentials = var.google_credentials_file != "" ? file(var.google_credentials_file) : null
}

module "bigquery" {
  source            = "./modules/bigquery"
  project_id        = var.project_id
  dataset_id        = var.dataset_id
  table_id          = var.table_id
  location          = var.location
  bigquery_location = var.location
}

module "iam" {
  source                  = "./modules/iam"
  project_id              = var.project_id
  dbt_sa_account_id       = "dbt-runner"
  ingestion_sa_account_id = "gmail-bq-ingestor"
  bq_dataset_id           = var.dataset_id
}

module "scheduler" {
  source     = "./modules/scheduler"
  project_id = var.project_id
  region     = var.region

  ingestor_sa_email      = module.iam.ingestor_sa_email
  gmail_sa_key_secret_id = module.iam.gmail_sa_key_secret_id

  gmail_user_email     = var.gmail_user_email
  first_run_start_date = var.first_run_start_date
  gmail_query_extra    = var.gmail_query_extra

  # BigQuery table the function reads/writes
  bq_dataset_id = var.dataset_id
  bq_table_id   = var.table_id
}
