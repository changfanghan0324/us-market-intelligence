"""Cross-object checks that depend on runtime policy rather than one model."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from market_intelligence.domain.models import DailyReport, SourceEvidence
from market_intelligence.errors import EvidenceValidationError


def iter_report_sources(report: DailyReport) -> Iterable[SourceEvidence]:
    for item in report.market_news:
        yield from item.sources
    for candidate in report.earnings.candidates:
        yield from candidate.sources
    for event in report.earnings.confirmed_events:
        yield from event.sources
    yield from report.global_macro.sources
    if report.research_discovery is not None:
        yield from report.research_discovery.sources


def _host_matches(host: str, domain: str) -> bool:
    normalized_host = host.rstrip(".").casefold()
    normalized_domain = domain.rstrip(".").casefold()
    return normalized_host == normalized_domain or normalized_host.endswith(
        f".{normalized_domain}"
    )


def validate_source_domains(
    report: DailyReport,
    *,
    allowed_domains: Iterable[str],
    publisher_domains: Mapping[str, str] | None = None,
) -> None:
    """Enforce the run's host allowlist and optional publisher/host binding."""

    allowed = tuple(domain.casefold().rstrip(".") for domain in allowed_domains)
    if not allowed:
        raise EvidenceValidationError("source domain allowlist cannot be empty")
    publisher_map = {
        publisher.casefold(): domain.casefold().rstrip(".")
        for publisher, domain in (publisher_domains or {}).items()
    }
    for source in iter_report_sources(report):
        host = (source.url.host or "").casefold().rstrip(".")
        if not any(_host_matches(host, domain) for domain in allowed):
            raise EvidenceValidationError(
                "evidence host is outside the configured allowlist"
            )
        expected = publisher_map.get(source.publisher.casefold())
        if expected is not None and not _host_matches(host, expected):
            raise EvidenceValidationError(
                "evidence publisher does not match its URL host"
            )


def validate_source_cutoff(report: DailyReport) -> None:
    """Reject evidence known to have been published after the report's as-of cutoff."""

    for source in iter_report_sources(report):
        if (
            source.published_at is not None
            and source.published_at > report.source_cutoff_at
        ):
            raise EvidenceValidationError(
                "evidence was published after the report source cutoff"
            )


__all__ = [
    "iter_report_sources",
    "validate_source_cutoff",
    "validate_source_domains",
]
