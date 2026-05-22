"""Authentication helpers for Cloud Run (Application Default Credentials).

The gmail-bigquery-ingestor service account has:
  - roles/bigquery.dataEditor + roles/bigquery.jobUser  (via Terraform IAM)
  - Gmail API access via **subject impersonation** using a service account key
    with domain-wide delegation enabled on the Google Workspace / personal
    account side.

How it works on Cloud Run
──────────────────────────
google.auth.default() returns the attached runtime SA identity automatically.
For BigQuery that SA identity is sufficient.

For Gmail the SA must impersonate the target user account (gmail_user_email)
because Gmail does not allow SA-level access without impersonation.
We use google.oauth2.service_account.Credentials with subject= for that.

On Cloud Run the SA key is NOT mounted as a file.  Instead, Workload Identity
/ the metadata server provides ADC.  For Gmail subject impersonation we need
the raw SA key, which we pass in via the GMAIL_SA_KEY_JSON env var (the full
JSON string, stored in Secret Manager and injected at deploy time by Terraform).

Local dev
──────────
Set GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa-key.json and
GMAIL_SA_KEY_JSON=$(cat /path/to/sa-key.json).
"""

from __future__ import annotations

import json
import os

import google.auth
from google.auth import impersonated_credentials
from google.oauth2 import service_account

_GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
_BQ_SCOPES = [
    "https://www.googleapis.com/auth/bigquery",
    "https://www.googleapis.com/auth/cloud-platform",
]


def get_bigquery_credentials():
    """Return ADC credentials scoped for BigQuery.

    On Cloud Run this is the runtime SA via the metadata server.
    Locally it uses GOOGLE_APPLICATION_CREDENTIALS.
    """
    credentials, _ = google.auth.default(scopes=_BQ_SCOPES)
    return credentials


def get_gmail_credentials(gmail_user_email: str):
    """Return credentials that impersonate *gmail_user_email* for Gmail API.

    Requires the SA key JSON to be available as the GMAIL_SA_KEY_JSON env var
    (injected from Secret Manager by Terraform at Cloud Run deploy time).

    The SA must have:
      - The Gmail API enabled in the GCP project.
      - Domain-wide delegation granted in the Google Workspace admin console
        (or the personal Google account owner grants access), with the scope
        https://www.googleapis.com/auth/gmail.readonly whitelisted.
    """
    key_json = os.environ.get("GMAIL_SA_KEY_JSON")
    if not key_json:
        raise EnvironmentError(
            "GMAIL_SA_KEY_JSON env var is not set. "
            "On Cloud Run, mount the SA key from Secret Manager. "
            "Locally, export GMAIL_SA_KEY_JSON=$(cat /path/to/sa-key.json)."
        )

    service_account_info = json.loads(key_json)
    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=_GMAIL_SCOPES,
        subject=gmail_user_email,
    )
    return credentials