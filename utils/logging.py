"""Structured JSON logging with per-request correlation IDs via asyncio context vars."""

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from config import LOG_FILE, LOG_LEVEL

_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)

_STANDARD_ATTRS = frozenset(
    {
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
        "message",
        "taskName",
    }
)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "event": record.message,
            "module": record.name.removeprefix("pueo."),
        }
        for key, val in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = val
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _STANDARD_ATTRS
            and not k.startswith("_")
            and k != "correlation_id"
        }
        suffix = (
            ("  " + "  ".join(f"{k}={v!r}" for k, v in extras.items()))
            if extras
            else ""
        )
        return f"{record.levelname:<8} {record.message}{suffix}"


class StructuredLogger:
    """Logger wrapper that accepts keyword arguments as structured JSON fields."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def _log(self, level: int, event: str, **kwargs: Any) -> None:
        cid = _correlation_id.get("")
        if cid:
            kwargs.setdefault("correlation_id", cid)
        self._logger.log(level, event, extra=kwargs)

    def debug(self, event: str, **kwargs: Any) -> None:
        self._log(logging.DEBUG, event, **kwargs)

    def info(self, event: str, **kwargs: Any) -> None:
        self._log(logging.INFO, event, **kwargs)

    def warning(self, event: str, **kwargs: Any) -> None:
        self._log(logging.WARNING, event, **kwargs)

    def error(self, event: str, **kwargs: Any) -> None:
        self._log(logging.ERROR, event, **kwargs)

    def critical(self, event: str, **kwargs: Any) -> None:
        self._log(logging.CRITICAL, event, **kwargs)


_configured = False


def setup_logging(console_text: bool = False) -> None:
    """Configure the pueo logger with JSON output to file and stderr. Idempotent.

    When console_text=True the stderr handler uses a human-readable plain-text
    format instead of JSON (file handler always stays JSON).
    """
    global _configured
    if _configured:
        return
    _configured = True

    logger = logging.getLogger("pueo")
    logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))
    logger.propagate = False

    json_formatter = _JsonFormatter()

    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setFormatter(json_formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(_TextFormatter() if console_text else json_formatter)
    logger.addHandler(console_handler)


def get_logger(name: str) -> StructuredLogger:
    """Return a StructuredLogger namespaced under pueo.<name>."""
    return StructuredLogger(logging.getLogger(f"pueo.{name}"))


def set_correlation_id(cid: str) -> None:
    """Bind a correlation ID to the current asyncio context."""
    _correlation_id.set(cid)


def get_correlation_id() -> str:
    """Return the active correlation ID, or empty string if none is set."""
    return _correlation_id.get("")
