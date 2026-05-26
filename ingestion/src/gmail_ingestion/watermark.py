"""Determine the start timestamp for the current sync run.

Resolution order (first non-NULL wins):

1. ``MAX(sync_end) WHERE status='ok'`` from ``gmail_data.ingestion_runs``
   — the authoritative watermark; only successful windows advance it.
2. ``MAX(received_at)`` from ``gmail_data.gmail_messages`` — migration
   fallback for deployments that pre-date the run-history table.
3. ``settings.first_run_start_date`` — cold start.

Returned timestamps are floored to whole seconds because Gmail's
``after:`` / ``before:`` operators only accept epoch seconds. Any boundary-
second overlap is absorbed by the ``message_id`` dedup guard in
``load.py``.
"""

from __future__ import annotations

import datetime

from google.api_core import exceptions as gax_exceptions
from google.cloud import bigquery

from .settings import get_settings


def _query_max_ts(bq_client: bigquery.Client, query: str) -> datetime.datetime | None:
    """Run ``query`` and return ``rows[0][0]``, or None if the table is missing/empty."""
    try:
        rows = list(bq_client.query(query).result())
    except gax_exceptions.NotFound:
        return None
    return rows[0][0] if rows else None


def get_sync_start(bq_client: bigquery.Client) -> datetime.datetime:
    """Return the UTC datetime from which this run should fetch messages."""
    s = get_settings()

    runs_max = _query_max_ts(
        bq_client,
        f"SELECT MAX(sync_end) FROM `{s.bq_runs_table_id}` WHERE status = 'ok'",
    )
    if runs_max is not None:
        return runs_max.replace(microsecond=0)

    legacy_max = _query_max_ts(
        bq_client,
        f"SELECT MAX(received_at) FROM `{s.bq_table_id}`",
    )
    if legacy_max is not None:
        return legacy_max.replace(microsecond=0)

    floor = s.first_run_start_date
    return datetime.datetime(floor.year, floor.month, floor.day, tzinfo=datetime.timezone.utc)
