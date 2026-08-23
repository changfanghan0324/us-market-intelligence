"""Exact-name retention for the public HTML archive.

Only files matching the managed daily-report name are ever candidates for
deletion.  The canonical JSON records deliberately do not use this module:
those records are append-only so future journal and backtest features retain
their source data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

DEFAULT_RETAINED_REPORTS = 8
_REPORT_RE = re.compile(
    r"\Adaily_market_report_(?P<date>\d{4}-\d{2}-\d{2})\.html\Z",
    re.ASCII,
)


class RetentionError(RuntimeError):
    """Raised when a managed archive path is unsafe or malformed."""


@dataclass(frozen=True, slots=True)
class ManagedReport:
    """A validated, ordinary file in the public report archive."""

    report_date: date
    path: Path


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    """The deterministic result of applying a newest-N retention policy."""

    retained: tuple[ManagedReport, ...]
    expired: tuple[ManagedReport, ...]


def _canonical_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise TypeError("report date must be a date or ISO date string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("report date must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError("report date must use canonical YYYY-MM-DD")
    return parsed


def report_filename(report_date: date | str) -> str:
    """Return the sole supported public filename for a report date."""

    parsed = _canonical_date(report_date)
    return f"daily_market_report_{parsed.isoformat()}.html"


def parse_report_filename(filename: str) -> date | None:
    """Parse an exact managed filename; unrelated names return ``None``."""

    match = _REPORT_RE.fullmatch(filename)
    if match is None:
        return None
    raw_date = match.group("date")
    try:
        parsed = date.fromisoformat(raw_date)
    except ValueError:
        return None
    if report_filename(parsed) != filename:
        return None
    return parsed


def _validate_archive_directory(reports_dir: Path) -> None:
    if reports_dir.is_symlink():
        raise RetentionError("public reports directory must not be a symlink")
    if reports_dir.exists() and not reports_dir.is_dir():
        raise RetentionError("public reports path must be a directory")


def discover_reports(reports_dir: Path) -> tuple[ManagedReport, ...]:
    """Discover validated managed files, sorted newest first.

    Unrelated ordinary files are intentionally ignored.  Any symlink in this
    managed directory is rejected so a later write or cleanup cannot be steered
    outside the staging tree.
    """

    reports_dir = Path(reports_dir)
    _validate_archive_directory(reports_dir)
    if not reports_dir.exists():
        return ()

    discovered: list[ManagedReport] = []
    for candidate in reports_dir.iterdir():
        if candidate.is_symlink():
            raise RetentionError("symlinks are not allowed in the report archive")
        parsed = parse_report_filename(candidate.name)
        if parsed is None:
            continue
        if not candidate.is_file():
            raise RetentionError(
                f"managed report is not a regular file: {candidate.name}"
            )
        discovered.append(ManagedReport(parsed, candidate))

    discovered.sort(key=lambda item: (item.report_date, item.path.name), reverse=True)
    return tuple(discovered)


def plan_retention(
    reports_dir: Path,
    *,
    keep: int = DEFAULT_RETAINED_REPORTS,
) -> RetentionPlan:
    """Return the newest-N retention split without mutating the archive."""

    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1 or keep > 366:
        raise ValueError("keep must be an integer between 1 and 366")
    discovered = discover_reports(reports_dir)
    return RetentionPlan(discovered[:keep], discovered[keep:])


def apply_retention(
    reports_dir: Path,
    *,
    keep: int = DEFAULT_RETAINED_REPORTS,
) -> RetentionPlan:
    """Delete only expired, exact-name public HTML reports.

    Revalidation immediately before unlinking narrows the chance of a path swap.
    Records are not accepted here and must never be pruned by this function.
    """

    reports_dir = Path(reports_dir)
    if reports_dir.name != "reports":
        raise RetentionError("retention may run only on a directory named 'reports'")
    plan = plan_retention(reports_dir, keep=keep)
    for managed in plan.expired:
        candidate = managed.path
        if candidate.parent != reports_dir:
            raise RetentionError("managed report escaped the report archive")
        if candidate.is_symlink() or not candidate.is_file():
            raise RetentionError("managed report changed during retention")
        if parse_report_filename(candidate.name) != managed.report_date:
            raise RetentionError("managed report name changed during retention")
        candidate.unlink()
    return plan


__all__ = [
    "DEFAULT_RETAINED_REPORTS",
    "ManagedReport",
    "RetentionError",
    "RetentionPlan",
    "apply_retention",
    "discover_reports",
    "parse_report_filename",
    "plan_retention",
    "report_filename",
]
