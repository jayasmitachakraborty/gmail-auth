"""Mint a Gmail user OAuth refresh token for the ingestion pipeline.

Run once locally for the target mailbox; the resulting JSON has the shape
``Credentials.to_json()`` produces and can be uploaded directly as a new
version of the ``gmail-user-oauth-token`` Secret Manager secret.

Setup:
  1. Create an OAuth client (type: Desktop app) in Google Cloud Console.
  2. Download its JSON to ``creds/credentials.json`` (or pass --client-secrets).
  3. Add the target Gmail address as a test user on the consent screen.

Usage:
    python ingestion/scripts/get_user_token.py \\
        [--client-secrets creds/credentials.json] \\
        [--output creds/tokens.json]

Upload to Secret Manager:
    gcloud secrets versions add gmail-user-oauth-token \\
        --data-file=creds/tokens.json
"""

from __future__ import annotations

import argparse
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Mint a Gmail user OAuth refresh token.")
    parser.add_argument(
        "--client-secrets",
        default="creds/credentials.json",
        help="Path to the OAuth client JSON (Desktop type).",
    )
    parser.add_argument(
        "--output",
        default="creds/tokens.json",
        help="Where to write the resulting refresh-token JSON.",
    )
    args = parser.parse_args()

    client_secrets = Path(args.client_secrets)
    if not client_secrets.is_file():
        raise SystemExit(
            f"OAuth client JSON not found at {client_secrets}. Download it "
            "from Google Cloud Console → Credentials and re-run."
        )

    creds = InstalledAppFlow.from_client_secrets_file(
        str(client_secrets), _SCOPES
    ).run_local_server(port=0)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(creds.to_json(), encoding="utf-8")

    print(f"Wrote {output}")
    print(
        f"Upload with:\n  gcloud secrets versions add gmail-user-oauth-token --data-file={output}"
    )


if __name__ == "__main__":
    main()
