"""Pipeline settings, sourced from environment variables.

On Cloud Functions the runtime SA (``gmail-bq-ingestor``) is picked up by
``google.auth.default()`` automatically. Locally, point
``GOOGLE_APPLICATION_CREDENTIALS`` at a downloaded SA key.
"""

from __future__ import annotations

import datetime
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class AppSettings(BaseSettings):
    """GCP project, BigQuery targets, and pipeline watermark config."""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE) if _ENV_FILE.is_file() else None,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

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
    runs_table_id: str = Field(
        default="ingestion_runs",
        validation_alias=AliasChoices("BQ_RUNS_TABLE_ID", "RUNS_TABLE_ID"),
    )

    # Watermark floor for the very first run, before any rows exist.
    first_run_start_date: datetime.date = Field(
        default=datetime.date(2026, 3, 1),
        validation_alias=AliasChoices("FIRST_RUN_START_DATE", "START_DATE"),
    )

    # Extra Gmail search operators appended to every query.
    gmail_query_extra: str = Field(
        default="in:inbox",
        validation_alias=AliasChoices("GMAIL_QUERY_EXTRA", "GMAIL_QUERY"),
    )

    # Safety cap on messages per run; 0 = unlimited.
    max_messages_per_run: int = Field(
        default=0,
        validation_alias=AliasChoices("MAX_MESSAGES_PER_RUN", "MAX_MESSAGES"),
    )
    ingest_batch_size: int = Field(
        default=50,
        validation_alias=AliasChoices("INGEST_BATCH_SIZE", "BATCH_SIZE"),
    )
    # Per-message body length cap (chars). 0 disables. Defends against
    # multi-MB HTML emails blowing past the Cloud Function memory ceiling.
    max_body_chars: int = Field(
        default=512_000,
        validation_alias=AliasChoices("MAX_BODY_CHARS",),
    )

    # Day-windowed ingestion. Each window is committed atomically (its
    # ingestion_runs row only lands on success), so a mid-run crash bounds
    # the lost work to the *current* window.
    window_days: int = Field(
        default=1,
        validation_alias=AliasChoices("WINDOW_DAYS",),
    )

    @computed_field
    @property
    def bq_table_id(self) -> str:
        return f"{self.project_id}.{self.dataset_id}.{self.table_id}"

    @computed_field
    @property
    def bq_runs_table_id(self) -> str:
        return f"{self.project_id}.{self.dataset_id}.{self.runs_table_id}"


@lru_cache
def get_settings() -> AppSettings:
    return AppSettings()
