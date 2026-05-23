"""Authentication helpers for BigQuery and Gmail.

BigQuery uses Application Default Credentials (the Cloud Function runtime SA
in production, ``gcloud auth application-default login`` locally).

Gmail uses a long-lived **user OAuth refresh token** because the target is a
personal ``@gmail.com`` mailbox; consumer Gmail does not support
service-account domain-wide delegation. The refresh-token JSON lives in
Secret Manager (``gmail-user-oauth-token``) and is mounted into the function
as ``GMAIL_USER_TOKEN_JSON``. Mint a new token with
``ingestion/scripts/get_user_token.py`` and upload via
``gcloud secrets versions add``.
"""

from __future__ import annotations

import json
import os

import google.auth
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

_GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
_BQ_SCOPES = [
    "https://www.googleapis.com/auth/bigquery",
    "https://www.googleapis.com/auth/cloud-platform",
]


def get_bigquery_credentials():
    """Return ADC credentials scoped for BigQuery."""
    credentials, _ = google.auth.default(scopes=_BQ_SCOPES)
    return credentials


def get_gmail_credentials() -> Credentials:
    """Return user-OAuth credentials authorised for Gmail readonly.

    Token sources (in order):
      1. ``GMAIL_USER_TOKEN_JSON`` env var (full JSON; from Secret Manager
         in production).
      2. ``GMAIL_USER_TOKEN_PATH`` env var (filesystem path; local dev).
    """
    token_json = os.environ.get("GMAIL_USER_TOKEN_JSON")
    if not token_json:
        token_path = os.environ.get("GMAIL_USER_TOKEN_PATH")
        if token_path and os.path.isfile(token_path):
            with open(token_path, "r", encoding="utf-8") as f:
                token_json = f.read()

    if not token_json:
        raise EnvironmentError(
            "No Gmail user OAuth token found. Set GMAIL_USER_TOKEN_JSON "
            "(production: Secret Manager 'gmail-user-oauth-token') or "
            "GMAIL_USER_TOKEN_PATH (local). Mint one with "
            "ingestion/scripts/get_user_token.py."
        )

    creds = Credentials.from_authorized_user_info(json.loads(token_json), scopes=_GMAIL_SCOPES)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError(
                "Gmail user OAuth credentials are invalid and cannot be "
                "refreshed. Re-mint with scripts/get_user_token.py and "
                "update the 'gmail-user-oauth-token' secret."
            )

    return creds
