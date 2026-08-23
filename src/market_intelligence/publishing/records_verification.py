"""Semantic and filesystem verification for append-only publication records."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from market_intelligence.providers.base import ResearchSection

from .safety import PublicArtifactSafetyError, assert_safe_tree

_BASE_RECORD_RE = re.compile(
    r"\Adaily_market_report_(?P<date>\d{4}-\d{2}-\d{2})\.json\Z", re.ASCII
)
_REVISION_RECORD_RE = re.compile(
    r"\Adaily_market_report_(?P<date>\d{4}-\d{2}-\d{2})_revision_"
    r"(?P<digest>[a-f0-9]{12})\.json\Z",
    re.ASCII,
)
_EVENT_ID_RE = re.compile(r"\Ausage_[a-f0-9]{32}\Z", re.ASCII)
_SHA256_RE = re.compile(r"\A[a-f0-9]{64}\Z", re.ASCII)
_FIXED_FILENAMES = frozenset(
    {"latest.json", "predictions.jsonl", "usage.json", "usage_events.jsonl"}
)
_MAX_MANAGED_FILE_BYTES = 50 * 1024 * 1024
_RECORD_CORE_FIELDS = frozenset(
    {"schema_version", "report_id", "report_date", "generated_at"}
)
_PREDICTION_FIELDS = frozenset(
    {
        "schema_version",
        "report_id",
        "report_date",
        "generated_at",
        "prediction_id",
        "ticker",
        "earnings_at",
        "prediction",
        "source_evidence_ids",
    }
)
_USAGE_FIELDS = frozenset({"schema_version", "reports", "totals", "monthly"})
_USAGE_REPORT_FIELDS = frozenset(
    {
        "usage_run_id",
        "report_id",
        "report_date",
        "generated_at",
        "runs",
        "totals",
    }
)
_USAGE_RUN_FIELDS = frozenset(
    {
        "provider",
        "section",
        "model",
        "status",
        "attempts",
        "duration_ms",
        "source_count",
        "request_id",
        "request_ids",
        "web_search_calls",
        "input_tokens",
        "output_tokens",
    }
)
_USAGE_TOTAL_FIELDS = frozenset(
    {"attempts", "duration_ms", "web_search_calls", "input_tokens", "output_tokens"}
)
_USAGE_SUMMARY_FIELDS = frozenset(
    {"reports", "input_tokens", "output_tokens", "web_search_calls"}
)
_USAGE_EVENT_FIELDS = frozenset(
    {
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
)
_USAGE_STATUSES = frozenset({"success", "degraded", "failed", "skipped"})
_RESEARCH_SECTIONS = frozenset(section.value for section in ResearchSection)
_RELEASE_TIMINGS = frozenset(
    {"before_market", "during_market", "after_market", "time_not_confirmed"}
)


class _DuplicateJSONKey(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class VerifiedRecords:
    """Integrity metadata for a verified records tree."""

    latest_report_id: str | None
    latest_report_date: date | None
    latest_sha256: str | None
    base_record_count: int
    revision_count: int
    prediction_count: int
    usage_event_count: int


@dataclass(frozen=True, slots=True)
class _Record:
    path: Path
    report_date: date
    raw: bytes
    document: dict[str, Any]


def _fail(message: str) -> PublicArtifactSafetyError:
    return PublicArtifactSafetyError(message)


def _regular_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise _fail("managed record artifact must be a regular file")
    try:
        if path.stat().st_size > _MAX_MANAGED_FILE_BYTES:
            raise _fail("managed record artifact exceeds its safe size")
        return path.read_bytes()
    except OSError as error:
        raise _fail("managed record artifact could not be read") from error


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey(key)
        result[key] = value
    return result


def _parse_json(raw: bytes, *, label: str) -> Any:
    try:
        text = raw.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise _fail(f"{label} is not valid canonical UTF-8 JSON") from error


def _canonical_json_bytes(value: Any, *, compact: bool = False) -> bytes:
    options: dict[str, Any] = {
        "ensure_ascii": False,
        "sort_keys": True,
        "allow_nan": False,
    }
    if compact:
        options["separators"] = (",", ":")
    else:
        options["indent"] = 2
    try:
        rendered = json.dumps(value, **options)
    except (TypeError, ValueError) as error:
        raise _fail("managed record contains a non-canonical JSON value") from error
    return (rendered + "\n").encode("utf-8")


def _json_document(path: Path, *, label: str) -> tuple[bytes, dict[str, Any]]:
    raw = _regular_bytes(path)
    document = _parse_json(raw, label=label)
    if not isinstance(document, dict):
        raise _fail(f"{label} must contain a JSON object")
    if raw != _canonical_json_bytes(document):
        raise _fail(f"{label} does not use the canonical JSON encoding")
    return raw, document


def _canonical_date(value: object, *, label: str) -> date:
    if not isinstance(value, str):
        raise _fail(f"{label} must be a canonical date string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise _fail(f"{label} is invalid") from error
    if parsed.isoformat() != value:
        raise _fail(f"{label} is not canonical")
    return parsed


def _public_date(value: date | str) -> date:
    if isinstance(value, datetime):
        raise _fail("public report dates must not include a time")
    if isinstance(value, date):
        return value
    return _canonical_date(value, label="public report date")


def _aware_timestamp(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise _fail(f"{label} must be a timestamp string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise _fail(f"{label} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _fail(f"{label} must include a timezone")
    return value


def _nonnegative_integer(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _fail(f"{label} must be a nonnegative integer")
    return value


def _optional_nonnegative_integer(value: object, *, label: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_integer(value, label=label)


def _record_document(path: Path, expected_date: date) -> _Record:
    raw, document = _json_document(path, label="canonical report record")
    if not _RECORD_CORE_FIELDS.issubset(document):
        raise _fail("canonical report record is missing required metadata")
    if document.get("schema_version") != "1.0":
        raise _fail("canonical report record has an unsupported schema version")
    report_date = _canonical_date(
        document.get("report_date"), label="canonical record report date"
    )
    if report_date != expected_date:
        raise _fail("canonical report record date does not match its filename")
    expected_report_id = f"daily-market-report-{report_date.isoformat()}"
    if document.get("report_id") != expected_report_id:
        raise _fail("canonical report record ID does not match its filename date")
    _aware_timestamp(
        document.get("generated_at"), label="canonical record generated time"
    )
    earnings = document.get("earnings")
    if not isinstance(earnings, dict) or not isinstance(
        earnings.get("candidates"), list
    ):
        raise _fail("canonical report record has an invalid earnings section")
    return _Record(path=path, report_date=report_date, raw=raw, document=document)


def _record_inventory(
    root: Path,
) -> tuple[dict[date, _Record], dict[date, list[_Record]]]:
    base_records: dict[date, _Record] = {}
    revisions: dict[date, list[_Record]] = {}
    for path in sorted(root.iterdir(), key=lambda candidate: candidate.name):
        if path.is_symlink() or not path.is_file():
            raise _fail("records tree may contain only regular top-level files")
        try:
            if path.stat().st_size > _MAX_MANAGED_FILE_BYTES:
                raise _fail("managed record artifact exceeds its safe size")
        except OSError as error:
            raise _fail("managed record artifact could not be inspected") from error

        base_match = _BASE_RECORD_RE.fullmatch(path.name)
        revision_match = _REVISION_RECORD_RE.fullmatch(path.name)
        if base_match is None and revision_match is None:
            if path.name not in _FIXED_FILENAMES:
                raise _fail(f"records tree contains an unmanaged artifact: {path.name}")
            continue

        match = base_match or revision_match
        assert match is not None
        record_date = _canonical_date(match.group("date"), label="record filename date")
        record = _record_document(path, record_date)
        if base_match is not None:
            base_records[record_date] = record
            continue

        declared_digest = match.group("digest")
        actual_digest = hashlib.sha256(record.raw).hexdigest()[:12]
        if declared_digest != actual_digest:
            raise _fail("canonical revision filename does not match its content digest")
        revisions.setdefault(record_date, []).append(record)

    for report_date, values in revisions.items():
        base = base_records.get(report_date)
        if base is None:
            raise _fail("canonical revision does not have a base report record")
        if any(revision.raw == base.raw for revision in values):
            raise _fail("canonical revision duplicates its immutable base record")
    return base_records, revisions


def _expected_predictions(records: Iterable[_Record]) -> dict[str, dict[str, Any]]:
    expected: dict[str, dict[str, Any]] = {}
    for record in records:
        earnings = record.document["earnings"]
        assert isinstance(earnings, dict)
        candidates = earnings["candidates"]
        assert isinstance(candidates, list)
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise _fail("canonical report contains an invalid earnings candidate")
            prediction = candidate.get("prediction")
            sources = candidate.get("sources")
            if not isinstance(prediction, dict) or not isinstance(sources, list):
                raise _fail("canonical report contains an invalid earnings prediction")
            prediction_id = prediction.get("prediction_id")
            if not isinstance(prediction_id, str) or not prediction_id:
                raise _fail("canonical report contains an invalid prediction ID")
            if candidate.get("ticker") is None or not isinstance(
                candidate.get("ticker"), str
            ):
                raise _fail("canonical report contains an invalid prediction ticker")
            earnings_at_value = candidate.get("earnings_at")
            event_window = prediction.get("event_window")
            if not isinstance(event_window, dict):
                raise _fail("canonical report has an invalid prediction event window")
            if earnings_at_value == "":
                if (
                    candidate.get("release_timing") not in _RELEASE_TIMINGS
                    or event_window.get("anchor_basis") != "market_window_proxy"
                ):
                    raise _fail(
                        "canonical report has an invalid unconfirmed earnings time"
                    )
                earnings_at = ""
            else:
                earnings_at = _aware_timestamp(
                    earnings_at_value, label="prediction earnings time"
                )
                if event_window.get("anchor_basis") != "confirmed_release_time":
                    raise _fail(
                        "canonical report prediction timing basis is inconsistent"
                    )
            evidence_ids: list[str] = []
            for source in sources:
                if not isinstance(source, dict) or not isinstance(
                    source.get("evidence_id"), str
                ):
                    raise _fail("canonical report prediction source is invalid")
                evidence_ids.append(source["evidence_id"])
            entry = {
                "schema_version": record.document["schema_version"],
                "report_id": record.document["report_id"],
                "report_date": record.document["report_date"],
                "generated_at": record.document["generated_at"],
                "prediction_id": prediction_id,
                "ticker": candidate["ticker"],
                "earnings_at": earnings_at,
                "prediction": prediction,
                "source_evidence_ids": evidence_ids,
            }
            prior = expected.get(prediction_id)
            if prior is not None and prior != entry:
                raise _fail(
                    "canonical records reuse a prediction ID with different content"
                )
            expected[prediction_id] = entry
    return expected


def _json_lines(path: Path, *, label: str) -> list[dict[str, Any]]:
    raw = _regular_bytes(path)
    if not raw:
        return []
    if not raw.endswith(b"\n"):
        raise _fail(f"{label} must end with a newline")
    entries: list[dict[str, Any]] = []
    for line_number, encoded_line in enumerate(raw.splitlines(keepends=True), start=1):
        if not encoded_line.endswith(b"\n") or encoded_line in {b"\n", b"\r\n"}:
            raise _fail(f"{label} contains an invalid line at {line_number}")
        line = encoded_line[:-1]
        document = _parse_json(line, label=f"{label} line {line_number}")
        if not isinstance(document, dict):
            raise _fail(f"{label} line {line_number} must be a JSON object")
        if encoded_line != _canonical_json_bytes(document, compact=True):
            raise _fail(f"{label} line {line_number} is not canonical JSON")
        entries.append(document)
    return entries


def _verify_predictions(
    path: Path | None, expected: Mapping[str, dict[str, Any]]
) -> int:
    if path is None:
        if expected:
            raise _fail("prediction ledger is missing canonical report predictions")
        return 0
    entries = _json_lines(path, label="prediction ledger")
    seen: set[str] = set()
    for entry in entries:
        if set(entry) != _PREDICTION_FIELDS or entry.get("schema_version") != "1.0":
            raise _fail("prediction ledger entry has an invalid shape")
        report_date = _canonical_date(
            entry.get("report_date"), label="prediction ledger report date"
        )
        if entry.get("report_id") != f"daily-market-report-{report_date.isoformat()}":
            raise _fail("prediction ledger report ID does not match its date")
        _aware_timestamp(entry.get("generated_at"), label="prediction generated time")
        if entry.get("earnings_at") != "":
            _aware_timestamp(entry.get("earnings_at"), label="prediction earnings time")
        prediction_id = entry.get("prediction_id")
        prediction = entry.get("prediction")
        if (
            not isinstance(prediction_id, str)
            or not prediction_id
            or prediction_id in seen
            or not isinstance(prediction, dict)
            or prediction.get("prediction_id") != prediction_id
            or not isinstance(entry.get("ticker"), str)
            or not isinstance(entry.get("source_evidence_ids"), list)
            or not all(isinstance(value, str) for value in entry["source_evidence_ids"])
        ):
            raise _fail("prediction ledger entry is invalid")
        seen.add(prediction_id)
        if expected.get(prediction_id) != entry:
            raise _fail("prediction ledger entry does not match a canonical report")
    if seen != set(expected):
        raise _fail("prediction ledger does not exactly cover canonical predictions")
    return len(entries)


def _validate_usage_run(run: object) -> dict[str, int]:
    if not isinstance(run, dict) or set(run) != _USAGE_RUN_FIELDS:
        raise _fail("usage ledger provider run has an invalid shape")
    if (
        not isinstance(run.get("provider"), str)
        or not run["provider"]
        or not isinstance(run.get("section"), str)
        or not run["section"]
        or run.get("status") not in _USAGE_STATUSES
        or (run.get("model") is not None and not isinstance(run.get("model"), str))
        or (
            run.get("request_id") is not None
            and not isinstance(run.get("request_id"), str)
        )
        or not isinstance(run.get("request_ids"), list)
        or not all(isinstance(value, str) for value in run["request_ids"])
    ):
        raise _fail("usage ledger provider run is invalid")
    return {
        "attempts": _nonnegative_integer(run.get("attempts"), label="usage attempts"),
        "duration_ms": _nonnegative_integer(
            run.get("duration_ms"), label="usage duration"
        ),
        "web_search_calls": _nonnegative_integer(
            run.get("web_search_calls"), label="usage web search calls"
        ),
        "input_tokens": _optional_nonnegative_integer(
            run.get("input_tokens"), label="usage input tokens"
        )
        or 0,
        "output_tokens": _optional_nonnegative_integer(
            run.get("output_tokens"), label="usage output tokens"
        )
        or 0,
    }


def _sum_usage_runs(runs: list[object]) -> dict[str, int]:
    totals = {
        "attempts": 0,
        "duration_ms": 0,
        "web_search_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    for run in runs:
        values = _validate_usage_run(run)
        for field, value in values.items():
            totals[field] += value
    return totals


def _verify_usage(path: Path, known_reports: set[tuple[str, str, str]]) -> None:
    _, document = _json_document(path, label="usage ledger")
    if set(document) != _USAGE_FIELDS or document.get("schema_version") != "1.0":
        raise _fail("usage ledger has an invalid shape")
    reports = document.get("reports")
    if not isinstance(reports, list):
        raise _fail("usage ledger reports must be a list")

    seen_run_ids: set[str] = set()
    seen_report_keys: set[tuple[str, str, str]] = set()
    expected_summary = {
        "reports": len(reports),
        "input_tokens": 0,
        "output_tokens": 0,
        "web_search_calls": 0,
    }
    expected_monthly: dict[str, dict[str, int]] = {}
    for entry in reports:
        if not isinstance(entry, dict) or set(entry) != _USAGE_REPORT_FIELDS:
            raise _fail("usage ledger report entry has an invalid shape")
        report_date = _canonical_date(
            entry.get("report_date"), label="usage report date"
        )
        report_id = entry.get("report_id")
        generated_at = _aware_timestamp(
            entry.get("generated_at"), label="usage generated time"
        )
        if report_id != f"daily-market-report-{report_date.isoformat()}":
            raise _fail("usage report ID does not match its date")
        report_key = (report_id, report_date.isoformat(), generated_at)
        if report_key not in known_reports:
            raise _fail("usage ledger report does not match a canonical record")
        seen_report_keys.add(report_key)
        expected_run_id = hashlib.sha256(
            f"{report_id}\0{generated_at}".encode()
        ).hexdigest()
        usage_run_id = entry.get("usage_run_id")
        if (
            not isinstance(usage_run_id, str)
            or _SHA256_RE.fullmatch(usage_run_id) is None
            or usage_run_id != expected_run_id
            or usage_run_id in seen_run_ids
        ):
            raise _fail("usage ledger contains an invalid or duplicate run ID")
        seen_run_ids.add(usage_run_id)
        runs = entry.get("runs")
        totals = entry.get("totals")
        if not isinstance(runs, list) or not isinstance(totals, dict):
            raise _fail("usage ledger report totals are invalid")
        expected_totals = _sum_usage_runs(runs)
        if set(totals) != _USAGE_TOTAL_FIELDS or totals != expected_totals:
            raise _fail("usage ledger report totals do not match its provider runs")
        for field in ("input_tokens", "output_tokens", "web_search_calls"):
            expected_summary[field] += expected_totals[field]
        month = report_date.strftime("%Y-%m")
        monthly = expected_monthly.setdefault(
            month,
            {
                "reports": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "web_search_calls": 0,
            },
        )
        monthly["reports"] += 1
        for field in ("input_tokens", "output_tokens", "web_search_calls"):
            monthly[field] += expected_totals[field]

    if seen_report_keys != known_reports:
        raise _fail("usage ledger does not exactly cover canonical report runs")
    totals = document.get("totals")
    if (
        not isinstance(totals, dict)
        or set(totals) != _USAGE_SUMMARY_FIELDS
        or totals != expected_summary
    ):
        raise _fail("usage ledger aggregate totals are inconsistent")
    monthly = document.get("monthly")
    if not isinstance(monthly, dict) or monthly != expected_monthly:
        raise _fail("usage ledger monthly totals are inconsistent")


def _verify_usage_events(path: Path | None) -> int:
    if path is None:
        return 0
    events = _json_lines(path, label="usage event ledger")
    event_ids: set[str] = set()
    for event in events:
        if set(event) != _USAGE_EVENT_FIELDS or event.get("schema_version") != "1.0":
            raise _fail("usage event ledger entry has an invalid shape")
        event_id = event.get("event_id")
        if (
            not isinstance(event_id, str)
            or _EVENT_ID_RE.fullmatch(event_id) is None
            or event_id in event_ids
        ):
            raise _fail("usage event ledger contains an invalid or duplicate event ID")
        event_ids.add(event_id)
        _canonical_date(event.get("report_date"), label="usage event report date")
        _aware_timestamp(event.get("occurred_at"), label="usage event time")
        if event.get("section") not in _RESEARCH_SECTIONS:
            raise _fail("usage event ledger contains an invalid research section")
        attempt = _nonnegative_integer(
            event.get("attempt"), label="usage event attempt"
        )
        if not 1 <= attempt <= 10:
            raise _fail("usage event attempt is outside its allowed range")
        request_id = event.get("request_id")
        if request_id is not None and (
            not isinstance(request_id, str)
            or not 1 <= len(request_id) <= 200
            or any(character in request_id for character in "\r\n\0")
        ):
            raise _fail("usage event request ID is invalid")
        for field in ("input_tokens", "output_tokens", "web_search_calls"):
            _nonnegative_integer(event.get(field), label=f"usage event {field}")
    return len(events)


def verify_records_tree(
    records_root: str | Path,
    *,
    public_report_dates: Iterable[date | str] = (),
    blocked_values: Iterable[str] = (),
) -> VerifiedRecords:
    """Verify the complete append-only records inventory and its cross-links.

    ``public_report_dates`` should come from an already verified Pages manifest.
    Public retention is shorter than canonical retention, so this is intentionally
    a subset check: every public date needs a base record, while older base records
    remain valid and append-only.
    """

    root = Path(records_root)
    if root.is_symlink() or not root.is_dir():
        raise _fail("records root must be a regular directory")
    base_records, revisions = _record_inventory(root)
    assert_safe_tree(root, blocked_values=blocked_values)

    required_dates = {_public_date(value) for value in public_report_dates}
    missing_dates = sorted(required_dates - set(base_records))
    if missing_dates:
        raise _fail(
            "public report date is missing its base canonical record: "
            + missing_dates[0].isoformat()
        )

    latest_path = root / "latest.json"
    usage_path = root / "usage.json"
    predictions_path = root / "predictions.jsonl"
    usage_events_path = root / "usage_events.jsonl"
    if not base_records:
        if any(
            path.exists() or path.is_symlink()
            for path in (latest_path, usage_path, predictions_path)
        ):
            raise _fail("record ledgers exist without a base canonical report")
        usage_event_count = _verify_usage_events(
            usage_events_path if usage_events_path.exists() else None
        )
        return VerifiedRecords(
            latest_report_id=None,
            latest_report_date=None,
            latest_sha256=None,
            base_record_count=0,
            revision_count=0,
            prediction_count=0,
            usage_event_count=usage_event_count,
        )

    if not latest_path.exists() or not usage_path.exists():
        raise _fail("records tree is missing latest.json or usage.json")
    latest_raw, latest_document = _json_document(latest_path, label="latest record")
    latest_date = _canonical_date(
        latest_document.get("report_date"), label="latest record report date"
    )
    newest_date = max(base_records)
    if latest_date != newest_date:
        raise _fail("latest record does not use the newest canonical report date")
    if (
        latest_document.get("report_id")
        != f"daily-market-report-{newest_date.isoformat()}"
    ):
        raise _fail("latest record ID does not match its date")
    candidates = [base_records[newest_date], *revisions.get(newest_date, [])]
    matching_record = next(
        (record for record in candidates if record.raw == latest_raw), None
    )
    if matching_record is None:
        raise _fail(
            "latest record does not match a base or revision for the newest date"
        )

    all_records = [*base_records.values()]
    for values in revisions.values():
        all_records.extend(values)
    expected_predictions = _expected_predictions(all_records)
    prediction_count = _verify_predictions(
        predictions_path if predictions_path.exists() else None,
        expected_predictions,
    )
    known_report_runs = {
        (
            str(record.document["report_id"]),
            str(record.document["report_date"]),
            str(record.document["generated_at"]),
        )
        for record in all_records
    }
    _verify_usage(usage_path, known_report_runs)
    usage_event_count = _verify_usage_events(
        usage_events_path if usage_events_path.exists() else None
    )
    return VerifiedRecords(
        latest_report_id=str(matching_record.document["report_id"]),
        latest_report_date=newest_date,
        latest_sha256=hashlib.sha256(latest_raw).hexdigest(),
        base_record_count=len(base_records),
        revision_count=sum(len(values) for values in revisions.values()),
        prediction_count=prediction_count,
        usage_event_count=usage_event_count,
    )


__all__ = ["VerifiedRecords", "verify_records_tree"]
