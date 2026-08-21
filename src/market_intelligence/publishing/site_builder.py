"""Transactional construction of the GitHub Pages and canonical record trees."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from market_intelligence.reporting.renderer import ReportRenderer, project_public_report
from market_intelligence.reporting.retention import (
    DEFAULT_RETAINED_REPORTS,
    RetentionError,
    apply_retention,
    discover_reports,
    report_filename,
)

from .safety import PublicArtifactSafetyError, assert_public_text, assert_safe_tree
from .verification import verify_site_tree

_BASE_RECORD_RE = re.compile(
    r"\Adaily_market_report_(?P<date>\d{4}-\d{2}-\d{2})\.json\Z", re.ASCII
)
_REVISION_RECORD_RE = re.compile(
    r"\Adaily_market_report_(?P<date>\d{4}-\d{2}-\d{2})_revision_"
    r"(?P<digest>[a-f0-9]{12})\.json\Z",
    re.ASCII,
)
_MAX_LEDGER_BYTES = 50 * 1024 * 1024


class PublicationError(RuntimeError):
    """Raised when a report cannot be safely or consistently published."""


@dataclass(frozen=True, slots=True)
class BuildResult:
    """Paths and integrity metadata for a completed publication build."""

    report_id: str
    report_date: date
    dated_html: Path
    latest_html: Path
    manifest: Path
    record: Path
    sha256: str
    changed: bool


def _as_scalar(value: Any) -> str:
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _safe_nonnegative_int(value: Any, field: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PublicationError(f"usage field is invalid: {field}")
    return value


def _json_bytes(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PublicationError("public record is not JSON serializable") from exc
    return (rendered + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_bytes(path: Path, *, maximum: int = _MAX_LEDGER_BYTES) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise PublicationError(f"managed artifact is not a regular file: {path.name}")
    size = path.stat().st_size
    if size > maximum:
        raise PublicationError(f"managed artifact exceeds its safe size: {path.name}")
    return path.read_bytes()


def _atomic_write(path: Path, content: bytes) -> None:
    """Write a fixed staging path without following an existing symlink."""

    if path.parent.is_symlink() or not path.parent.is_dir():
        raise PublicationError("artifact parent must be a real staging directory")
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise PublicationError(f"artifact target is unsafe: {path.name}")

    descriptor, temporary_name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()
        raise


def _copy_tree_without_links(source: Path, destination: Path) -> None:
    """Copy an existing generated tree while refusing links and special files."""

    if source.is_symlink() or not source.is_dir():
        raise PublicationError(f"existing {source.name} path must be a real directory")
    destination.mkdir(parents=True, exist_ok=False)

    for current_root, directories, filenames in os.walk(source, followlinks=False):
        current = Path(current_root)
        relative = current.relative_to(source)
        target_directory = destination / relative

        for directory_name in list(directories):
            item = current / directory_name
            if item.is_symlink():
                raise PublicationError("symlinks are not allowed in managed output")
            mode = item.stat(follow_symlinks=False).st_mode
            if not stat.S_ISDIR(mode):
                raise PublicationError("non-directory found in managed output tree")
            (target_directory / directory_name).mkdir()

        for filename in filenames:
            item = current / filename
            if item.is_symlink():
                raise PublicationError("symlinks are not allowed in managed output")
            mode = item.stat(follow_symlinks=False).st_mode
            if not stat.S_ISREG(mode):
                raise PublicationError("non-regular file found in managed output tree")
            shutil.copy2(item, target_directory / filename, follow_symlinks=False)


def _prepare_tree(existing: Path, staging: Path) -> None:
    if existing.exists() or existing.is_symlink():
        _copy_tree_without_links(existing, staging)
    else:
        staging.mkdir(parents=True, exist_ok=False)


def _tree_digest(root: Path) -> str | None:
    if not root.exists():
        return None
    if root.is_symlink() or not root.is_dir():
        raise PublicationError("managed output path must be a real directory")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
        if path.is_symlink():
            raise PublicationError("symlinks are not allowed in managed output")
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if path.is_dir():
            digest.update(b"D\0" + relative + b"\0")
        elif path.is_file():
            digest.update(b"F\0" + relative + b"\0")
            digest.update(_read_bytes(path))
            digest.update(b"\0")
        else:
            raise PublicationError("non-regular path found in managed output")
    return digest.hexdigest()


def _base_record_filename(report_date: str) -> str:
    candidate = f"daily_market_report_{report_date}.json"
    match = _BASE_RECORD_RE.fullmatch(candidate)
    if match is None or date.fromisoformat(match.group("date")).isoformat() != report_date:
        raise PublicationError("unsafe canonical record date")
    return candidate


def _revision_record_filename(report_date: str, digest: str) -> str:
    candidate = f"daily_market_report_{report_date}_revision_{digest[:12]}.json"
    if _REVISION_RECORD_RE.fullmatch(candidate) is None:
        raise PublicationError("unsafe canonical record revision name")
    return candidate


def _load_json_file(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default
    raw = _read_bytes(path)
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationError(f"managed JSON is invalid: {path.name}") from exc


def _select_record_path(
    records_dir: Path,
    report_date: str,
    record_bytes: bytes,
    *,
    force: bool,
) -> Path:
    """Keep canonical records immutable, adding a content-addressed force revision."""

    base = records_dir / _base_record_filename(report_date)
    if not base.exists():
        _atomic_write(base, record_bytes)
        return base
    existing = _read_bytes(base)
    if existing == record_bytes:
        return base
    if not force:
        raise PublicationError(
            "a different canonical record already exists for this report date; "
            "rerun with force only after review"
        )

    digest = _sha256_bytes(record_bytes)
    revision = records_dir / _revision_record_filename(report_date, digest)
    if revision.exists():
        if _read_bytes(revision) != record_bytes:
            raise PublicationError("canonical record revision digest collision")
    else:
        _atomic_write(revision, record_bytes)
    return revision


def _load_previous_manifest(site_dir: Path) -> dict[str, dict[str, str]]:
    path = site_dir / "manifest.json"
    document = _load_json_file(path, default={})
    if document == {}:
        return {}
    if not isinstance(document, dict) or not isinstance(document.get("reports"), list):
        raise PublicationError("existing manifest has an invalid shape")
    entries: dict[str, dict[str, str]] = {}
    for raw in document["reports"]:
        if not isinstance(raw, dict):
            raise PublicationError("existing manifest report entry is invalid")
        report_date = raw.get("report_date")
        href = raw.get("href")
        if not isinstance(report_date, str) or not isinstance(href, str):
            raise PublicationError("existing manifest report entry is incomplete")
        expected_href = f"reports/{report_filename(report_date)}"
        if href != expected_href:
            raise PublicationError("existing manifest contains an unsafe report href")
        expected_report_id = f"daily-market-report-{report_date}"
        if raw.get("report_id") != expected_report_id:
            raise PublicationError("existing manifest contains an invalid report ID")
        generated_at = raw.get("generated_at")
        if not isinstance(generated_at, str):
            raise PublicationError("existing manifest contains an invalid generated time")
        try:
            parsed_generated_at = datetime.fromisoformat(generated_at)
        except ValueError as exc:
            raise PublicationError(
                "existing manifest contains an invalid generated time"
            ) from exc
        if parsed_generated_at.tzinfo is None or parsed_generated_at.utcoffset() is None:
            raise PublicationError("existing manifest generated time lacks a timezone")
        if raw.get("schema_version") != "1.0":
            raise PublicationError("existing manifest contains an unknown schema version")
        if report_date in entries:
            raise PublicationError("existing manifest contains a duplicate report date")
        entries[report_date] = {
            "report_id": expected_report_id,
            "generated_at": generated_at,
            "schema_version": "1.0",
        }
    return entries


def _build_manifest(
    site_dir: Path,
    public_report: Mapping[str, Any],
    previous: Mapping[str, Mapping[str, str]],
) -> tuple[dict[str, Any], str]:
    reports = discover_reports(site_dir / "reports")
    if not reports:
        raise PublicationError("retention removed every public report")
    if reports[0].report_date.isoformat() != public_report["report_date"]:
        raise PublicationError("the new report date is older than the existing public latest")

    entries: list[dict[str, str]] = []
    for managed in reports:
        report_date = managed.report_date.isoformat()
        digest = _sha256_bytes(_read_bytes(managed.path))
        if report_date == public_report["report_date"]:
            metadata = {
                "report_id": str(public_report["report_id"]),
                "generated_at": str(public_report["generated_at"]),
                "schema_version": str(public_report["schema_version"]),
            }
        else:
            if report_date not in previous:
                raise PublicationError(
                    "a managed dated report is missing validated manifest metadata"
                )
            metadata = dict(previous[report_date])
        entries.append(
            {
                **metadata,
                "report_date": report_date,
                "href": f"reports/{managed.path.name}",
                "sha256": digest,
            }
        )

    latest_dated_bytes = _read_bytes(reports[0].path)
    latest_path = site_dir / "latest.html"
    _atomic_write(latest_path, latest_dated_bytes)
    dated_digest = entries[0]["sha256"]
    latest_digest = _sha256_bytes(_read_bytes(latest_path))
    if dated_digest != latest_digest:
        raise PublicationError("latest.html is not an exact copy of the newest dated report")

    latest = {
        **entries[0],
        "dated_sha256": dated_digest,
        "latest_sha256": latest_digest,
    }
    manifest = {
        "manifest_version": "1.0",
        "generated_at": str(public_report["generated_at"]),
        "retention": {"dated_reports": DEFAULT_RETAINED_REPORTS},
        "latest": latest,
        "reports": entries,
    }
    return manifest, latest_digest


def _append_predictions(records_dir: Path, public_report: Mapping[str, Any]) -> None:
    path = records_dir / "predictions.jsonl"
    existing_bytes = b""
    existing_by_id: dict[str, dict[str, Any]] = {}
    if path.exists():
        existing_bytes = _read_bytes(path)
        try:
            existing_text = existing_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PublicationError("prediction ledger is not UTF-8") from exc
        for line_number, line in enumerate(existing_text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PublicationError(
                    f"prediction ledger contains invalid JSON at line {line_number}"
                ) from exc
            if not isinstance(entry, dict) or not isinstance(entry.get("prediction_id"), str):
                raise PublicationError("prediction ledger entry is invalid")
            prediction_id = entry["prediction_id"]
            if prediction_id in existing_by_id and existing_by_id[prediction_id] != entry:
                raise PublicationError("prediction ledger contains a conflicting duplicate ID")
            existing_by_id[prediction_id] = entry

    new_lines: list[bytes] = []
    for candidate in public_report["earnings"]["candidates"]:
        prediction = candidate["prediction"]
        entry = {
            "schema_version": public_report["schema_version"],
            "report_id": public_report["report_id"],
            "report_date": public_report["report_date"],
            "generated_at": public_report["generated_at"],
            "prediction_id": prediction["prediction_id"],
            "ticker": candidate["ticker"],
            "earnings_at": candidate["earnings_at"],
            "prediction": prediction,
            "source_evidence_ids": [source["evidence_id"] for source in candidate["sources"]],
        }
        prior = existing_by_id.get(prediction["prediction_id"])
        if prior is not None:
            if prior != entry:
                raise PublicationError("prediction ID already exists with different content")
            continue
        compact = json.dumps(
            entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        assert_public_text(compact, label="prediction ledger entry")
        new_lines.append((compact + "\n").encode("utf-8"))

    if not path.exists() or new_lines:
        normalized_existing = existing_bytes
        if normalized_existing and not normalized_existing.endswith(b"\n"):
            normalized_existing += b"\n"
        _atomic_write(path, normalized_existing + b"".join(new_lines))


def _usage_entry(report: Any, public_report: Mapping[str, Any]) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    for run in _field(report, "provider_runs", []) or []:
        provider = _as_scalar(_field(run, "provider", "")).strip()
        section = _as_scalar(_field(run, "section", "")).strip()
        status_value = _as_scalar(_field(run, "status", "")).strip()
        if not provider or not section or status_value not in {
            "success",
            "degraded",
            "failed",
            "skipped",
        }:
            raise PublicationError("provider usage metadata is invalid")
        runs.append(
            {
                "provider": provider,
                "section": section,
                "model": (
                    _as_scalar(_field(run, "model")).strip()
                    if _field(run, "model") is not None
                    else None
                ),
                "status": status_value,
                "attempts": _safe_nonnegative_int(_field(run, "attempts"), "attempts"),
                "duration_ms": _safe_nonnegative_int(
                    _field(run, "duration_ms"), "duration_ms"
                ),
                "source_count": _safe_nonnegative_int(
                    _field(run, "source_count", 0), "source_count"
                ),
                "request_id": (
                    _as_scalar(_field(run, "request_id")).strip()
                    if _field(run, "request_id") is not None
                    else None
                ),
                "request_ids": [
                    _as_scalar(value).strip()
                    for value in (_field(run, "request_ids", []) or [])
                    if _as_scalar(value).strip()
                ],
                "web_search_calls": _safe_nonnegative_int(
                    _field(run, "web_search_calls", 0), "web_search_calls"
                ),
                "input_tokens": _safe_nonnegative_int(
                    _field(run, "input_tokens"), "input_tokens", optional=True
                ),
                "output_tokens": _safe_nonnegative_int(
                    _field(run, "output_tokens"), "output_tokens", optional=True
                ),
            }
        )
    usage_run_id = hashlib.sha256(
        f"{public_report['report_id']}\0{public_report['generated_at']}".encode()
    ).hexdigest()
    return {
        "usage_run_id": usage_run_id,
        "report_id": public_report["report_id"],
        "report_date": public_report["report_date"],
        "generated_at": public_report["generated_at"],
        "runs": runs,
        "totals": {
            "attempts": sum(item["attempts"] for item in runs),
            "duration_ms": sum(item["duration_ms"] for item in runs),
            "web_search_calls": sum(item["web_search_calls"] for item in runs),
            "input_tokens": sum(item["input_tokens"] or 0 for item in runs),
            "output_tokens": sum(item["output_tokens"] or 0 for item in runs),
        },
    }


def _update_usage(records_dir: Path, report: Any, public_report: Mapping[str, Any]) -> None:
    path = records_dir / "usage.json"
    document = _load_json_file(path, default={"schema_version": "1.0", "reports": []})
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != "1.0"
        or not isinstance(document.get("reports"), list)
    ):
        raise PublicationError("usage ledger has an invalid shape")
    reports: list[Any] = document["reports"]
    new_entry = _usage_entry(report, public_report)
    if not any(
        isinstance(item, dict) and item.get("usage_run_id") == new_entry["usage_run_id"]
        for item in reports
    ):
        reports.append(new_entry)
    document["totals"] = {
        "reports": len(reports),
        "input_tokens": sum(
            item.get("totals", {}).get("input_tokens", 0)
            for item in reports
            if isinstance(item, dict)
        ),
        "output_tokens": sum(
            item.get("totals", {}).get("output_tokens", 0)
            for item in reports
            if isinstance(item, dict)
        ),
        "web_search_calls": sum(
            item.get("totals", {}).get("web_search_calls", 0)
            for item in reports
            if isinstance(item, dict)
        ),
    }
    monthly: dict[str, dict[str, int]] = {}
    for item in reports:
        if not isinstance(item, dict) or not isinstance(item.get("report_date"), str):
            raise PublicationError("usage ledger report entry is invalid")
        try:
            month = date.fromisoformat(item["report_date"]).strftime("%Y-%m")
        except ValueError as exc:
            raise PublicationError("usage ledger contains an invalid report date") from exc
        aggregate = monthly.setdefault(
            month,
            {
                "reports": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "web_search_calls": 0,
            },
        )
        aggregate["reports"] += 1
        totals = item.get("totals", {})
        if not isinstance(totals, dict):
            raise PublicationError("usage ledger totals are invalid")
        for field in ("input_tokens", "output_tokens", "web_search_calls"):
            value = totals.get(field, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise PublicationError("usage ledger total is invalid")
            aggregate[field] += value
    document["monthly"] = monthly
    _atomic_write(path, _json_bytes(document))


def _remove_generated_tree(path: Path, expected_parent: Path) -> None:
    if path.parent != expected_parent or path.name not in {"site", "records"}:
        raise PublicationError("refusing to remove an unexpected rollback path")
    if path.is_symlink():
        raise PublicationError("rollback target became a symlink")
    if path.exists():
        shutil.rmtree(path)


def _install_trees(project_root: Path, staged_site: Path, staged_records: Path) -> None:
    """Install both validated trees, restoring the prior pair on an error."""

    final_site = project_root / "site"
    final_records = project_root / "records"
    backup = project_root / f".publication-backup-{uuid4().hex}"
    backup.mkdir()
    old_site = backup / "site"
    old_records = backup / "records"
    had_site = final_site.exists()
    had_records = final_records.exists()
    installed_site = False
    installed_records = False
    cleanup_backup = False

    try:
        if final_site.is_symlink() or final_records.is_symlink():
            raise PublicationError("managed output roots must not be symlinks")
        if had_site:
            os.replace(final_site, old_site)
        if had_records:
            os.replace(final_records, old_records)
        os.replace(staged_site, final_site)
        installed_site = True
        os.replace(staged_records, final_records)
        installed_records = True
        cleanup_backup = True
    except BaseException:
        try:
            if installed_records:
                _remove_generated_tree(final_records, project_root)
            if installed_site:
                _remove_generated_tree(final_site, project_root)
            if old_site.exists():
                os.replace(old_site, final_site)
            if old_records.exists():
                os.replace(old_records, final_records)
            cleanup_backup = True
        except BaseException as rollback_error:
            raise PublicationError(
                "publication install failed and automatic rollback could not complete; "
                "the generated backup was retained"
            ) from rollback_error
        raise
    finally:
        if cleanup_backup and backup.exists() and not backup.is_symlink():
            shutil.rmtree(backup)


class SiteBuilder:
    """Build `site/` and `records/` without exposing the canonical model."""

    def __init__(
        self,
        project_root: Path,
        *,
        renderer: ReportRenderer | None = None,
        retained_reports: int = DEFAULT_RETAINED_REPORTS,
        blocked_values: Iterable[str] = (),
    ) -> None:
        candidate = Path(project_root).absolute()
        if candidate.is_symlink() or not candidate.is_dir():
            raise ValueError("project root must be an existing real directory")
        if retained_reports != DEFAULT_RETAINED_REPORTS:
            raise ValueError("the public retention policy is fixed at eight dated reports")
        self.project_root = candidate.resolve()
        self.renderer = renderer or ReportRenderer()
        self.blocked_values = tuple(
            value
            for value in blocked_values
            if isinstance(value, str) and len(value) >= 8
        )

    def build(self, report: Any, *, force: bool = False) -> BuildResult:
        """Render, validate, stage, and atomically install a report publication."""

        if not isinstance(force, bool):
            raise TypeError("force must be a boolean")
        try:
            public_report = project_public_report(report)
            html = self.renderer.render(report)
        except (PublicArtifactSafetyError, ValueError) as exc:
            raise PublicationError("report failed the public rendering boundary") from exc

        report_date_string = str(public_report["report_date"])
        parsed_report_date = date.fromisoformat(report_date_string)
        dated_filename = report_filename(parsed_report_date)
        html_bytes = html.encode("utf-8")
        if not html_bytes.endswith(b"\n"):
            html_bytes += b"\n"
        record_bytes = _json_bytes(public_report)

        stage_root = Path(
            tempfile.mkdtemp(prefix=".publication-stage-", dir=self.project_root)
        )
        staged_site = stage_root / "site"
        staged_records = stage_root / "records"
        staged_record_path: Path | None = None
        try:
            _prepare_tree(self.project_root / "site", staged_site)
            _prepare_tree(self.project_root / "records", staged_records)
            reports_dir = staged_site / "reports"
            if reports_dir.exists():
                if reports_dir.is_symlink() or not reports_dir.is_dir():
                    raise PublicationError("site/reports must be a real directory")
            else:
                reports_dir.mkdir()

            previous_manifest = _load_previous_manifest(staged_site)
            dated_path = reports_dir / dated_filename
            _atomic_write(dated_path, html_bytes)
            apply_retention(reports_dir, keep=DEFAULT_RETAINED_REPORTS)

            staged_record_path = _select_record_path(
                staged_records,
                report_date_string,
                record_bytes,
                force=force,
            )
            _atomic_write(staged_records / "latest.json", record_bytes)
            _append_predictions(staged_records, public_report)
            _update_usage(staged_records, report, public_report)

            manifest_document, latest_digest = _build_manifest(
                staged_site, public_report, previous_manifest
            )
            _atomic_write(staged_site / "manifest.json", _json_bytes(manifest_document))
            index_html = self.renderer.render_index(manifest_document).encode("utf-8")
            if not index_html.endswith(b"\n"):
                index_html += b"\n"
            _atomic_write(staged_site / "index.html", index_html)
            _atomic_write(staged_site / "robots.txt", b"User-agent: *\nDisallow: /\n")
            _atomic_write(staged_site / ".nojekyll", b"")

            assert_safe_tree(staged_site, blocked_values=self.blocked_values)
            assert_safe_tree(staged_records, blocked_values=self.blocked_values)
            verified = verify_site_tree(
                staged_site,
                blocked_values=self.blocked_values,
            )
            latest_bytes = _read_bytes(staged_site / "latest.html")
            dated_bytes = _read_bytes(staged_site / "reports" / dated_filename)
            if latest_bytes != dated_bytes or _sha256_bytes(latest_bytes) != latest_digest:
                raise PublicationError("latest report integrity check failed")
            if verified.report_id != public_report["report_id"]:
                raise PublicationError("verified latest report ID does not match the build")

            changed = (
                _tree_digest(staged_site) != _tree_digest(self.project_root / "site")
                or _tree_digest(staged_records) != _tree_digest(self.project_root / "records")
            )
            final_record_name = staged_record_path.name
            if changed:
                _install_trees(self.project_root, staged_site, staged_records)

            result = BuildResult(
                report_id=str(public_report["report_id"]),
                report_date=parsed_report_date,
                dated_html=self.project_root / "site" / "reports" / dated_filename,
                latest_html=self.project_root / "site" / "latest.html",
                manifest=self.project_root / "site" / "manifest.json",
                record=self.project_root / "records" / final_record_name,
                sha256=latest_digest,
                changed=changed,
            )
            return result
        except (OSError, PublicArtifactSafetyError, RetentionError, ValueError) as exc:
            if isinstance(exc, PublicationError):
                raise
            raise PublicationError("publication staging failed safely") from exc
        finally:
            if stage_root.exists() and not stage_root.is_symlink():
                shutil.rmtree(stage_root)


__all__ = ["BuildResult", "PublicationError", "SiteBuilder"]
