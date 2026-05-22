"""Gmail → BigQuery incremental sync.

Entry points
────────────
Cloud Function (HTTP trigger, called by Cloud Scheduler):
    run_pipeline_http(request)   ← set as entry_point in Terraform

Local / manual scheduler trigger:
    python scripts/run_pipeline.py               # run ingestion directly
    python scripts/run_pipeline.py --trigger     # POST to the Cloud Function URL
    python scripts/run_pipeline.py --backfill    # full backfill (ignores watermark)

Environment variables
─────────────────────
Required:
  GMAIL_USER_EMAIL        Gmail address the ingestor SA will impersonate
  GMAIL_SA_KEY_JSON       Full SA key JSON (injected from Secret Manager on GCF;
                          set manually for local dev)

Optional:
  GCP_PROJECT_ID          Default: jobs-and-career-494813
  BQ_DATASET_ID           Default: gmail_data
  BQ_TABLE_ID             Default: gmail_messages
  FIRST_RUN_START_DATE    Default: 2026-03-01
  GMAIL_QUERY_EXTRA       Default: in:inbox
  MAX_MESSAGES_PER_RUN    Default: 0 (unlimited)

For --trigger (manual Cloud Scheduler invoke):
  FUNCTION_URL            HTTPS URL of the deployed Cloud Function
                          (printed by `terraform output cloud_function_url`)
"""

from __future__ import annotations

import datetime
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


# ── Core pipeline ─────────────────────────────────────────────────────────────

def run_pipeline(full_backfill: bool = False) -> dict:
    """Execute the Gmail → BigQuery sync.  Returns a summary dict."""
    s = get_settings()

    bq_client = bigquery.Client(
        credentials=get_bigquery_credentials(),
        project=s.project_id,
    )
    gmail_creds = get_gmail_credentials(s.gmail_user_email)
    gmail_service = build("gmail", "v1", credentials=gmail_creds)

    # Determine sync window
    if full_backfill:
        floor = s.first_run_start_date
        sync_start = datetime.datetime(
            floor.year, floor.month, floor.day,
            tzinfo=datetime.timezone.utc,
        )
        print(f"FULL_BACKFILL — syncing from {sync_start.date()} (ignoring watermark).")
    else:
        sync_start = get_sync_start(bq_client)
        print(f"Incremental sync — fetching messages after {sync_start.isoformat()}.")

    # Fetch, transform, and stream-insert in rolling batches
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
        rows.append(transform_message(message))
        fetched += 1

        if len(rows) >= 500:
            inserted += insert_rows(bq_client, rows)
            rows = []

    if rows:
        inserted += insert_rows(bq_client, rows)

    summary = {
        "status": "ok",
        "fetched": fetched,
        "inserted": inserted,
        "sync_start": sync_start.isoformat(),
    }
    print(f"Run complete: {summary}")
    return summary


# ── Cloud Function HTTP entry point ───────────────────────────────────────────

def run_pipeline_http(request) -> tuple[str, int]:
    """HTTP handler — invoked by Cloud Scheduler via authenticated POST.

    functions-framework passes a Werkzeug Request object; we only need
    request.get_json() from it so there is no Flask import required.
    The framework itself (already a dependency via functions-framework) bundles
    Werkzeug and handles all HTTP plumbing — this function just returns a
    (body_str, status_code) tuple which the framework serialises for us.

    Cloud Scheduler sends POST {"source": "cloud-scheduler"}.
    Manual callers can POST {"full_backfill": true} to force a full re-sync.
    Returns 200 on success, 500 on error (Scheduler retries on 5xx per retry_config).
    """
    try:
        body = request.get_json(silent=True) or {}
        full_backfill = str(body.get("full_backfill", "")).lower() == "true"
        summary = run_pipeline(full_backfill=full_backfill)
        return json.dumps(summary), 200
    except Exception as exc:
        error_body = {"status": "error", "message": str(exc)}
        print(f"ERROR: {exc}", file=sys.stderr)
        return json.dumps(error_body), 500


# ── CLI — local run or manual scheduler trigger ───────────────────────────────

def _trigger_scheduler_job() -> None:
    """POST to the deployed Cloud Function URL using ADC credentials.

    Use this to manually kick off a run without waiting for the cron schedule,
    e.g. after a deploy or to test in production:

        python scripts/run_pipeline.py --trigger
    """
    import google.auth
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

    import urllib.request
    import json

    # Obtain an OIDC token for the function URL audience
    auth_req = google.auth.transport.requests.Request()
    token = id_token.fetch_id_token(auth_req, function_url)

    payload = json.dumps({"source": "manual-trigger"}).encode()
    req = urllib.request.Request(
        function_url,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read().decode()
        print(f"HTTP {resp.status}: {body}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gmail → BigQuery pipeline runner")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--trigger",
        action="store_true",
        help="POST to the deployed Cloud Function (set FUNCTION_URL env var)",
    )
    group.add_argument(
        "--backfill",
        action="store_true",
        help="Run locally with full_backfill=True (ignores watermark)",
    )
    args = parser.parse_args()

    if args.trigger:
        _trigger_scheduler_job()
    else:
        try:
            run_pipeline(full_backfill=args.backfill)
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)