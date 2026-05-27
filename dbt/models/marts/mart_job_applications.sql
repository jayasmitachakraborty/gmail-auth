{{ config(
    materialized='table',
    partition_by={
        'field': 'latest_status_at',
        'data_type': 'timestamp',
        'granularity': 'day'
    },
    cluster_by=['outcome', 'company']
) }}

-- Application-grain mart. One row per Gmail thread. Drives every dashboard
-- visualization (scorecards, funnel, donut, timeline, company/role table).

with msgs as (
    select * from {{ ref('mart_job_pipeline') }}
    where thread_id is not null
),

ranked as (
    select
        thread_id,
        received_at,
        job_pipeline_category,
        case job_pipeline_category
            when 'offer'                  then 6
            when 'rejection'              then 5
            when 'interview scheduling'   then 4
            when 'applications submitted' then 3
            when 'recruiter outreach'     then 2
            else 0
        end as status_rank
    from msgs
),

latest_at as (
    select thread_id, received_at as latest_status_at
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
                 then received_at end)                            as applied_at,
        min(received_at)                                          as first_email_at,
        max(received_at)                                          as last_email_at,
        count(*)                                                  as email_count,
        logical_or(job_pipeline_category = 'interview scheduling') as has_interview,
        logical_or(job_pipeline_category = 'offer')                as has_offer,
        logical_or(job_pipeline_category = 'rejection')            as has_rejection,
        logical_or(job_pipeline_category = 'recruiter outreach')   as has_recruiter_outreach
    from msgs
    group by thread_id
),

-- Pick the earliest non-null extraction per thread. The first email is
-- usually the ATS confirmation, which carries the cleanest signals.
extract_winners as (
    select
        thread_id,
        (array_agg(company_guess    ignore nulls order by received_at limit 1))[safe_offset(0)] as company,
        (array_agg(role_guess       ignore nulls order by received_at limit 1))[safe_offset(0)] as role,
        (array_agg(ats_source_guess ignore nulls order by received_at limit 1))[safe_offset(0)] as ats_source
    from {{ ref('int_job_application_extract') }}
    group by thread_id
),

assembled as (
    select
        a.thread_id as application_id,
        e.company,
        e.role,
        e.ats_source,
        a.applied_at,
        a.first_email_at,
        a.last_email_at,
        a.email_count,
        l.latest_status_at,
        a.has_interview,
        a.has_offer,
        a.has_rejection,
        a.has_recruiter_outreach,
        (
            a.applied_at is not null
            and not a.has_interview
            and not a.has_offer
            and not a.has_rejection
            and timestamp_diff(current_timestamp(), a.applied_at, day)
                >= {{ var('no_response_days', 14) }}
        ) as is_no_response
    from agg a
    left join latest_at       l on l.thread_id = a.thread_id
    left join extract_winners e on e.thread_id = a.thread_id
)

select
    *,

    -- Single canonical bucket per thread. Drives the donut + filter pills.
    case
        when has_offer              then 'offer'
        when has_rejection          then 'rejection'
        when has_interview          then 'interview'
        when is_no_response         then 'no_response'
        when applied_at is not null then 'application'
        when has_recruiter_outreach then 'recruiter'
        else 'other'
    end as outcome,

    -- Days from apply (or first contact) to terminal status. Null while in flight.
    case
        when has_offer or has_rejection
        then timestamp_diff(latest_status_at, coalesce(applied_at, first_email_at), day)
    end as days_in_pipeline
from assembled
