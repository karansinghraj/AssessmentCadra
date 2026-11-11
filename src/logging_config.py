"""JSON logging for the API layer.

Every access log and lifecycle event is one JSON object per line, which is what
host log collectors expect. Email bodies and addresses are never logged; only
ids, states, outcomes, latency, tokens and cost.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

# Attributes present on every LogRecord. Everything else was passed via `extra`.
_RESERVED = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
    "message",
    "asctime",
    "color_message",
}

_API_LOGGERS = ("larkfield.api", "uvicorn", "uvicorn.error", "uvicorn.access")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str | None = None) -> None:
    """Attach one JSON handler to the API and uvicorn loggers.

    Only named loggers are touched, so pytest's capture handler and the root
    logger keep working. `propagate=False` prevents duplicate text lines.
    """
    resolved = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    numeric = getattr(logging, resolved, logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    for name in _API_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.addHandler(handler)
        # The middleware owns request access logs; suppress uvicorn's duplicate line.
        logger.setLevel(logging.WARNING if name == "uvicorn.access" else numeric)
        logger.propagate = False
