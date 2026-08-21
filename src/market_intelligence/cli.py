"""Command-line boundary used by GitHub Actions.

The CLI performs secret and budget preflight before any paid provider request,
then delegates canonical report generation and transactional publication.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from market_intelligence.config import load_config, require_openai_api_key
from market_intelligence.domain.models import build_report_id
from market_intelligence.domain.validation import (
    validate_source_cutoff,
    validate_source_domains,
)
from market_intelligence.errors import ConfigurationError, MarketIntelligenceError
from market_intelligence.log import configure_logging
from market_intelligence.mktcal import NYSECalendar
from market_intelligence.pipeline import (
    DailyReportPipeline,
    PipelinePolicy,
    ReportContext,
)
from market_intelligence.providers import (
    DisabledMarketDataProvider,
    OpenAIResearchProvider,
    OpenAIResearchSettings,
    ProviderError,
    ResearchSection,
)
from market_intelligence.providers.openai_research import DEFAULT_DOMAINS
from market_intelligence.publishing import (
    PublicationError,
    SiteBuilder,
    verify_site_tree,
)
from market_intelligence.publishing.safety import (
    PublicArtifactSafetyError,
    assert_safe_tree,
)
from market_intelligence.reporting.retention import report_filename
from market_intelligence.usage import UsageAttemptJournal, enforce_monthly_usage_budget

_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$", re.ASCII)


@dataclass(frozen=True, slots=True)
class CommandResult:
    changed: bool
    publishable: bool
    report_id: str
    report_date: str
    sha256: str
    latest_html: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _real_directory(path: str | Path, *, label: str) -> Path:
    candidate = Path(path).absolute()
    if candidate.is_symlink() or not candidate.is_dir():
        raise ConfigurationError(f"{label} must be an existing regular directory.")
    return candidate.resolve()


def _read_small_json(path: Path, *, label: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError(f"{label} must be a regular file.")
    try:
        if path.stat().st_size > _MAX_MANIFEST_BYTES:
            raise ConfigurationError(f"{label} exceeds its safe size limit.")
        return json.loads(path.read_text(encoding="utf-8"))
    except ConfigurationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"{label} could not be read safely.") from error


def _sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError("Managed report artifact must be a regular file.")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _existing_publication(project_root: Path, report_date: date) -> CommandResult | None:
    """Return a validated date-keyed no-op result before spending provider budget."""

    date_string = report_date.isoformat()
    dated = project_root / "site" / "reports" / report_filename(report_date)
    latest = project_root / "site" / "latest.html"
    manifest_path = project_root / "site" / "manifest.json"
    record = project_root / "records" / f"daily_market_report_{date_string}.json"
    markers = (dated, latest, manifest_path, record)
    present = tuple(path.exists() or path.is_symlink() for path in markers)
    if not any(present):
        return None
    if not all(present):
        raise ConfigurationError(
            "Existing publication state is incomplete. Run the scan command and repair "
            "the reports branch before generating another report."
        )

    manifest = _read_small_json(manifest_path, label="Publication manifest")
    canonical = _read_small_json(record, label="Canonical report record")
    report_id = build_report_id(report_date)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("latest"), dict):
        raise ConfigurationError("Publication manifest has an invalid shape.")
    latest_entry = manifest["latest"]
    expected_href = f"reports/{dated.name}"
    expected_values = (
        latest_entry.get("report_id") == report_id,
        latest_entry.get("report_date") == date_string,
        latest_entry.get("href") == expected_href,
        isinstance(canonical, dict),
        isinstance(canonical, dict) and canonical.get("report_id") == report_id,
        isinstance(canonical, dict) and canonical.get("report_date") == date_string,
    )
    if not all(expected_values):
        raise ConfigurationError("Existing publication metadata is inconsistent.")
    declared_sha = latest_entry.get("latest_sha256")
    if not isinstance(declared_sha, str) or _SHA256_RE.fullmatch(declared_sha) is None:
        raise ConfigurationError("Publication manifest has an invalid report digest.")
    dated_sha = _sha256(dated)
    latest_sha = _sha256(latest)
    if declared_sha != dated_sha or declared_sha != latest_sha:
        raise ConfigurationError("Existing latest report failed its integrity check.")
    return CommandResult(
        changed=False,
        publishable=True,
        report_id=report_id,
        report_date=date_string,
        sha256=declared_sha,
        latest_html=str(latest),
    )


def _write_result(path: str | Path, result: CommandResult) -> None:
    result_path = Path(path).absolute()
    if result_path.exists() and (result_path.is_symlink() or not result_path.is_file()):
        raise ConfigurationError("Result path must be a regular file.")
    parent = result_path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise ConfigurationError("Result-file parent must be a regular directory.")
    payload = (json.dumps(asdict(result), sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".result-", dir=parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, result_path)
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


def _settings(config: Any) -> OpenAIResearchSettings:
    domain_overrides = {}
    if config.company_ir_domains:
        for section in (ResearchSection.MARKET_NEWS, ResearchSection.EARNINGS):
            domain_overrides[section] = tuple(
                sorted(
                    set(DEFAULT_DOMAINS[section]).union(config.company_ir_domains)
                )
            )
    return OpenAIResearchSettings(
        model=config.model,
        reasoning_effort=config.reasoning_effort,
        search_context_size=config.search_context_size,
        max_output_tokens=config.max_output_tokens_per_section,
        max_tool_calls=config.max_tool_calls_per_section,
        max_attempts=config.max_retries + 1,
        request_timeout_seconds=config.request_timeout_seconds,
        total_deadline_seconds=config.total_deadline_seconds,
        domain_overrides=domain_overrides,
    )


def _generate(args: argparse.Namespace, logger: logging.Logger) -> int:
    started = time.monotonic()
    config = load_config(args.config)
    api_key = require_openai_api_key(config)
    # Reconfigure with the exact secret as an additional deterministic redaction value.
    logger = configure_logging(secrets=(api_key,), log_path=args.log_file)
    project_root = _real_directory(args.project_root, label="Project root")
    generated_at = _utc_now()
    report_date = generated_at.astimezone(ZoneInfo(config.report.timezone)).date()
    report_id = build_report_id(report_date)
    logger.info(
        "generation preflight started",
        extra={"phase": "preflight", "report_id": report_id, "status": "started"},
    )

    if not args.force:
        existing = _existing_publication(project_root, report_date)
        if existing is not None:
            _write_result(args.result_file, existing)
            logger.info(
                "validated date-keyed report already exists",
                extra={
                    "phase": "idempotency",
                    "report_id": report_id,
                    "status": "successful_completion",
                    "duration_ms": int((time.monotonic() - started) * 1_000),
                },
            )
            return 0

    budget = enforce_monthly_usage_budget(
        config.openai,
        usage_path=project_root / "records" / "usage.json",
        report_date=report_date,
    )
    for warning_code in budget.warning_codes:
        logger.warning(
            "monthly provider usage has crossed the warning threshold",
            extra={
                "phase": "cost_preflight",
                "report_id": report_id,
                "warning_code": warning_code,
                "status": "warning",
            },
        )

    if config.market_data.enabled:
        raise ConfigurationError(
            "Market data is enabled but no reviewed licensed adapter is installed. "
            "Keep market_data.enabled false or implement and security-review an adapter "
            "with public-display rights."
        )
    market_context = NYSECalendar().market_context(report_date)
    usage_journal = UsageAttemptJournal(
        project_root / "records" / "usage_events.jsonl",
        report_date=report_date,
    )
    provider = OpenAIResearchProvider(
        _settings(config.openai),
        api_key=api_key,
        usage_observer=usage_journal.record,
    )
    pipeline = DailyReportPipeline(
        provider,
        market_data_provider=DisabledMarketDataProvider(),
        policy=PipelinePolicy(
            top_news_count=config.publication.required_news_items,
            minimum_earnings_score=config.publication.earnings_min_score,
            max_workers=config.openai.max_parallel_sections,
        ),
    )
    report = pipeline.generate(
        ReportContext(
            market_context=market_context,
            generated_at=generated_at,
            source_cutoff_at=generated_at,
            timezone_name=config.report.timezone,
        )
    )
    validate_source_cutoff(report)
    validate_source_domains(report, allowed_domains=config.openai.allowed_domains)
    for run in report.provider_runs:
        log_method = logger.warning if run.attempts > 1 else logger.info
        log_method(
            (
                "provider section recovered after a bounded retry"
                if run.attempts > 1
                else "provider section completed"
            ),
            extra={
                "phase": "research",
                "report_id": report.report_id,
                "provider": run.provider,
                "section": run.section,
                "status": run.status,
                "source_count": run.source_count,
                "attempt": run.attempts,
                "duration_ms": run.duration_ms,
            },
        )

    built = SiteBuilder(
        project_root,
        retained_reports=config.report.public_history_count,
        blocked_values=(api_key,),
    ).build(report, force=args.force)
    result = CommandResult(
        changed=built.changed,
        publishable=True,
        report_id=built.report_id,
        report_date=built.report_date.isoformat(),
        sha256=built.sha256,
        latest_html=str(built.latest_html),
    )
    _write_result(args.result_file, result)
    logger.info(
        "report generation and staging completed",
        extra={
            "phase": "publication",
            "report_id": built.report_id,
            "status": "successful_completion",
            "duration_ms": int((time.monotonic() - started) * 1_000),
        },
    )
    return 0


def _scan(args: argparse.Namespace, logger: logging.Logger) -> int:
    project_root = _real_directory(args.project_root, label="Project root")
    site = project_root / "site"
    records = project_root / "records"
    assert_safe_tree(site)
    assert_safe_tree(records)
    verified = verify_site_tree(site)
    logger.info(
        "public and canonical artifact scan completed",
        extra={
            "phase": "scan",
            "report_id": verified.report_id,
            "status": "successful_completion",
        },
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market-intelligence")
    commands = parser.add_subparsers(dest="command", required=True)

    generate = commands.add_parser("generate", help="Generate and stage one daily report")
    generate.add_argument("--config", required=True)
    generate.add_argument("--project-root", required=True)
    generate.add_argument("--result-file", required=True)
    generate.add_argument("--log-file", required=True)
    generate.add_argument("--force", action="store_true")

    scan = commands.add_parser("scan", help="Validate staged public and record trees")
    scan.add_argument("--project-root", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logger = configure_logging(log_path=getattr(args, "log_file", None))
    try:
        if args.command == "generate":
            return _generate(args, logger)
        if args.command == "scan":
            return _scan(args, logger)
        raise ConfigurationError("Unsupported command.")
    except (
        MarketIntelligenceError,
        ProviderError,
        PublicationError,
        PublicArtifactSafetyError,
    ) as error:
        section = getattr(error, "section", None)
        logger.error(
            str(error),
            extra={
                "phase": getattr(args, "command", "unknown"),
                "status": "failed",
                "error_code": type(error).__name__,
                "section": getattr(section, "value", None),
            },
        )
        return 2
    except Exception:
        # Do not serialize unexpected exception messages or tracebacks; an SDK may
        # have embedded a request fragment. The class name is recorded by JsonFormatter.
        logger.exception(
            "Unexpected failure; inspect the reviewed code path and rerun safely.",
            extra={
                "phase": getattr(args, "command", "unknown"),
                "status": "failed",
                "error_code": "unexpected_error",
            },
        )
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
