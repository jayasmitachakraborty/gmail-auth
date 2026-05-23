"""Cloud Functions v2 entry-point shim.

Cloud Functions expects ``main.py`` at the source-zip root with the entry
point importable from it. The real code lives in ``src/gmail_ingestion/``
and ``scripts/run_ingestion.py``; this shim wires both onto sys.path and
re-exports the HTTP handler.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
for _sub in ("src", "scripts"):
    _path = str(_ROOT / _sub)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from run_ingestion import run_pipeline_http  # noqa: E402

__all__ = ["run_pipeline_http"]
