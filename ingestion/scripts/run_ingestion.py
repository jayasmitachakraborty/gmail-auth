"""Gmail → BigQuery incremental sync.

Entry points
────────────
Cloud Function (HTTP, invoked from GitHub Actions or ad-hoc):
    run_pipeline_http(request)   ← entry_point in Terraform

Local / manual:
    python scripts/run_ingestion.py             # incremental
    python scripts/run_ingestion.py --backfill  # ignore watermark
    python scripts/run_ingestion.py --trigger   # POST to deployed function

Crash safety
────────────
Each invocation processes the open range ``[watermark, now)`` as a
sequence of day-aligned windows in *ascending* order. After each
window's BigQuery inserts commit, one immutable row is appended to
``gmail_data.ingestion_runs`` with ``status='ok'`` — and *only then*
does the watermark advance. If the function OOMs or crashes mid-window,
no run row is written and the next invocation retries the same window
from the watermark; older completed windows are never re-processed and
never silently lost.

Environment variables
─────────────────────
Required in production:
  GMAIL_USER_TOKEN_JSON   Gmail user OAuth refresh-token JSON, mounted from
                          Secret Manager. Mint locally with
                          scripts/get_user_token.py.

Local-only alternative:
  GMAIL_USER_TOKEN_PATH   Filesystem path to the same JSON.

Optional (see settings.py for defaults):
  GCP_PROJECT_ID, BQ_DATASET_ID, BQ_TABLE_ID, BQ_RUNS_TABLE_ID,
  FIRST_RUN_START_DATE, GMAIL_QUERY_EXTRA, MAX_MESSAGES_PER_RUN,
  INGEST_BATCH_SIZE, MAX_BODY_CHARS, WINDOW_DAYS

For --trigger:
  FUNCTION_URL            HTTPS URL of the deployed Cloud Function.
"""

from __future__ import annotations

import datetime
import gc
import json
import os
import sys
import traceback
from typing import Iterator

from google.cloud import bigquery
from googleapiclient.discovery import build

from gmail_ingestion.auth import get_bigquery_credentials, get_gmail_credentials
from gmail_ingestion.fetch import get_message, iter_message_ids, transform_message
from gmail_ingestion.load import insert_rows
from gmail_ingestion.runs import new_run_id, record_window
from gmail_ingestion.settings import get_settings
from gmail_ingestion.watermark import get_sync_start


def _floor_to_day(ts: datetime.datetime) -> datetime.datetime:
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)


def _iter_windows(
    floor: datetime.datetime,
    ceiling: datetime.datetime,
    window_days: int,
) -> Iterator[tuple[datetime.datetime, datetime.datetime]]:
    """Yield ascending half-open ``[start, end)`` UTC windows.

    Boundaries align to UTC day starts; the first window may begin mid-day
    (at ``floor``) and the last is clipped to ``ceiling``.
    """
    if floor >= ceiling:
        return
    step = datetime.timedelta(days=max(1, window_days))
    start = floor
    next_boundary = _floor_to_day(floor) + step
    while start < ceiling:
        end = min(next_boundary, ceiling)
        yield (start, end)
        start = end
        next_boundary += step


def _process_window(
    bq_client: bigquery.Client,
    gmail_service,
    window_start: datetime.datetime,
    window_end: datetime.datetime,
    s,
) -> tuple[int, int]:
    """Fetch + load one window. Returns ``(fetched, inserted)``."""
    rows: list[dict] = []
    fetched = 0
    inserted = 0

    for message_id in iter_message_ids(
        gmail_service,
        after=window_start,
        before=window_end,
        extra_query=s.gmail_query_extra,
        max_messages=s.max_messages_per_run,
    ):
        message = get_message(gmail_service, message_id)
        rows.append(transform_message(message, max_body_chars=s.max_body_chars))
        del message
        fetched += 1

        if len(rows) >= s.ingest_batch_size:
            inserted += insert_rows(bq_client, rows)
            rows.clear()
            gc.collect()

    if rows:
        inserted += insert_rows(bq_client, rows)
        rows.clear()
        gc.collect()

    return fetched, inserted


