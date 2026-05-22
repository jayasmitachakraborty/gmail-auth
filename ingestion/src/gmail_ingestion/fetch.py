"""Gmail API fetch helpers.

All functions accept an already-built Gmail service object so auth is
handled once at the call site (run_pipeline.py) and not buried here.
"""

from __future__ import annotations

import base64
import datetime
from typing import Iterator

from googleapiclient.discovery import Resource


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _decode_base64url(data: str) -> str:
    if not data:
        return ""
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")


def _headers_to_dict(headers: list[dict]) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in headers}


def _extract_bodies(payload: dict) -> dict[str, str]:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    def _walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if mime == "text/plain" and data:
            plain_parts.append(_decode_base64url(data))
        if mime == "text/html" and data:
            html_parts.append(_decode_base64url(data))
        for child in part.get("parts", []):
            _walk(child)

    _walk(payload)
    return {
        "plain_body": "\n".join(plain_parts),
        "html_body": "\n".join(html_parts),
    }


# ── Public API ────────────────────────────────────────────────────────────────

def iter_message_ids(
    gmail_service: Resource,
    after: datetime.datetime,
    extra_query: str = "in:inbox",
    max_messages: int = 0,
) -> Iterator[str]:
    """Yield Gmail message IDs newer than *after* (full pagination).

    Parameters
    ----------
    gmail_service:
        Authenticated Gmail API resource.
    after:
        Only messages with internalDate >= this timestamp are returned.
        Passed to Gmail as `after:<unix_epoch>`.
    extra_query:
        Additional Gmail search operators (default: ``in:inbox``).
    max_messages:
        Hard cap on total messages yielded.  0 = no cap.
    """
    epoch_secs = int(after.timestamp())
    query = f"after:{epoch_secs} {extra_query}".strip()

    page_token: str | None = None
    yielded = 0

    while True:
        kwargs: dict = dict(
            userId="me",
            q=query,
            maxResults=500,  # API max per page
        )
        if page_token:
            kwargs["pageToken"] = page_token

        response = gmail_service.users().messages().list(**kwargs).execute()

        for msg in response.get("messages", []):
            yield msg["id"]
            yielded += 1
            if max_messages and yielded >= max_messages:
                return

        page_token = response.get("nextPageToken")
        if not page_token:
            break


def get_message(gmail_service: Resource, message_id: str) -> dict:
    """Fetch a single full message."""
    return (
        gmail_service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )


def transform_message(message: dict) -> dict:
    """Convert a raw Gmail API message dict into a flat BigQuery row dict."""
    payload = message.get("payload", {})
    headers = _headers_to_dict(payload.get("headers", []))
    bodies = _extract_bodies(payload)

    # internalDate is milliseconds since epoch (string)
    internal_date_ms = message.get("internalDate")
    received_at: str | None = None
    if internal_date_ms:
        ts = datetime.datetime.fromtimestamp(
            int(internal_date_ms) / 1000,
            tz=datetime.timezone.utc,
        )
        received_at = ts.strftime("%Y-%m-%d %H:%M:%S.%f+00:00")

    return {
        "message_id": message.get("id"),
        "thread_id": message.get("threadId"),
        "history_id": message.get("historyId"),
        "subject": headers.get("subject"),
        "sender": headers.get("from"),
        "recipient": headers.get("to"),
        "date": headers.get("date"),
        "snippet": message.get("snippet"),
        "plain_body": bodies["plain_body"],
        "html_body": bodies["html_body"],
        "labels": ",".join(message.get("labelIds", [])),
        "received_at": received_at,
        "ingested_at": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S.%f+00:00"
        ),
    }