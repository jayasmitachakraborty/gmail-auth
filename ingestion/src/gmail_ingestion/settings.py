"""Application settings sourced from environment variables.

On Cloud Run the runtime SA is gmail-bigquery-ingestor@<project>.iam.gserviceaccount.com.
google.auth.default() picks up that identity automatically — no key files needed.

For local development, point GOOGLE_APPLICATION_CREDENTIALS at a downloaded SA key JSON.
"""

from __future__ import annotations

import datetime
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

# On Cloud Run there is no .env file, but keep it for local dev convenience.
_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class AppSettings(BaseSettings):
    """GCP project, BigQuery targets, and pipeline watermark config."""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE) if _ENV_FILE.is_file() else None,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── GCP / BigQuery ────────────────────────────────────────────────────────
    project_id: str = Field(
        default="jobs-and-career-494813",
        validation_alias=AliasChoices("GCP_PROJECT_ID", "PROJECT_ID"),
    )
    dataset_id: str = Field(
        default="gmail_data",
        validation_alias=AliasChoices("BQ_DATASET_ID", "DATASET_ID"),
    )
    table_id: str = Field(
        default="gmail_messages",
        validation_alias=AliasChoices("BQ_TABLE_ID", "TABLE_ID"),
    )

    # ── Gmail SA impersonation ────────────────────────────────────────────────
    # The gmail-bigquery-ingestor SA cannot call Gmail directly; it must
    # impersonate a real Google Workspace / personal user account via
    # domain-wide delegation OR you supply the target email so the SA uses
    # subject impersonation.  Set this to the Gmail address being synced.
    gmail_user_email: str = Field(
        validation_alias=AliasChoices("GMAIL_USER_EMAIL", "GMAIL_USER"),
    )

    # ── Pipeline watermark ────────────────────────────────────────────────────
    # Hard floor for the very first run.  Stored as ISO-8601 date string so it
    # is easy to set in Cloud Run env vars / Terraform locals.
    first_run_start_date: datetime.date = Field(
        default=datetime.date(2026, 3, 1),
        validation_alias=AliasChoices("FIRST_RUN_START_DATE", "START_DATE"),
    )

    # ── Gmail query extras ────────────────────────────────────────────────────
    # Additional Gmail search operators appended to every query, e.g. "in:inbox"
    gmail_query_extra: str = Field(
        default="in:inbox",
        validation_alias=AliasChoices("GMAIL_QUERY_EXTRA", "GMAIL_QUERY"),
    )

    # Maximum messages per run (safety cap; 0 = unlimited)
    max_messages_per_run: int = Field(
        default=0,
        validation_alias=AliasChoices("MAX_MESSAGES_PER_RUN", "MAX_MESSAGES"),
    )

    # ── Computed helpers ──────────────────────────────────────────────────────
    @computed_field
    @property
    def bq_table_id(self) -> str:
        return f"{self.project_id}.{self.dataset_id}.{self.table_id}"


@lru_cache
def get_settings() -> AppSettings:
    return AppSettings()