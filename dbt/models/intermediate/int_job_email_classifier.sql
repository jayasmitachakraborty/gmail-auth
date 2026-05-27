-- Assigns one job_pipeline_category per email. Reads from the finder
-- so we only classify emails that already look job-related.

select
    *,
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
        -- Catch rejections whose subject is generic ("Update on your application")
        -- but whose body carries the rejection language.
        when regexp_contains(lower(plain_body),
            r'\b(unfortunately|not moving forward|decided to proceed with other candidates|not selected)\b')
        then 'rejection'
        else 'other'
    end as job_pipeline_category
from {{ ref('int_job_email_finder') }}
