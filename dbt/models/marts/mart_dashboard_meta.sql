{{ config(materialized='table') }}

-- Singleton (one row) table powering the dashboard's "Last updated" stamp.

select
    current_timestamp()                                              as mart_built_at,
    (select max(received_at) from {{ ref('stg_gmail_messages') }})   as data_through,
    (select count(*)         from {{ ref('mart_job_applications') }}) as application_count
