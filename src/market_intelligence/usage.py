"""Fail-closed monthly usage checks and response-attempt journaling.

Published reports contribute aggregate usage through ``records/usage.json``. Every
provider response is also appended immediately to ``records/usage_events.jsonl``;
events whose request IDs are already reconciled into a published report are ignored.
This preserves observed usage when generation fails after a paid response.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from market_intelligence.config import OpenAIConfig
from market_intelligence.errors import ConfigurationError
from market_intelligence.providers.base import ProviderUsage, ResearchSection

_MAX_USAGE_BYTES = 10 * 1024 * 1024
_USAGE_EVENTS_NAME = "usage_events.jsonl"


class UsageBudgetExceeded(ConfigurationError):
    """The configured monthly provider-usage ceiling has been reached."""


@dataclass(frozen=True, slots=True)
class MonthlyUsage:
    month: str
    input_tokens: int = 0
    output_tokens: int = 0
    web_search_calls: int = 0


@dataclass(frozen=True, slots=True)
class UsageBudgetStatus:
    usage: MonthlyUsage
    warning_codes: tuple[str, ...]


class UsageAttemptJournal:
    """Thread-safe append-only journal for sanitized per-response usage metadata."""

    def __init__(self, path: str | Path, *, report_date: date) -> None:
        candidate = Path(path).absolute()
        if candidate.name != _USAGE_EVENTS_NAME:
            raise ConfigurationError("Usage event journal has an unexpected filename.")
        parent = candidate.parent
        if parent.is_symlink():
            raise ConfigurationError("Usage event journal parent cannot be a link.")
        if not parent.exists():
            project_root = parent.parent
            if (
                parent.name != "records"
                or project_root.is_symlink()
                or not project_root.is_dir()
            ):
                raise ConfigurationError(
                    "Usage event journal parent must be a managed records directory."
                )
            parent.mkdir(mode=0o700)
        if not parent.is_dir():
            raise ConfigurationError("Usage event journal parent must be a directory.")
        if candidate.exists() and (candidate.is_symlink() or not candidate.is_file()):
            raise ConfigurationError("Usage event journal must be a regular file.")
        self.path = candidate
        self.report_date = report_date
        self._lock = threading.Lock()

    def record(
        self,
        section: ResearchSection,
        attempt: int,
        usage: ProviderUsage,
        occurred_at: datetime,
        request_id: str | None,
    ) -> None:
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ConfigurationError("Usage event timestamp must be timezone-aware.")
        if isinstance(attempt, bool) or not 1 <= attempt <= 10:
            raise ConfigurationError("Usage event attempt is invalid.")
        if request_id is not None and (
            not isinstance(request_id, str)
            or not 1 <= len(request_id) <= 200
            or any(character in request_id for character in "\r\n\0")
        ):
            raise ConfigurationError("Usage event request ID is invalid.")
        event = {
            "schema_version": "1.0",
            "event_id": f"usage_{uuid4().hex}",
            "report_date": self.report_date.isoformat(),
            "occurred_at": occurred_at.astimezone(UTC).isoformat(),
            "section": section.value,
            "attempt": attempt,
            "request_id": request_id,
            "input_tokens": _nonnegative_integer(usage.input_tokens, "input-token"),
            "output_tokens": _nonnegative_integer(usage.output_tokens, "output-token"),
            "web_search_calls": _nonnegative_integer(
                usage.web_search_calls, "web-search-call"
            ),
        }
        encoded = (
            json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            + "\n"
        ).encode("utf-8")
        with self._lock:
            self._append(encoded)

    def _append(self, encoded: bytes) -> None:
        if self.path.exists():
            if self.path.is_symlink() or not self.path.is_file():
                raise ConfigurationError("Usage event journal must be a regular file.")
            if self.path.stat().st_size + len(encoded) > _MAX_USAGE_BYTES:
                raise ConfigurationError(
                    "Usage event journal exceeds its safe size limit."
                )
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags, 0o600)
            with os.fdopen(descriptor, "ab", closefd=True) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            raise ConfigurationError(
                "Usage event journal could not be written safely."
            ) from error


def _nonnegative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigurationError(f"Usage ledger has an invalid {field} total.")
    return value


def _load_usage_document(usage_path: Path) -> dict[str, object] | None:
    if not usage_path.exists():
        return None
    if usage_path.is_symlink() or not usage_path.is_file():
        raise ConfigurationError("Usage ledger must be a regular file, not a link.")
    try:
        if usage_path.stat().st_size > _MAX_USAGE_BYTES:
            raise ConfigurationError("Usage ledger exceeds its safe size limit.")
        document = json.loads(usage_path.read_text(encoding="utf-8"))
    except ConfigurationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigurationError("Usage ledger could not be read safely.") from error
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != "1.0"
        or not isinstance(document.get("monthly"), dict)
    ):
        raise ConfigurationError("Usage ledger has an invalid shape.")
    return document


def _reconciled_request_ids(document: dict[str, object] | None) -> set[str]:
    if document is None:
        return set()
    reports = document.get("reports", [])
    if not isinstance(reports, list):
        raise ConfigurationError("Usage ledger reports have an invalid shape.")
    reconciled: set[str] = set()
    for report in reports:
        if not isinstance(report, dict):
            raise ConfigurationError("Usage ledger report entry is invalid.")
        runs = report.get("runs", [])
        if not isinstance(runs, list):
            raise ConfigurationError("Usage ledger provider runs are invalid.")
        for run in runs:
            if not isinstance(run, dict):
                raise ConfigurationError("Usage ledger provider run is invalid.")
            values = run.get("request_ids", [])
            if not isinstance(values, list):
                raise ConfigurationError("Usage ledger request IDs are invalid.")
            request_id = run.get("request_id")
            if isinstance(request_id, str):
                reconciled.add(request_id)
            for value in values:
                if not isinstance(value, str):
                    raise ConfigurationError("Usage ledger request ID is invalid.")
                reconciled.add(value)
    return reconciled


def _unreconciled_event_usage(
    path: Path,
    *,
    month: str,
    reconciled_request_ids: set[str],
) -> tuple[int, int, int]:
    if not path.exists():
        return (0, 0, 0)
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError("Usage event journal must be a regular file.")
    try:
        if path.stat().st_size > _MAX_USAGE_BYTES:
            raise ConfigurationError("Usage event journal exceeds its safe size limit.")
        lines = path.read_text(encoding="utf-8").splitlines()
    except ConfigurationError:
        raise
    except (OSError, UnicodeError) as error:
        raise ConfigurationError(
            "Usage event journal could not be read safely."
        ) from error
    totals = [0, 0, 0]
    event_ids: set[str] = set()
    expected_fields = {
        "schema_version",
        "event_id",
        "report_date",
        "occurred_at",
        "section",
        "attempt",
        "request_id",
        "input_tokens",
        "output_tokens",
        "web_search_calls",
    }
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise ConfigurationError(
                "Usage event journal contains invalid JSON."
            ) from error
        if (
            not isinstance(event, dict)
            or set(event) != expected_fields
            or event.get("schema_version") != "1.0"
        ):
            raise ConfigurationError("Usage event journal contains an invalid event.")
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or event_id in event_ids:
            raise ConfigurationError(
                "Usage event journal contains an invalid event ID."
            )
        event_ids.add(event_id)
        try:
            event_date = date.fromisoformat(event["report_date"])
            occurred_at = datetime.fromisoformat(event["occurred_at"])
        except (TypeError, ValueError) as error:
            raise ConfigurationError(
                "Usage event journal contains an invalid date."
            ) from error
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ConfigurationError(
                "Usage event journal timestamp is not timezone-aware."
            )
        if event.get("section") not in {section.value for section in ResearchSection}:
            raise ConfigurationError("Usage event journal section is invalid.")
        if not 1 <= _nonnegative_integer(event.get("attempt"), "attempt") <= 10:
            raise ConfigurationError("Usage event journal attempt is invalid.")
        request_id = event.get("request_id")
        if request_id is not None and not isinstance(request_id, str):
            raise ConfigurationError("Usage event journal request ID is invalid.")
        if (
            event_date.strftime("%Y-%m") != month
            or request_id in reconciled_request_ids
        ):
            continue
        totals[0] += _nonnegative_integer(event.get("input_tokens"), "input-token")
        totals[1] += _nonnegative_integer(event.get("output_tokens"), "output-token")
        totals[2] += _nonnegative_integer(
            event.get("web_search_calls"), "web-search-call"
        )
    return tuple(totals)


def load_monthly_usage(path: str | Path, *, report_date: date) -> MonthlyUsage:
    """Load published aggregates plus unreconciled observed-response events."""

    usage_path = Path(path)
    month = report_date.strftime("%Y-%m")
    document = _load_usage_document(usage_path)
    raw: object = None
    if document is not None:
        monthly = document["monthly"]
        assert isinstance(monthly, dict)
        raw = monthly.get(month)
    if raw is not None and not isinstance(raw, dict):
        raise ConfigurationError("Usage ledger has an invalid monthly entry.")
    published = raw if isinstance(raw, dict) else {}
    event_input, event_output, event_searches = _unreconciled_event_usage(
        usage_path.with_name(_USAGE_EVENTS_NAME),
        month=month,
        reconciled_request_ids=_reconciled_request_ids(document),
    )
    return MonthlyUsage(
        month=month,
        input_tokens=(
            _nonnegative_integer(published.get("input_tokens", 0), "input-token")
            + event_input
        ),
        output_tokens=(
            _nonnegative_integer(published.get("output_tokens", 0), "output-token")
            + event_output
        ),
        web_search_calls=_nonnegative_integer(
            published.get("web_search_calls", 0), "web-search-call"
        )
        + event_searches,
    )


def enforce_monthly_usage_budget(
    config: OpenAIConfig,
    *,
    usage_path: str | Path,
    report_date: date,
) -> UsageBudgetStatus:
    """Stop at 100% and return stable warning codes at the 80% threshold."""

    usage = load_monthly_usage(usage_path, report_date=report_date)
    checks = (
        (
            "input_tokens",
            usage.input_tokens,
            config.monthly_input_token_limit,
        ),
        (
            "output_tokens",
            usage.output_tokens,
            config.monthly_output_token_limit,
        ),
        (
            "web_search_calls",
            usage.web_search_calls,
            config.monthly_web_search_call_limit,
        ),
    )
    if any(current >= limit for _, current, limit in checks):
        raise UsageBudgetExceeded(
            "The configured monthly OpenAI usage limit has been reached. "
            "Wait for the next calendar month or review and raise the non-secret "
            "limit in config/config.yaml before rerunning."
        )
    warnings = tuple(
        f"monthly_{name}_at_80_percent"
        for name, current, limit in checks
        if current / limit >= config.usage_warning_fraction
    )
    return UsageBudgetStatus(usage=usage, warning_codes=warnings)


__all__ = [
    "MonthlyUsage",
    "UsageAttemptJournal",
    "UsageBudgetExceeded",
    "UsageBudgetStatus",
    "enforce_monthly_usage_budget",
    "load_monthly_usage",
]
