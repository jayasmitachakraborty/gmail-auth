"""BigQuery load helpers.

Rows are inserted via the streaming insert API in configurable batches.
A dedup guard filters out message_ids already present in BigQuery so
re-runs are idempotent even if the watermark overlaps slightly.
"""

from __future__ import annotations

from google.cloud import bigquery

from .settings import get_settings

_INSERT_BATCH_SIZE = 500


def _existing_message_ids(
    client: bigquery.Client,
    candidate_ids: list[str],
) -> set[str]:
    """Return the subset of *candidate_ids* already in the BQ table."""
    if not candidate_ids:
        return set()

    s = get_settings()
    # Parameterised query — safe against injection.
    placeholders = ", ".join(f"'{mid}'" for mid in candidate_ids)
    query = f"""
        SELECT message_id
        FROM `{s.bq_table_id}`
        WHERE message_id IN ({placeholders})
    """
    rows = list(client.query(query).result())
    return {row.message_id for row in rows}


def insert_rows(client: bigquery.Client, rows: list[dict]) -> int:
    """Insert *rows* into BigQuery, skipping any already present.

    Returns the number of rows actually inserted.
    """
    if not rows:
        return 0

    s = get_settings()

    # Dedup: check which message_ids already exist
    candidate_ids = [r["message_id"] for r in rows if r.get("message_id")]
    already_present = _existing_message_ids(client, candidate_ids)

    new_rows = [r for r in rows if r.get("message_id") not in already_present]
    if not new_rows:
        print(f"All {len(rows)} rows already in BigQuery — nothing to insert.")
        return 0

    skipped = len(rows) - len(new_rows)
    if skipped:
        print(f"Skipping {skipped} duplicate message(s) already in BigQuery.")

    # Insert in batches to stay within API limits
    inserted_total = 0
    for i in range(0, len(new_rows), _INSERT_BATCH_SIZE):
        batch = new_rows[i : i + _INSERT_BATCH_SIZE]
        errors = client.insert_rows_json(s.bq_table_id, batch)
        if errors:
            raise RuntimeError(f"BigQuery streaming insert errors: {errors}")
        inserted_total += len(batch)
        print(f"Inserted batch of {len(batch)} rows ({inserted_total}/{len(new_rows)} total).")

    return inserted_total