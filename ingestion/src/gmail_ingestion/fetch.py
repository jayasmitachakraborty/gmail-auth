"""Gmail API fetch and transform helpers.

All public functions take an already-built Gmail service object so auth
happens once at the call site (see ``run_ingestion.py``).
"""

from __future__ import annotations

import base64
import datetime
from typing import Iterator

from googleapiclient.discovery import Resource


def _decode_base64url(data: str) -> str:
    if not data:
        return ""
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")


def _headers_to_dict(headers: list[dict]) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in headers}


def _extract_bodies(payload: dict) -> dict[str, str]:
    """Walk the MIME tree, decode only text/plain + text/html *body* parts.

    Skips attachment-like parts (anything with a ``filename`` or
    ``body.attachmentId``) — those carry base64 bytes we don't want and
    were a primary source of memory pressure.
    """
    plain_parts: list[str] = []
    html_parts: list[str] = []

    def _walk(part: dict) -> None:
        body = part.get("body", {})
        filename = part.get("filename")
        if filename or body.get("attachmentId"):
            return

        mime = part.get("mimeType", "")
        data = body.get("data")
        if mime == "text/plain" and data:
            plain_parts.append(_decode_base64url(data))
        elif mime == "text/html" and data:
            html_parts.append(_decode_base64url(data))

        for child in part.get("parts", []):
            _walk(child)

    _walk(payload)
    return {
        "plain_body": "\n".join(plain_parts),
        "html_body": "\n".join(html_parts),
    }


def iter_message_ids(
    gmail_service: Resource,
    after: datetime.datetime,
    before: datetime.datetime | None = None,
    extra_query: str = "in:inbox",
    max_messages: int = 0,
) -> Iterator[str]:
    """Yield Gmail message IDs in the half-open window ``[after, before)``.

    Both bounds are floored/ceilinged to whole seconds because Gmail's
    ``after:`` / ``before:`` operators only accept epoch seconds.
    ``before=None`` means "up to now". ``max_messages=0`` means no cap.
    """
    parts = [f"after:{int(after.timestamp())}"]
    if before is not None:
        parts.append(f"before:{int(before.timestamp())}")
    if extra_query:
        parts.append(extra_query)
    query = " ".join(parts).strip()

    page_token: str | None = None
    yielded = 0
    while True:
        kwargs: dict = {"userId": "me", "q": query, "maxResults": 500}
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
            return


def get_message(gmail_service: Resource, message_id: str) -> dict:
    """Fetch a single full message."""
    return (
        gmail_service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )


def _truncate(s: str, limit: int) -> str:
    if limit and s and len(s) > limit:
        return s[:limit]
    return s


def transform_message(message: dict, max_body_chars: int = 0) -> dict:
    """Convert a raw Gmail API message dict into a flat BigQuery row.

    ``max_body_chars`` truncates ``plain_body`` / ``html_body`` to defend
    against multi-MB emails breaching the function's memory ceiling and
    BigQuery's per-row size limit. ``0`` disables truncation.
    """
    payload = message.get("payload", {})
    headers = _headers_to_dict(payload.get("headers", []))
    bodies = _extract_bodies(payload)

    received_at: str | None = None
    internal_date_ms = message.get("internalDate")
    if internal_date_ms:
        ts = datetime.datetime.fromtimestamp(
            int(internal_date_ms) / 1000, tz=datetime.timezone.utc
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
        "plain_body": _truncate(bodies["plain_body"], max_body_chars),
        "html_body": _truncate(bodies["html_body"], max_body_chars),
        "labels": ",".join(message.get("labelIds", [])),
        "received_at": received_at,
        "ingested_at": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S.%f+00:00"
        ),
    }
