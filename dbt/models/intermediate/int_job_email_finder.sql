with base as (
    select * from {{ ref('stg_gmail_messages') }}
),

classified as (
    select
        *,
        case
            when regexp_contains(lower(sender),
                r'jobs-noreply@linkedin|greenhouse|lever|ashby|workday|smartrecruiters|indeed|jobs|recruit|ziprecruiter|wellfound')
            then true
            when regexp_contains(lower(subject),
                r'job|application|interview|recruiter|position|opportunity|career|hiring')
            then true
            when regexp_contains(lower(snippet),
                r'application|resume|cv|interview|hiring manager')
            then true
            else false
        end as is_job_email
    from base
    where not labels_lower like '%category_promotions%'
)

select * from classified
where is_job_email = true