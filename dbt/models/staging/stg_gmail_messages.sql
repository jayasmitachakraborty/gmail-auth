with source as (
    select * from {{ source('gmail_data', 'gmail_messages') }}
),

cleaned as (
    select
        message_id,
        sender,
        subject,
        snippet,
        plain_body,
        labels,
        received_at,
        ingested_at,
        lower(labels) as labels_lower,
        lower(concat(subject, ' ', snippet)) as subject_snippet_lower
    from source
    where message_id is not null
)

select * from cleaned