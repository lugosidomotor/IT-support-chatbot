"""Structured (JSON lines) logging with secret redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

_SECRETS: list[str] = []

# Common credential shapes, redacted even if they were never configured here.
_PATTERNS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-or-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\bATSTT[A-Za-z0-9_=-]{10,}\b"),
    re.compile(r"\bATATT[A-Za-z0-9_=-]{10,}\b"),
]

_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


def register_secrets(values: list[str]) -> None:
    """Register configured secret values so they are scrubbed from every log line."""
    for value in values:
        if value and value not in _SECRETS:
            _SECRETS.append(value)
    _SECRETS.sort(key=len, reverse=True)


def redact(text: str) -> str:
    if not text:
        return text
    for secret in _SECRETS:
        text = text.replace(secret, "[REDACTED]")
    for pattern in _PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)} [REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_") and key != "color_message":
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, default=str, ensure_ascii=False))


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Third-party request loggers are noisy and may print full URLs; keep them quiet.
    for noisy in ("httpx", "httpx2", "httpcore", "mcp", "claude_agent_sdk"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for uvicorn_logger in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(uvicorn_logger)
        lg.handlers[:] = []
        lg.propagate = True
