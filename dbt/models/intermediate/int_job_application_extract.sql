{{ config(materialized='view', schema='gmail_intermediate') }}

-- Per-email extraction of (company, role) using cheap deterministic regex.
-- The downstream mart picks one winner per thread. Null is acceptable: the
-- LLM fallback (future work) will fill the long tail.

with src as (
    select
        message_id,
        thread_id,
        sender,
        subject,
        plain_body,
        received_at,
        job_pipeline_category
    from {{ ref('mart_job_pipeline') }}
    where thread_id is not null
),

with_signals as (
    select
        *,
        regexp_extract(sender, r'@([^>\s]+)')              as sender_domain,
        regexp_extract(sender, r'^\s*"?([^"<]+?)"?\s*<')   as sender_display_raw,
        substr(coalesce(plain_body, ''), 1, 1500)          as body_head
    from src
),

cleaned as (
    select
        message_id,
        thread_id,
        sender,
        subject,
        body_head,
        received_at,
        job_pipeline_category,
        sender_domain,
        nullif(
            trim(regexp_replace(
                ifnull(sender_display_raw, ''),
                -- strip generic suffixes like "Stripe Careers" -> "Stripe"
                r'(?i)\s*(careers|recruiting|recruitment|talent(?:\s+acquisition)?|team|jobs|hiring(?:\s+team)?|hr|people(?:\s+team)?|no[\s\-]?reply)\s*$',
                ''
            )),
            ''
        ) as sender_display_clean
    from with_signals
)

select
    message_id,
    thread_id,
    received_at,
    job_pipeline_category,

    coalesce(
        -- 1. cleaned display name (most reliable for ATS senders)
        case
            when sender_display_clean is not null
             and length(sender_display_clean) between 2 and 60
            then sender_display_clean
        end,

        -- 2. subject patterns: "...to/at/with/joining <Company>"
        regexp_extract(
            subject,
            r'(?i)\b(?:to|at|with|joining|join)\s+([A-Z][A-Za-z0-9&\.\-]*(?:\s+[A-Z][A-Za-z0-9&\.\-]*){0,3})'
        ),

        -- 3. body patterns: "thank you for your interest in <Company>", etc.
        regexp_extract(
            body_head,
            r'(?i)(?:thank you for (?:your interest in|applying to)|application (?:to|for|received at)|interest in (?:joining|working at))\s+([A-Z][A-Za-z0-9&\.\-]*(?:\s+[A-Z][A-Za-z0-9&\.\-]*){0,3})'
        ),

        -- 4. sender domain, but only when it isn't a known ATS / job board
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
        -- 1. "applying for [the] <Role>"
        regexp_extract(
            subject,
            r'(?i)applying\s+for(?:\s+the)?\s+([A-Za-z0-9&/\-\s]+?)(?:\s+(?:role|position|at)\b|$)'
        ),

        -- 2. "for the <Role> role/position"
        regexp_extract(
            subject,
            r'(?i)\bfor\s+the\s+([A-Za-z0-9&/\-\s]+?)\s+(?:role|position)\b'
        ),

        -- 3. "<Role> position/role/opportunity at ..."
        regexp_extract(
            subject,
            r'(?i)\b([A-Z][A-Za-z0-9&/\-\s]+?)\s+(?:position|role|opportunity)\s+at\b'
        ),

        -- 4. body fallback
        regexp_extract(
            body_head,
            r'(?i)applying\s+for(?:\s+the)?\s+([A-Za-z0-9&/\-\s]+?)\s+(?:role|position)\b'
        )
    ) as role_guess
from cleaned
