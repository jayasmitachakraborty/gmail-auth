with base as (
    select * from {{ ref('stg_gmail_messages') }}
)

select
    message_id,
    sender,
    subject,
    snippet,
    case
        when regexp_contains(subject_snippet_lower,
            r'\b(offer|congratulations|pleased to offer|we are excited to offer)\b')
        then 'offer'
        when regexp_contains(subject_snippet_lower,
            r'\b(unfortunately|not moving forward|decided to proceed with other candidates|not selected|rejection|move forward with other candidates)\b')
        then 'rejection'
        when regexp_contains(subject_snippet_lower,
            r'\b(interview|schedule a call|availability|available times|calendar invite|meet with|phone screen|technical screen)\b')
        then 'interview scheduling'
        when regexp_contains(subject_snippet_lower,
            r'\b(application received|thank you for applying|your application|submitted|confirmation)\b')
        then 'applications submitted'
        when regexp_contains(subject_snippet_lower,
            r'\b(recruiter|sourcer|talent acquisition|opportunity|hiring for|came across your profile|open role)\b')
        then 'recruiter outreach'
        when regexp_contains(subject_snippet_lower,
            r'\b(coffee chat|networking|intro|introduction|referral|connect|catch up)\b')
        then 'networking'
        when regexp_contains(lower(plain_body),
            r'\b(unfortunately|not moving forward|decided to proceed with other candidates|not selected|rejection)\b')
        then 'rejection'
        else 'other'
    end as job_pipeline_category
from base