"""Determine the start timestamp for the current sync run.

The watermark is ``MAX(received_at)`` from the BigQuery landing table,
floored to whole seconds (the finest granularity Gmail's ``after:`` filter
supports). Re-fetching the boundary second is intentional: any duplicates
are dropped by the ``message_id`` guard in ``load.py``. Missing rows would
be much more expensive than the small overlap.

If the table is empty (or doesn't exist yet), fall back to
``settings.first_run_start_date``.
"""

from __future__ import annotations

import datetime

from google.api_core import exceptions as gax_exceptions
from google.cloud import bigquery

from .settings import get_settings


def get_sync_start(bq_client: bigquery.Client) -> datetime.datetime:
    """Return the UTC datetime from which this run should fetch messages."""
    s = get_settings()

    query = f"SELECT MAX(received_at) AS max_received_at FROM `{s.bq_table_id}`"

    try:
        rows = list(bq_client.query(query).result())
        max_ts = rows[0].max_received_at if rows else None
    except gax_exceptions.NotFound:
        # First deploy: table not yet created. Any other BQ error must
        # propagate so we don't silently re-backfill on every run.
        max_ts = None

    if max_ts is not None:
        return max_ts.replace(microsecond=0)

    floor = s.first_run_start_date
    return datetime.datetime(floor.year, floor.month, floor.day, tzinfo=datetime.timezone.utc)
