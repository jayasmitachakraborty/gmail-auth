output "dataset_id" {
  value = module.bigquery.dataset_id
}

output "table_id" {
  value = module.bigquery.table_id
}

output "bigquery_table" {
  value = "${var.project_id}.${module.bigquery.dataset_id}.${module.bigquery.table_id}"
}

output "ingestor_sa_email" {
  value = module.iam.ingestor_sa_email
}

output "gmail_sa_key_secret_id" {
  value = module.iam.gmail_sa_key_secret_id
}

output "cloud_function_name" {
  value = module.scheduler.cloud_function_name
}

output "cloud_function_url" {
  value       = module.scheduler.cloud_function_url
  description = "HTTPS trigger URL for the Gmail ingestor Cloud Function"
}

output "scheduler_job_name" {
  value = module.scheduler.scheduler_job_name
}

output "project_id" {
  value = var.project_id
}

output "region" {
  value = var.region
}
