"""Structured JSON logging that never writes transaction contents.

Only fields explicitly passed through ``extra`` and listed in ``SAFE_FIELDS`` are emitted, so an
accidental ``extra={"description": ...}`` is dropped instead of leaking into Cloud Logging.
"""

import json
import logging
import sys

SAFE_FIELDS = {
    "request_id",
    "method",
    "path",
    "status",
    "duration_ms",
    "user_id",
    "error_type",
    "provider",
    "series_id",
    "attempt",
    "rows",
    "imported",
    "duplicates",
    "rejected",
    "action",
    "tool",
    "count",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"level": record.levelname, "logger": record.name, "message": record.getMessage()}
        for key in SAFE_FIELDS:
            if key in record.__dict__:
                payload[key] = record.__dict__[key]
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception"] = record.exc_info[0].__name__
        return json.dumps(payload, default=str)


def _build() -> logging.Logger:
    log = logging.getLogger("lumiere")
    if not log.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


logger = _build()
