provider "google" {
  project = var.project_id
  region  = var.region
  # Local dev reads a key from disk; CI leaves this empty and uses
  # GOOGLE_CREDENTIALS / GOOGLE_APPLICATION_CREDENTIALS / ADC.
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

module "function" {
  source     = "./modules/function"
  project_id = var.project_id
  region     = var.region

  ingestor_sa_email          = module.iam.ingestor_sa_email
  gmail_user_token_secret_id = module.iam.gmail_user_token_secret_id

  first_run_start_date = var.first_run_start_date
  gmail_query_extra    = var.gmail_query_extra
  function_memory      = var.function_memory
  function_cpu         = var.function_cpu

  bq_dataset_id = var.dataset_id
  bq_table_id   = var.table_id
}
