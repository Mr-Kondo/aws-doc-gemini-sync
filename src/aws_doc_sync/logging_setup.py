"""Structured logging to stdout.

Log records carry an ``event`` name plus typed fields rather than prose, so a run
can be filtered and counted. Document bodies and credentials are never logged;
``redact`` exists to make that rule enforceable at the one place URLs and config
summaries enter a record.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, TextIO

_RESERVED = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info",
    "thread", "threadName", "taskName",
}

_SECRET_HINTS = ("token", "secret", "password", "credential", "client_id", "refresh")


def redact(value: Any) -> Any:
    """Replace anything that looks like a secret with a placeholder."""
    if isinstance(value, dict):
        return {
            k: ("***" if any(h in k.lower() for h in _SECRET_HINTS) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = redact(value)
        if record.exc_info:
            payload["error_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            payload["error"] = str(record.exc_info[1]) if record.exc_info[1] else None
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extras = {
            k: redact(v)
            for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        }
        tail = " ".join(f"{k}={v}" for k, v in extras.items())
        line = f"{record.levelname:<5} {record.getMessage()}"
        if tail:
            line = f"{line} {tail}"
        if record.exc_info and record.exc_info[1]:
            line = f"{line} error={record.exc_info[1]}"
        return line


def configure_logging(
    level: str = "INFO", fmt: str = "json", stream: TextIO | None = None
) -> None:
    """Install a single log handler. Safe to call more than once.

    Logs go to stdout by default. ``stream`` exists so that commands emitting a
    machine-readable report can push logs to stderr instead -- interleaving
    structured log lines with a JSON document on one stream would make neither
    parseable.
    """
    level = os.environ.get("AWS_DOC_SYNC_LOG_LEVEL", level).upper()
    fmt = os.environ.get("AWS_DOC_SYNC_LOG_FORMAT", fmt).lower()

    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())

    root = logging.getLogger("aws_doc_sync")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))
    root.propagate = False

    # Third-party HTTP chatter would drown the run's own events.
    for noisy in ("httpx", "httpcore", "googleapiclient", "google_auth_httplib2", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class _SafeLoggerAdapter(logging.LoggerAdapter):
    """Renames ``extra`` keys that collide with LogRecord attributes.

    ``logging`` raises KeyError when ``extra`` contains a reserved name such as
    ``name`` or ``module``. A structured-logging codebase will eventually pass
    one by accident, and an exception raised from a log call would abort a sync
    that was otherwise succeeding. Renaming is the only acceptable outcome.
    """

    def process(self, msg, kwargs):
        extra = kwargs.get("extra")
        if extra:
            kwargs["extra"] = {
                (f"field_{key}" if key in _RESERVED else key): value
                for key, value in extra.items()
            }
        return msg, kwargs


def get_logger(name: str) -> logging.LoggerAdapter:
    return _SafeLoggerAdapter(logging.getLogger(f"aws_doc_sync.{name}"), {})