def run_pipeline(full_backfill: bool = False) -> dict:
    """Execute the Gmail → BigQuery sync. Returns a summary dict.

    Iterates day-aligned windows oldest-first; commits each window's
    ``ingestion_runs`` row only on success so the watermark cannot
    advance past un-ingested messages.
    """
    s = get_settings()

    bq_client = bigquery.Client(credentials=get_bigquery_credentials(), project=s.project_id)
    gmail_service = build("gmail", "v1", credentials=get_gmail_credentials())

    if full_backfill:
        floor = s.first_run_start_date
        sync_floor = datetime.datetime(
            floor.year, floor.month, floor.day, tzinfo=datetime.timezone.utc
        )
        print(f"FULL_BACKFILL — syncing from {sync_floor.date()} (ignoring watermark).")
    else:
        sync_floor = get_sync_start(bq_client)
        print(f"Incremental sync — fetching messages after {sync_floor.isoformat()}.")

    ceiling = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    run_id = new_run_id()
    windows = list(_iter_windows(sync_floor, ceiling, s.window_days))

    print(
        f"Run {run_id}: {len(windows)} window(s) of <= {s.window_days} day(s) "
        f"from {sync_floor.isoformat()} to {ceiling.isoformat()}."
    )

    total_fetched = 0
    total_inserted = 0
    windows_completed = 0

    for window_start, window_end in windows:
        started_at = datetime.datetime.now(datetime.timezone.utc)
        print(f"  → Window [{window_start.isoformat()}, {window_end.isoformat()})")
        try:
            fetched, inserted = _process_window(
                bq_client, gmail_service, window_start, window_end, s
            )
        except Exception as exc:
            # Record the failure for observability, then abort so we don't
            # leap-frog the gap into newer windows.
            err = f"{type(exc).__name__}: {exc}"
            print(f"    ✗ Window failed: {err}", file=sys.stderr)
            traceback.print_exc()
            try:
                record_window(
                    bq_client,
                    run_id=run_id,
                    started_at=started_at,
                    sync_start=window_start,
                    sync_end=window_end,
                    status="error",
                    fetched=0,
                    inserted=0,
                    error=err,
                )
            except Exception as record_exc:  # noqa: BLE001
                print(f"    ✗ Also failed to record error row: {record_exc}", file=sys.stderr)
            raise

        record_window(
            bq_client,
            run_id=run_id,
            started_at=started_at,
            sync_start=window_start,
            sync_end=window_end,
            status="ok",
            fetched=fetched,
            inserted=inserted,
        )
        total_fetched += fetched
        total_inserted += inserted
        windows_completed += 1
        print(f"    ✓ fetched={fetched} inserted={inserted}")

    summary = {
        "status": "ok",
        "run_id": run_id,
        "fetched": total_fetched,
        "inserted": total_inserted,
        "windows_completed": windows_completed,
        "sync_start": sync_floor.isoformat(),
        "sync_end": ceiling.isoformat(),
    }
    print(f"Run complete: {summary}")
    return summary


def run_pipeline_http(request) -> tuple[str, int]:
    """HTTP handler invoked by Cloud Functions over authenticated POST.

    POST ``{"full_backfill": true}`` to force a full re-sync. Returns 200 on
    success and 500 on error.
    """
    try:
        body = request.get_json(silent=True) or {}
        full_backfill = str(body.get("full_backfill", "")).lower() == "true"
        return json.dumps(run_pipeline(full_backfill=full_backfill)), 200
    except Exception as exc:
        traceback.print_exc()
        return json.dumps({"status": "error", "message": str(exc)}), 500


def _trigger_cloud_function() -> None:
    """POST to the deployed Cloud Function URL using ADC credentials."""
    import urllib.request

    import google.auth.transport.requests
    from google.oauth2 import id_token

    function_url = os.environ.get("FUNCTION_URL")
    if not function_url:
        print(
            "ERROR: Set FUNCTION_URL to the Cloud Function HTTPS URL.\n"
            "       Get it with: terraform output cloud_function_url",
            file=sys.stderr,
        )
        sys.exit(1)

    token = id_token.fetch_id_token(google.auth.transport.requests.Request(), function_url)
    req = urllib.request.Request(
        function_url,
        data=json.dumps({"source": "manual-cli"}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        print(f"HTTP {resp.status}: {resp.read().decode()}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gmail → BigQuery pipeline runner")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--trigger",
        action="store_true",
        help="POST to the deployed Cloud Function (set FUNCTION_URL)",
    )
    group.add_argument(
        "--backfill",
        action="store_true",
        help="Run locally with full_backfill=True (ignores watermark)",
    )
    args = parser.parse_args()

    if args.trigger:
        _trigger_cloud_function()
    else:
        try:
            run_pipeline(full_backfill=args.backfill)
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
