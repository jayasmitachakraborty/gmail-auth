{{ config(
    materialized='table',
    partition_by={
        'field': 'latest_status_at',
        'data_type': 'timestamp',
        'granularity': 'day'
    },
    cluster_by=['latest_status', 'company']
) }}

-- Application-grain mart. One row per Gmail thread (~ one row per application
-- or recruiter conversation). Drives the dashboard scorecards, funnel, donut,
-- and company/role table.

with msgs as (
    select * from {{ ref('mart_job_pipeline') }}
    where thread_id is not null
),

ranked as (
    select
        thread_id,
        message_id,
        received_at,
        job_pipeline_category,
        case job_pipeline_category
            when 'offer'                  then 6
            when 'rejection'              then 5
            when 'interview scheduling'   then 4
            when 'applications submitted' then 3
            when 'recruiter outreach'     then 2
            when 'networking'             then 1
            else 0
        end as status_rank
    from msgs
),

latest as (
    select
        thread_id,
        job_pipeline_category as latest_status,
        received_at           as latest_status_at
    from ranked
    qualify row_number() over (
        partition by thread_id
        order by status_rank desc, received_at desc
    ) = 1
),

agg as (
    select
        thread_id,
        min(case when job_pipeline_category = 'applications submitted'
                 then received_at end)                        as applied_at,
        min(received_at)                                      as first_email_at,
        logical_or(job_pipeline_category = 'interview scheduling') as has_interview,
        logical_or(job_pipeline_category = 'offer')                as has_offer,
        logical_or(job_pipeline_category = 'rejection')            as has_rejection
    from ranked
    group by thread_id
),

extract_winners as (
    -- Pick the earliest non-null guess per thread. The first email in a
    -- thread is usually the ATS confirmation, which carries the cleanest
    -- company / role strings.
    select
        thread_id,
        (array_agg(company_guess ignore nulls order by received_at limit 1))[safe_offset(0)] as company,
        (array_agg(role_guess    ignore nulls order by received_at limit 1))[safe_offset(0)] as role
    from {{ ref('int_job_application_extract') }}
    group by thread_id
)

select
    a.thread_id                          as application_id,
    e.company,
    e.role,
    a.applied_at,
    a.first_email_at,
    l.latest_status,
    l.latest_status_at,
    a.has_interview,
    a.has_offer,
    a.has_rejection,
    (
        a.applied_at is not null
        and not a.has_interview
        and not a.has_offer
        and not a.has_rejection
        and timestamp_diff(current_timestamp(), a.applied_at, day)
            >= {{ var('no_response_days', 14) }}
    ) as is_no_response
from agg a
left join latest l           on l.thread_id = a.thread_id
left join extract_winners e  on e.thread_id = a.thread_id
