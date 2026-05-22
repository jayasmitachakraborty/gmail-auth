with finder as (
    select * from {{ ref('int_job_email_finder') }}
),

classifier as (
    select * from {{ ref('int_job_email_classifier') }}
)

select
    f.message_id,
    f.sender,
    f.subject,
    f.snippet,
    f.plain_body,
    f.labels,
    f.received_at,
    f.is_job_email,
    c.job_pipeline_category
from finder f
inner join classifier c
    on f.message_id = c.message_id