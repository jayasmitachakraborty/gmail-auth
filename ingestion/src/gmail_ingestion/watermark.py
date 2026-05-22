"""Watermark helpers: determine the start date for the current sync run.

Strategy
────────
1. Query BigQuery for the maximum `received_at` already in the table.
2. If the table is empty (or has no valid timestamp), fall back to
   settings.first_run_start_date (default: 2026-03-01).
3. Return a timezone-aware UTC datetime so Gmail `after:` queries and
   BigQuery comparisons are unambiguous.

Granularity caveat
──────────────────
The Gmail `after:` operator only accepts whole Unix epoch *seconds*, but
BigQuery stores `received_at` with microsecond precision.  If we advanced
the watermark past `max_received_at` (e.g. by adding 1 second), we would
silently skip any message that arrived later within the same wall-clock
second as the current max — because Gmail's second-resolution `after:`
filter cannot distinguish sub-second timestamps.

To prevent that, we floor the watermark to the start of its second.  This
deliberately produces a small overlap on every incremental run: Gmail
re-lists the messages from the boundary second, and the `message_id`
dedup guard in load.py drops the ones that are already in BigQuery.
Overlap is cheap; missing rows is not.
"""

from __future__ import annotations

import datetime

from google.api_core import exceptions as gax_exceptions
from google.cloud import bigquery

from .settings import get_settings


def get_sync_start(bq_client: bigquery.Client) -> datetime.datetime:
    """Return a UTC datetime from which this run should fetch messages.

    If no rows exist yet, returns midnight UTC on first_run_start_date.
    Otherwise returns ``max(received_at)`` floored to whole seconds — the
    finest granularity Gmail's ``after:`` operator supports.  Re-fetching
    the boundary second is intentional; dedup in load.py removes the
    duplicates.
    """
    s = get_settings()

    query = f"""
        SELECT MAX(received_at) AS max_received_at
        FROM `{s.bq_table_id}`
    """

    try:
        rows = list(bq_client.query(query).result())
    except gax_exceptions.NotFound:
        # First deploy: table/dataset hasn't been created by Terraform yet.
        # Any other BigQuery error (PermissionDenied, BadRequest, schema
        # mismatch, transient 5xx, …) must propagate — silently falling
        # back to first_run_start_date would trigger a full re-backfill
        # on every scheduled run and mask the underlying failure.
        max_ts = None
    else:
        max_ts = rows[0].max_received_at if rows else None

    if max_ts is not None:
        # max_ts is timezone-aware from the BQ client. Floor to the second so
        # Gmail's second-resolution `after:` filter still includes any message
        # whose sub-second timestamp is later than max_ts but within the same
        # second. Duplicates are filtered downstream by message_id.
        return max_ts.replace(microsecond=0)

    # First run: fall back to configured floor date.
    floor = s.first_run_start_date
    return datetime.datetime(
        floor.year, floor.month, floor.day,
        tzinfo=datetime.timezone.utc,
    )