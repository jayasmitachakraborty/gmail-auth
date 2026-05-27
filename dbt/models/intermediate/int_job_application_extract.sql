{{ config(materialized='view', schema='intermediate') }}

-- Per-email regex extraction of (company, role, ats_source).
-- mart_job_applications picks one winner per thread.
-- Reads from the classifier (not the mart) so it can run in parallel
-- with mart_job_pipeline.

with src as (
    select
        thread_id,
        sender,
        subject,
        plain_body,
        received_at
    from {{ ref('int_job_email_classifier') }}
    where thread_id is not null
),

signals as (
    select
        thread_id,
        received_at,
        sender,
        subject,
        regexp_extract(sender, r'@([^>\s]+)')            as sender_domain,
        regexp_extract(sender, r'^\s*"?([^"<]+?)"?\s*<') as sender_display_raw,
        substr(coalesce(plain_body, ''), 1, 1500)        as body_head
    from src
),

cleaned as (
    select
        thread_id,
        received_at,
        subject,
        body_head,
        sender_domain,
        nullif(
            trim(regexp_replace(
                ifnull(sender_display_raw, ''),
                -- Strip generic suffixes: "Stripe Careers" -> "Stripe".
                r'(?i)\s*(careers|recruiting|recruitment|talent(?:\s+acquisition)?|team|jobs|hiring(?:\s+team)?|hr|people(?:\s+team)?|no[\s\-]?reply)\s*$',
                ''
            )),
            ''
        ) as sender_display_clean
    from signals
)

select
    thread_id,
    received_at,

    coalesce(
        case
            when sender_display_clean is not null
             and length(sender_display_clean) between 2 and 60
            then sender_display_clean
        end,
        regexp_extract(
            subject,
            r'(?i)\b(?:to|at|with|joining|join)\s+([A-Z][A-Za-z0-9&\.\-]*(?:\s+[A-Z][A-Za-z0-9&\.\-]*){0,3})'
        ),
        regexp_extract(
            body_head,
            r'(?i)(?:thank you for (?:your interest in|applying to)|application (?:to|for|received at)|interest in (?:joining|working at))\s+([A-Z][A-Za-z0-9&\.\-]*(?:\s+[A-Z][A-Za-z0-9&\.\-]*){0,3})'
        ),
        -- Sender domain, but only when it isn't a known ATS / job board.
        case
            when sender_domain is not null
             and not regexp_contains(
                 sender_domain,
                 r'(?i)(greenhouse\.io|lever\.co|ashbyhq\.com|myworkday\.com|smartrecruiters\.com|icims\.com|bamboohr\.com|recruitee\.com|jobvite\.com|taleo\.net|workable\.com|breezy\.hr|linkedin\.com|indeed\.com|ziprecruiter\.com|wellfound\.com|hire\.com|gmail\.com|googlemail\.com)'
             )
            then initcap(regexp_extract(sender_domain, r'^([A-Za-z0-9\-]+)\.'))
        end
    ) as company_guess,

    coalesce(
        regexp_extract(
            subject,
            r'(?i)applying\s+for(?:\s+the)?\s+([A-Za-z0-9&/\-\s]+?)(?:\s+(?:role|position|at)\b|$)'
        ),
        regexp_extract(
            subject,
            r'(?i)\bfor\s+the\s+([A-Za-z0-9&/\-\s]+?)\s+(?:role|position)\b'
        ),
        regexp_extract(
            subject,
            r'(?i)\b([A-Z][A-Za-z0-9&/\-\s]+?)\s+(?:position|role|opportunity)\s+at\b'
        ),
        regexp_extract(
            body_head,
            r'(?i)applying\s+for(?:\s+the)?\s+([A-Za-z0-9&/\-\s]+?)\s+(?:role|position)\b'
        )
    ) as role_guess,

    case
        when regexp_contains(sender_domain, r'(?i)greenhouse\.io')              then 'Greenhouse'
        when regexp_contains(sender_domain, r'(?i)lever\.co')                   then 'Lever'
        when regexp_contains(sender_domain, r'(?i)ashbyhq\.com')                then 'Ashby'
        when regexp_contains(sender_domain, r'(?i)myworkday\.com|workday\.com') then 'Workday'
        when regexp_contains(sender_domain, r'(?i)smartrecruiters\.com')        then 'SmartRecruiters'
        when regexp_contains(sender_domain, r'(?i)icims\.com')                  then 'iCIMS'
        when regexp_contains(sender_domain, r'(?i)bamboohr\.com')               then 'BambooHR'
        when regexp_contains(sender_domain, r'(?i)recruitee\.com')              then 'Recruitee'
        when regexp_contains(sender_domain, r'(?i)jobvite\.com')                then 'Jobvite'
        when regexp_contains(sender_domain, r'(?i)taleo\.net')                  then 'Taleo'
        when regexp_contains(sender_domain, r'(?i)workable\.com')               then 'Workable'
        when regexp_contains(sender_domain, r'(?i)breezy\.hr')                  then 'Breezy'
        when regexp_contains(sender_domain, r'(?i)linkedin\.com')               then 'LinkedIn'
        when regexp_contains(sender_domain, r'(?i)indeed\.com')                 then 'Indeed'
        when regexp_contains(sender_domain, r'(?i)ziprecruiter\.com')           then 'ZipRecruiter'
        when regexp_contains(sender_domain, r'(?i)wellfound\.com')              then 'Wellfound'
        when regexp_contains(sender_domain, r'(?i)hire\.com')                   then 'Hire'
        when sender_domain is not null                                          then 'Direct'
    end as ats_source_guess
from cleaned
