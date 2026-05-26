"""Run-history bookkeeping for the Gmail → BigQuery pipeline.

One immutable row per completed (or captured-failure) ingestion window
is appended to ``gmail_data.ingestion_runs``. The watermark is derived
from this table — see ``watermark.py``.

Append-only is deliberate: BigQuery streaming inserts cannot be UPDATE'd
while in the streaming buffer (~90 min), so the simplest crash-safe
pattern is to emit one row per *outcome* and never mutate it.
"""

from __future__ import annotations

import datetime
import uuid

from google.cloud import bigquery

from .settings import get_settings


def new_run_id() -> str:
    """Unique identifier for one Cloud Function invocation."""
    return uuid.uuid4().hex


def _ts(t: datetime.datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S.%f+00:00")


def record_window(
    client: bigquery.Client,
    *,
    run_id: str,
    started_at: datetime.datetime,
    sync_start: datetime.datetime,
    sync_end: datetime.datetime,
    status: str,
    fetched: int,
    inserted: int,
    error: str | None = None,
) -> None:
    """Append one ``ingestion_runs`` row. ``status`` is ``'ok'`` or ``'error'``."""
    if status not in ("ok", "error"):
        raise ValueError(f"status must be 'ok' or 'error', got {status!r}")

    s = get_settings()
    row = {
        "run_id": run_id,
        "started_at": _ts(started_at),
        "finished_at": _ts(datetime.datetime.now(datetime.timezone.utc)),
        "status": status,
        "sync_start": _ts(sync_start),
        "sync_end": _ts(sync_end),
        "fetched": fetched,
        "inserted": inserted,
        "error": error,
    }
    errors = client.insert_rows_json(s.bq_runs_table_id, [row])
    if errors:
        raise RuntimeError(f"Failed to write ingestion_runs row: {errors}")
