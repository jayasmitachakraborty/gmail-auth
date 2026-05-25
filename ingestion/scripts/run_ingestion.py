"""Gmail → BigQuery incremental sync.

Entry points
────────────
Cloud Function (HTTP, invoked from GitHub Actions or ad-hoc):
    run_pipeline_http(request)   ← entry_point in Terraform

Local / manual:
    python scripts/run_ingestion.py             # incremental
    python scripts/run_ingestion.py --backfill  # ignore watermark
    python scripts/run_ingestion.py --trigger   # POST to deployed function

Environment variables
─────────────────────
Required in production:
  GMAIL_USER_TOKEN_JSON   Gmail user OAuth refresh-token JSON, mounted from
                          Secret Manager. Mint locally with
                          scripts/get_user_token.py.

Local-only alternative:
  GMAIL_USER_TOKEN_PATH   Filesystem path to the same JSON.

Optional (see settings.py for defaults):
  GCP_PROJECT_ID, BQ_DATASET_ID, BQ_TABLE_ID, FIRST_RUN_START_DATE,
  GMAIL_QUERY_EXTRA, MAX_MESSAGES_PER_RUN, INGEST_BATCH_SIZE

For --trigger:
  FUNCTION_URL            HTTPS URL of the deployed Cloud Function.
"""

from __future__ import annotations

import datetime
import gc
import json
import os
import sys

from google.cloud import bigquery
from googleapiclient.discovery import build

from gmail_ingestion.auth import get_bigquery_credentials, get_gmail_credentials
from gmail_ingestion.fetch import get_message, iter_message_ids, transform_message
from gmail_ingestion.load import insert_rows
from gmail_ingestion.settings import get_settings
from gmail_ingestion.watermark import get_sync_start


def run_pipeline(full_backfill: bool = False) -> dict:
    """Execute the Gmail → BigQuery sync. Returns a summary dict."""
    s = get_settings()

    bq_client = bigquery.Client(credentials=get_bigquery_credentials(), project=s.project_id)
    gmail_service = build("gmail", "v1", credentials=get_gmail_credentials())

    if full_backfill:
        floor = s.first_run_start_date
        sync_start = datetime.datetime(
            floor.year, floor.month, floor.day, tzinfo=datetime.timezone.utc
        )
        print(f"FULL_BACKFILL — syncing from {sync_start.date()} (ignoring watermark).")
    else:
        sync_start = get_sync_start(bq_client)
        print(f"Incremental sync — fetching messages after {sync_start.isoformat()}.")

    rows: list[dict] = []
    fetched = 0
    inserted = 0

    for message_id in iter_message_ids(
        gmail_service,
        after=sync_start,
        extra_query=s.gmail_query_extra,
        max_messages=s.max_messages_per_run,
    ):
        message = get_message(gmail_service, message_id)
        rows.append(transform_message(message, max_body_chars=s.max_body_chars))
        # Drop the raw payload as soon as it's flattened — Gmail responses
        # carry base64 attachment bytes that bloat the heap otherwise.
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

    summary = {
        "status": "ok",
        "fetched": fetched,
        "inserted": inserted,
        "sync_start": sync_start.isoformat(),
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
        print(f"ERROR: {exc}", file=sys.stderr)
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
