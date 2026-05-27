{{ config(
    partition_by={
        'field': 'received_at',
        'data_type': 'timestamp',
        'granularity': 'day'
    },
    cluster_by=['job_pipeline_category', 'sender']
) }}

-- Email-grain mart: one row per job-related Gmail message with its
-- rule-based pipeline category. Drops the staging helper columns
-- (subject_snippet_lower, labels_lower) that are only used during
-- transformation.

select
    message_id,
    thread_id,
    sender,
    subject,
    snippet,
    plain_body,
    labels,
    received_at,
    job_pipeline_category
from {{ ref('int_job_email_classifier') }}
