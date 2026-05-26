# jobs_pipeline (dbt)

Transforms raw Gmail messages into a job-search analytics mart.

```
gmail_data.gmail_messages          (raw, Cloud Function lands here)
  └── stg_gmail_messages           (view, gmail_staging)
       ├── int_job_email_finder    (ephemeral) — keeps job-related emails
       ├── int_job_email_classifier (ephemeral) — labels each email with a category
       │     └── mart_job_pipeline (table, gmail_marts) — email grain
       │           └── int_job_application_extract (view) — regex company/role
       │                 └── mart_job_applications (table) — application grain
       │                       ▲
       │                       └── dashboard reads from here
       └── ...
```

## Datasets

| Dataset | Layer | Materialization |
|---|---|---|
| `gmail_data` | raw / source | streaming insert from Cloud Function |
| `gmail_staging` | staging | views |
| `gmail_intermediate` | intermediate | ephemeral (inlined) or view |
| `gmail_marts` | marts | tables |

## Models

### Staging

- **`stg_gmail_messages`** — cleans the raw landing table, lowercases helper
  columns, and propagates `thread_id` for downstream aggregation.

### Intermediate

- **`int_job_email_finder`** — keeps only job-related emails (matches sender
  domains, subjects, snippets; drops Gmail promotions).
- **`int_job_email_classifier`** — assigns a single `job_pipeline_category`
  per email using subject/snippet/body regex.
- **`int_job_application_extract`** — per-email regex extraction of
  `company_guess` and `role_guess` from sender display name, subject, body,
  and (non-ATS) sender domain. Materialized as a view because the regex is
  cheap enough that storing it would be wasteful.

### Marts

- **`mart_job_pipeline`** — email-grain table. One row per job-related
  Gmail message with its category. Partitioned by `received_at`, clustered
  on `(job_pipeline_category, sender)`.
- **`mart_job_applications`** — **application-grain table; this is the
  dashboard table.** One row per Gmail thread. Partitioned by
  `latest_status_at`, clustered on `(latest_status, company)`.

## `mart_job_applications` schema

| Column | Type | Nullable | Description |
|---|---|---|---|
| `application_id` | STRING | no | Gmail `thread_id`. Natural key — one row per thread. |
| `company` | STRING | yes | Best-effort hiring company. Extracted in this priority: (1) cleaned sender display name, (2) `to/at/with <Company>` in subject, (3) `thank you for your interest in <Company>` in body, (4) sender domain when it isn't a known ATS. |
| `role` | STRING | yes | Best-effort role title. Extracted in this priority: (1) `applying for [the] <Role>` in subject, (2) `for the <Role> role/position` in subject, (3) `<Role> position/role/opportunity at …` in subject, (4) same pattern in body. |
| `applied_at` | TIMESTAMP | yes | `received_at` of the earliest `applications submitted` email in the thread. `NULL` when no submission email was identified (e.g. recruiter-initiated threads). |
| `first_email_at` | TIMESTAMP | no | `received_at` of the first email in the thread. Always present. |
| `latest_status` | STRING | no | Highest-ranked category in the thread (see priority below). |
| `latest_status_at` | TIMESTAMP | no | `received_at` of the email that produced `latest_status`. |
| `has_interview` | BOOL | no | Any email in the thread categorized as `interview scheduling`. |
| `has_offer` | BOOL | no | Any email in the thread categorized as `offer`. |
| `has_rejection` | BOOL | no | Any email in the thread categorized as `rejection`. |
| `is_no_response` | BOOL | no | `TRUE` when `applied_at IS NOT NULL` AND no interview/offer/rejection AND `current_timestamp() - applied_at >= no_response_days`. |

### `latest_status` priority

When a thread has multiple categories, the highest-priority category wins.
Within the same priority, the most recent `received_at` wins.

| Rank | Category |
|---:|---|
| 6 | `offer` |
| 5 | `rejection` |
| 4 | `interview scheduling` |
| 3 | `applications submitted` |
| 2 | `recruiter outreach` |
| 1 | `networking` |
| 0 | `other` |

## Variables

| Variable | Default | Purpose |
|---|---:|---|
| `no_response_days` | `14` | Days with no follow-up after `applied_at` before a thread is treated as `is_no_response = TRUE`. |

Override at run time:

```bash
dbt build --vars '{"no_response_days": 21}'
```

## Dashboard mapping

Every visualization in the planned dashboard maps to a single SQL pattern
against `mart_job_applications`:

| Visualization | Query shape |
|---|---|
| Scorecard: Total Apps | `count(*) where applied_at is not null` |
| Scorecard: Interviews | `countif(has_interview)` |
| Scorecard: Offers | `countif(has_offer)` |
| Scorecard: No Response | `countif(is_no_response)` |
| Applications over time | `count(*) group by date_trunc(applied_at, week)` |
| Job Funnel (bar) | `countif(applied_at is not null)`, `countif(has_interview)`, `countif(has_offer)` |
| Outcome Donut | `count(*) group by latest_status` |
| Company / Role table | `select company, role, applied_at, latest_status from mart_job_applications` |

## Tests

Run `dbt build` (or `dbt test` after a `dbt run`). Highlights:

- `application_id` is unique + not null.
- `latest_status` matches the accepted-values list above.
- `has_*` and `is_no_response` are not null.
- Source freshness on `gmail_data.gmail_messages.ingested_at`
  (warn @ 24h, error @ 72h).
- `dbt_utils.recency` on `stg_gmail_messages.received_at` (72h).

## Future work

- **LLM extraction fallback for `company` / `role`.** Regex coverage will
  plateau around 60–80%. The plan is a `int_job_application_llm_extract`
  view that reads the still-null rows from `int_job_application_extract`
  and calls `ML.GENERATE_TEXT` (Vertex Gemini via a BigQuery remote
  connection). It would be incremental on `message_id` so prompt costs are
  bounded. Coalesced into `mart_job_applications` after the regex pass.
- **Better `applied_at` for recruiter-led threads.** Today these have
  `applied_at = NULL`; could promote `recruiter outreach` → `applied_at`
  when the user replied (requires distinguishing outbound vs inbound).
