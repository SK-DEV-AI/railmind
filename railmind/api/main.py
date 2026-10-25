"""ASGI entry point. `uvicorn railmind.api.main:app --port 8000`.

The app is built at import so a misconfiguration fails at startup rather than at
a user's first request. A missing API key is a deployment error: refusing to
boot is the loud, honest version of it.
"""

from __future__ import annotations

import os
from pathlib import Path

from railmind.api.app import create_app
from railmind.engine.metrics import RequestLog
from railmind.wiring import build_registry


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env reader, identical in behaviour to the one in scripts/."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def build_app():
    load_dotenv()
    registry = build_registry()
    log = RequestLog()
    return create_app(registry=registry, log=log)


app = build_app()
