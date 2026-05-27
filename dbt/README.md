# jobs_pipeline (dbt)

Transforms raw Gmail messages into a job-search analytics mart.

```
gmail_data.gmail_messages          (raw, Cloud Function lands here)
  └── stg_gmail_messages           (view)
       └── int_job_email_finder    (ephemeral) — keeps job-related emails
            └── int_job_email_classifier (ephemeral) — labels each email
                 ├── mart_job_pipeline (table) — email grain
                 └── int_job_application_extract (view) — regex company/role
                      └── mart_job_applications (table) — application grain
                            ▲
                            └── dashboard reads from here
                 mart_dashboard_meta (table) — "last updated" singleton
```

## Models

### Staging

- **`stg_gmail_messages`** — cleans the raw landing table; lowercases
  helper columns; propagates `thread_id` for downstream aggregation.

### Intermediate

- **`int_job_email_finder`** — filters to job-related emails (sender,
  subject, snippet patterns; drops Gmail promotions).
- **`int_job_email_classifier`** — assigns one `job_pipeline_category`
  per email via subject/snippet/body regex.
- **`int_job_application_extract`** — per-email regex extraction of
  `company_guess`, `role_guess`, and `ats_source_guess`. Materialized as
  a view (storing it would be wasteful — the regex is cheap).

### Marts

- **`mart_job_pipeline`** — email-grain table. Partitioned by
  `received_at`, clustered on `(job_pipeline_category, sender)`.
- **`mart_job_applications`** — **application-grain table; the dashboard
  table.** Partitioned by `latest_status_at`, clustered on
  `(outcome, company)`.
- **`mart_dashboard_meta`** — one-row metadata table. Exposes
  `mart_built_at`, `data_through`, `application_count`.

## `mart_job_applications` schema

| Column | Type | Description |
|---|---|---|
| `application_id` | STRING | Gmail `thread_id`. Natural key. |
| `company` | STRING | Best-effort hiring company. Priority: (1) cleaned sender display, (2) `to/at/with <Company>` in subject, (3) `thank you for your interest in <Company>` in body, (4) sender domain when not a known ATS. |
| `role` | STRING | Best-effort role title. Priority: (1) `applying for [the] <Role>` in subject, (2) `for the <Role> role/position` in subject, (3) `<Role> position/role/opportunity at …` in subject, (4) same in body. |
| `ats_source` | STRING | `Greenhouse`, `Lever`, `Ashby`, `Workday`, `SmartRecruiters`, `iCIMS`, `BambooHR`, `Recruitee`, `Jobvite`, `Taleo`, `Workable`, `Breezy`, `LinkedIn`, `Indeed`, `ZipRecruiter`, `Wellfound`, `Hire`, `Direct`. From sender domain of the earliest email. |
| `applied_at` | TIMESTAMP | `received_at` of the earliest `applications submitted` email. `NULL` for recruiter-initiated threads. |
| `first_email_at` | TIMESTAMP | `received_at` of the first email in the thread. |
| `last_email_at` | TIMESTAMP | `received_at` of the most recent email. |
| `email_count` | INT64 | Number of emails in the thread. |
| `latest_status_at` | TIMESTAMP | `received_at` of the highest-priority email (the moment the status last changed). Partition key. |
| `has_interview` | BOOL | Any email categorized as `interview scheduling`. |
| `has_offer` | BOOL | Any email categorized as `offer`. |
| `has_rejection` | BOOL | Any email categorized as `rejection`. |
| `has_recruiter_outreach` | BOOL | Any email categorized as `recruiter outreach`. Drives the Recruiter row in the funnel. |
| `is_no_response` | BOOL | `applied_at IS NOT NULL` AND no interview/offer/rejection AND `current_timestamp() - applied_at >= no_response_days`. |
| `outcome` | STRING | Single canonical bucket per thread; partitions all rows for the donut and filter pills. One of: `offer`, `rejection`, `interview`, `no_response`, `application`, `recruiter`, `other`. |
| `days_in_pipeline` | INT64 | Days from `applied_at` (or `first_email_at`) to `latest_status_at`. Set only for `offer`/`rejection` threads. |

### `outcome` priority

First match wins:

| Rank | Bucket | Condition |
|---:|---|---|
| 1 | `offer` | `has_offer` |
| 2 | `rejection` | `has_rejection` |
| 3 | `interview` | `has_interview` |
| 4 | `no_response` | `is_no_response` |
| 5 | `application` | `applied_at IS NOT NULL` (in flight) |
| 6 | `recruiter` | `has_recruiter_outreach` (no application submitted) |
| 7 | `other` | everything else |

## Variables

| Variable | Default | Purpose |
|---|---:|---|
| `no_response_days` | `14` | Days with no follow-up after `applied_at` before a thread is flagged `is_no_response`. |

Override at run time:

```bash
dbt build --vars '{"no_response_days": 21}'
```

## Dashboard mapping

Every dashboard tile maps to one SQL pattern against `mart_job_applications`:

| Visualization | Query shape |
|---|---|
| Scorecard: Total Applied | `countif(applied_at is not null)` |
| Scorecard: Interviews | `countif(has_interview)` |
| Scorecard: Offers | `countif(has_offer)` |
| Scorecard: No Response | `countif(is_no_response)` |
| Scorecard delta (this month) | `countif(date_trunc(date(applied_at), month) = date_trunc(current_date(), month))` |
| Applications over time | `select date_trunc(date(applied_at), week(monday)) as week, count(*) group by week` |
| Job Funnel — Applied | `countif(applied_at is not null)` |
| Job Funnel — Recruiter | `countif(has_recruiter_outreach)` |
| Job Funnel — Interview | `countif(has_interview)` |
| Job Funnel — Offer | `countif(has_offer)` |
| Outcome Donut | `select outcome, count(*) group by outcome` |
| Company / Role table | `select company, role, ats_source, applied_at, outcome from mart_job_applications` |
| Filter pills | `where outcome in ('interview', 'offer', 'rejection', 'no_response')` |
| Header "Last updated" | `select mart_built_at from mart_dashboard_meta` |

## Tests

Run `dbt build` (or `dbt test` after `dbt run`). Highlights:

- `application_id` unique + not null.
- `outcome` and `job_pipeline_category` match their accepted-values lists.
- `has_*` and `is_no_response` are not null.
- Source freshness on `gmail_data.gmail_messages.ingested_at`
  (warn @ 24h, error @ 72h).
- `dbt_utils.recency` on `stg_gmail_messages.received_at` (72h).

## Future work

- **LLM extraction fallback for `company` / `role`.** Regex coverage will
  plateau around 60–80%. Plan: `int_job_application_llm_extract` reads
  the still-null rows from `int_job_application_extract` and calls
  `ML.GENERATE_TEXT` (Vertex Gemini via BigQuery remote connection),
  incremental on `message_id`. Coalesced after the regex pass.
- **Better `applied_at` for recruiter-led threads.** Today they have
  `applied_at = NULL`; could promote `recruiter outreach` → `applied_at`
  when the user replied (requires distinguishing outbound vs inbound).
