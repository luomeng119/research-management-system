from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

SENSITIVE_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "session",
    "token",
    "cookie",
    "api_key",
    "apikey",
    "id_card",
    "bank_card",
    "phone",
    "contact",
    "content",
    "body",
    "absolute_path",
    "file_path",
)
LOG_FIELDS = (
    "request_id",
    "user_id",
    "app_module",
    "action",
    "object_id",
    "duration_ms",
    "result",
    "error_code",
)


def is_sensitive_key(key: str) -> bool:
    lowered = key.casefold()
    return any(fragment in lowered for fragment in SENSITIVE_FRAGMENTS)


def safe_log_value(value):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    rendered = str(value)
    lowered = rendered.casefold().strip()
    if (
        len(rendered) > 256
        or lowered.startswith(("/", "\\"))
        or re.search(r"(?:^|\s)/(?:[^\s]+)", lowered)
        or re.search(r"(?:^|\s)[a-z]:[\\/](?:[^\s]+)", lowered)
    ):
        return "[REDACTED]"
    if any(fragment in lowered for fragment in SENSITIVE_FRAGMENTS):
        return "[REDACTED]"
    return rendered


class RedactingJsonFormatter(logging.Formatter):
    def __init__(self, *, app_version: str) -> None:
        super().__init__()
        self.app_version = app_version

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "server_time": datetime.now(timezone.utc).isoformat(),
            "message": record.getMessage()
            if record.getMessage() in {
                "request completed",
                "authentication database unavailable",
                "successful login audit unavailable",
                "login failure audit unavailable",
                "logout audit unavailable",
                "maintenance authorization audit unavailable",
                "account operation failed closed",
                "password reset failed closed",
                "password change transaction unavailable",
                "session validation database unavailable",
            }
            else "event",
            "app_version": self.app_version,
        }
        for field in LOG_FIELDS:
            if hasattr(record, field) and not is_sensitive_key(field):
                payload["module" if field == "app_module" else field] = safe_log_value(
                    getattr(record, field)
                )
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_json_logging(app) -> None:
    path = Path(app.config["LOG_FILE"])
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path,
        maxBytes=int(app.config["LOG_MAX_BYTES"]),
        backupCount=int(app.config["LOG_BACKUP_COUNT"]),
        encoding="utf-8",
    )
    handler.setFormatter(RedactingJsonFormatter(app_version=app.config["VERSION"]))
    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(logging.INFO)
    app.logger.handlers = [handler]
    app.logger.setLevel(logging.INFO)
    app.logger.propagate = False
