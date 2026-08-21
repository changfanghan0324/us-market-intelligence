"""Minimal JSON logging with deterministic secret and token redaction."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

_REDACTED = "[REDACTED]"
_TOKEN_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\bgh(?:p|o|u|s|r)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*"),
)
_STANDARD_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__)
_SAFE_EXTRA_FIELDS = frozenset(
    {
        "attempt",
        "commit_sha",
        "deployment_url",
        "duration_ms",
        "error_code",
        "phase",
        "provider",
        "report_id",
        "request_id",
        "run_id",
        "section",
        "source_count",
        "status",
        "warning_code",
    }
)


class Redactor:
    """Replace configured secrets and recognizable token formats."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets = tuple(
            sorted(
                {secret for secret in secrets if isinstance(secret, str) and len(secret) >= 4},
                key=len,
                reverse=True,
            )
        )

    def text(self, value: object) -> str:
        result = str(value)
        for secret in self._secrets:
            result = result.replace(secret, _REDACTED)
        for pattern in _TOKEN_PATTERNS:
            result = pattern.sub(_REDACTED, result)
        return result

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {self.text(key): self.value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self.value(item) for item in value]
        return value


class JsonFormatter(logging.Formatter):
    """Emit only an allowlisted, redacted operational event envelope."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._redactor = Redactor(secrets)

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": self._redactor.text(record.getMessage()),
        }
        for name in _SAFE_EXTRA_FIELDS:
            if name in record.__dict__ and name not in _STANDARD_RECORD_FIELDS:
                payload[name] = self._redactor.value(record.__dict__[name])
        if record.exc_info and record.exc_info[0] is not None:
            payload["error_class"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def configure_logging(
    *,
    secrets: Iterable[str] = (),
    level: int | str = logging.INFO,
    stream: IO[str] | None = None,
    log_path: str | Path | None = None,
) -> logging.Logger:
    """Configure redacted JSONL to stderr/a supplied stream and an optional file."""

    resolved_log_path = Path(log_path) if log_path is not None else None
    if resolved_log_path is not None and resolved_log_path.is_symlink():
        raise ValueError("log_path cannot be a symbolic link")
    logger = logging.getLogger("market_intelligence")
    for existing_handler in logger.handlers:
        existing_handler.close()
    logger.handlers.clear()
    formatter = JsonFormatter(secrets)
    stream_handler = logging.StreamHandler(stream)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    if resolved_log_path is not None:
        file_handler = logging.FileHandler(resolved_log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger
