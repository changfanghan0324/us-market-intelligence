"""Exact GitHub Pages tree and manifest integrity verification."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from market_intelligence.reporting.retention import (
    DEFAULT_RETAINED_REPORTS,
    report_filename,
)

from .safety import PublicArtifactSafetyError, assert_safe_tree

_ROOT_FILES = frozenset(
    {".nojekyll", "index.html", "latest.html", "manifest.json", "robots.txt"}
)
_MANIFEST_FIELDS = frozenset(
    {"manifest_version", "generated_at", "retention", "latest", "reports"}
)
_REPORT_FIELDS = frozenset(
    {"report_id", "generated_at", "schema_version", "report_date", "href", "sha256"}
)
_LATEST_FIELDS = _REPORT_FIELDS | {"dated_sha256", "latest_sha256"}
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$", re.ASCII)
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_REPORT_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class VerifiedSite:
    report_id: str
    report_date: date
    sha256: str
    report_count: int


def _fail(message: str) -> PublicArtifactSafetyError:
    return PublicArtifactSafetyError(message)


def _read_bytes(path: Path, *, maximum: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise _fail("managed Pages artifact must be a regular file")
    try:
        if path.stat().st_size > maximum:
            raise _fail("managed Pages artifact exceeds its safe size")
        return path.read_bytes()
    except OSError as error:
        raise _fail("managed Pages artifact could not be read") from error


def _digest(path: Path) -> str:
    return hashlib.sha256(_read_bytes(path, maximum=_MAX_REPORT_BYTES)).hexdigest()


def _manifest(site_root: Path) -> dict[str, Any]:
    raw = _read_bytes(site_root / "manifest.json", maximum=_MAX_MANIFEST_BYTES)
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _fail("Pages manifest is not valid UTF-8 JSON") from error
    if not isinstance(document, dict) or set(document) != _MANIFEST_FIELDS:
        raise _fail("Pages manifest has an invalid top-level shape")
    return document


def _report_entry(raw: object) -> tuple[dict[str, str], date]:
    if not isinstance(raw, dict) or set(raw) != _REPORT_FIELDS:
        raise _fail("Pages manifest contains an invalid report entry")
    if not all(isinstance(raw.get(field), str) for field in _REPORT_FIELDS):
        raise _fail("Pages manifest report fields must be strings")
    entry = {field: raw[field] for field in _REPORT_FIELDS}
    try:
        parsed_date = date.fromisoformat(entry["report_date"])
    except ValueError as error:
        raise _fail("Pages manifest contains an invalid report date") from error
    if parsed_date.isoformat() != entry["report_date"]:
        raise _fail("Pages manifest report date is not canonical")
    if entry["report_id"] != f"daily-market-report-{entry['report_date']}":
        raise _fail("Pages manifest report ID does not match its date")
    expected_href = f"reports/{report_filename(parsed_date)}"
    if entry["href"] != expected_href:
        raise _fail("Pages manifest contains an unsafe report path")
    if _SHA256_RE.fullmatch(entry["sha256"]) is None:
        raise _fail("Pages manifest contains an invalid report digest")
    return entry, parsed_date


def verify_site_tree(
    site_root: str | Path,
    *,
    retained_reports: int = DEFAULT_RETAINED_REPORTS,
    blocked_values: Iterable[str] = (),
) -> VerifiedSite:
    """Reject extras and verify every dated report, digest, and latest alias."""

    root = Path(site_root)
    if root.is_symlink() or not root.is_dir():
        raise _fail("Pages root must be a regular directory")
    if retained_reports != DEFAULT_RETAINED_REPORTS:
        raise ValueError("the public retention policy is fixed at eight reports")
    assert_safe_tree(root, blocked_values=blocked_values)

    children = {path.name for path in root.iterdir()}
    if children != _ROOT_FILES | {"reports"}:
        raise _fail("Pages root contains an unmanaged or missing artifact")
    reports_root = root / "reports"
    if reports_root.is_symlink() or not reports_root.is_dir():
        raise _fail("Pages reports path must be a regular directory")
    if _read_bytes(root / ".nojekyll", maximum=1) != b"":
        raise _fail(".nojekyll must be empty")
    if (
        _read_bytes(root / "robots.txt", maximum=1_024)
        != b"User-agent: *\nDisallow: /\n"
    ):
        raise _fail("robots.txt does not match the publication policy")

    document = _manifest(root)
    if document.get("manifest_version") != "1.0":
        raise _fail("Pages manifest version is unsupported")
    if document.get("retention") != {"dated_reports": retained_reports}:
        raise _fail("Pages manifest retention does not match policy")
    raw_reports = document.get("reports")
    if not isinstance(raw_reports, list) or not 1 <= len(raw_reports) <= retained_reports:
        raise _fail("Pages manifest report count is outside policy")

    entries: list[dict[str, str]] = []
    dates: list[date] = []
    expected_names: set[str] = set()
    for raw_entry in raw_reports:
        entry, parsed_date = _report_entry(raw_entry)
        if parsed_date in dates:
            raise _fail("Pages manifest contains a duplicate report date")
        report_path = root / entry["href"]
        if _digest(report_path) != entry["sha256"]:
            raise _fail("A dated report does not match its manifest digest")
        entries.append(entry)
        dates.append(parsed_date)
        expected_names.add(report_path.name)
    if dates != sorted(dates, reverse=True):
        raise _fail("Pages manifest reports are not newest-first")

    actual_reports: set[str] = set()
    for path in reports_root.iterdir():
        if path.is_symlink() or not path.is_file():
            raise _fail("Pages reports directory contains an unmanaged path")
        actual_reports.add(path.name)
    if actual_reports != expected_names:
        raise _fail("Pages reports directory does not exactly match the manifest")

    raw_latest = document.get("latest")
    if not isinstance(raw_latest, dict) or set(raw_latest) != _LATEST_FIELDS:
        raise _fail("Pages latest manifest entry has an invalid shape")
    latest_core = {field: raw_latest.get(field) for field in _REPORT_FIELDS}
    if latest_core != entries[0]:
        raise _fail("Pages latest entry does not match the newest dated report")
    digest = entries[0]["sha256"]
    if (
        raw_latest.get("dated_sha256") != digest
        or raw_latest.get("latest_sha256") != digest
    ):
        raise _fail("Pages latest digests do not agree")
    if _digest(root / "latest.html") != digest:
        raise _fail("latest.html does not match the newest dated report")
    return VerifiedSite(
        report_id=entries[0]["report_id"],
        report_date=dates[0],
        sha256=digest,
        report_count=len(entries),
    )


__all__ = ["VerifiedSite", "verify_site_tree"]
