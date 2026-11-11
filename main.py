"""Vercel serverless entrypoint.

Vercel looks for a top-level FastAPI instance named `app` in `main.py` (or
`app.py`, `index.py`, `server.py`, `wsgi.py`, `asgi.py`). The actual application
lives in `src/app.py`; importing it here keeps local uvicorn usage unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.app import app  # noqa: E402

__all__ = ["app"]
