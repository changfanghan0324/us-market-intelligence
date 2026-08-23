"""Standalone HTML renderer with an explicit public-data projection.

The template never receives the canonical object.  It receives a newly created
dictionary containing only fields named in the projection functions below.
This runtime boundary remains effective even if a future canonical model gains
private holdings or journal attributes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from market_intelligence.publishing.safety import assert_standalone_html


class PublicProjectionError(ValueError):
    """Raised when canonical data cannot be safely projected for publication."""


_MISSING = object()
_SAFE_REPORT_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{5,127}\Z", re.ASCII)
_SEC_EARNINGS_WARNING_LABELS = {
    "sec_earnings_search_unavailable": "SEC filing search",
    "sec_earnings_documents_incomplete": ("one or more matching SEC filing documents"),
}

# Reviewable allowlists.  Canonical fields not named here—including provider run
# payloads and any future holdings/journal fields—cannot reach the renderer.
PUBLIC_REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "report_id",
        "report_date",
        "generated_at",
        "timezone",
        "source_cutoff_at",
        "market_context",
        "market_news",
        "earnings",
        "global_macro",
        "research_discovery",
        "knowledge_refresh",
        "section_statuses",
        "validation_status",
    }
)
PUBLIC_SOURCE_FIELDS = frozenset(
    {
        "evidence_id",
        "title",
        "publisher",
        "tier",
        "url",
        "published_at",
        "accessed_at",
        "evidence_role",
        "quoted_fragment",
    }
)
PUBLIC_IMPACT_FIELDS = frozenset(
    {
        "what_changed",
        "why_it_matters",
        "beneficiaries",
        "losers",
        "professional_investor_reaction",
        "indicators_to_monitor_next",
    }
)


def _get(obj: Any, name: str, default: Any = _MISSING) -> Any:
    if isinstance(obj, Mapping):
        if name in obj:
            return obj[name]
    elif hasattr(obj, name):
        return getattr(obj, name)
    if default is _MISSING:
        raise PublicProjectionError(f"required public field is missing: {name}")
    return default


def _first(obj: Any, names: tuple[str, ...], default: Any = _MISSING) -> Any:
    for name in names:
        if isinstance(obj, Mapping) and name in obj:
            return obj[name]
        if not isinstance(obj, Mapping) and hasattr(obj, name):
            return getattr(obj, name)
    if default is _MISSING:
        raise PublicProjectionError(f"required public field is missing: {names[0]}")
    return default


def _scalar(value: Any) -> str:
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if value is None:
        return ""
    return str(value)


def _text(value: Any, field: str, *, optional: bool = False) -> str:
    rendered = _scalar(value).strip()
    if not rendered and not optional:
        raise PublicProjectionError(f"public field must not be blank: {field}")
    return rendered


def _list(value: Any, field: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        raise PublicProjectionError(f"public field must be a list: {field}")
    return list(value)


def _number(value: Any, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise PublicProjectionError(f"public field must be numeric: {field}")
    return _scalar(value)


def _project_actor(actor: Any) -> dict[str, str]:
    kind = _text(_get(actor, "kind"), "impact actor kind")
    rationale = _text(_get(actor, "rationale"), "impact actor rationale")
    if kind == "named_entity":
        return {
            "kind": kind,
            "name": _text(_get(actor, "name"), "impact actor name"),
            "rationale": rationale,
        }
    if kind == "none_identified":
        return {"kind": kind, "name": "None identified", "rationale": rationale}
    raise PublicProjectionError("impact actor kind is not publishable")


def _project_impact(impact: Any) -> dict[str, Any]:
    return {
        "what_changed": _text(_get(impact, "what_changed"), "what_changed"),
        "why_it_matters": _text(_get(impact, "why_it_matters"), "why_it_matters"),
        "beneficiaries": [
            _project_actor(item)
            for item in _list(_get(impact, "beneficiaries"), "beneficiaries")
        ],
        "losers": [
            _project_actor(item) for item in _list(_get(impact, "losers"), "losers")
        ],
        "professional_investor_reaction": _text(
            _get(impact, "professional_investor_reaction"),
            "professional_investor_reaction",
        ),
        "indicators_to_monitor_next": [
            _text(item, "indicator")
            for item in _list(
                _get(impact, "indicators_to_monitor_next"),
                "indicators_to_monitor_next",
            )
        ],
    }


def _project_source(source: Any) -> dict[str, str]:
    url = _text(_get(source, "url"), "source URL")
    if not url.startswith("https://"):
        raise PublicProjectionError("only HTTPS source URLs may be published")
    return {
        "evidence_id": _text(_get(source, "evidence_id"), "evidence_id"),
        "title": _text(_get(source, "title"), "source title"),
        "publisher": _text(_get(source, "publisher"), "source publisher"),
        "tier": _text(_get(source, "tier"), "source tier"),
        "url": url,
        "published_at": _text(
            _get(source, "published_at", None), "source published_at", optional=True
        ),
        "accessed_at": _text(_get(source, "accessed_at"), "source accessed_at"),
        "evidence_role": _text(_get(source, "evidence_role"), "source evidence_role"),
        "quoted_fragment": _text(
            _get(source, "quoted_fragment", None),
            "source quoted_fragment",
            optional=True,
        ),
    }


def _project_sources(value: Any) -> list[dict[str, str]]:
    return [_project_source(source) for source in _list(value, "sources")]


def _project_score_components(
    score: Any, names: tuple[str, ...]
) -> list[dict[str, str]]:
    return [{"key": name, "value": _number(_get(score, name), name)} for name in names]


def _project_metric(metric: Any, field: str) -> dict[str, str]:
    value = _get(metric, "value", None)
    if value is None:
        return {
            "status": "unavailable",
            "value": "",
            "unit": "",
            "as_of": "",
            "provider": "",
            "unavailable_reason": _text(
                _get(metric, "unavailable_reason", "No licensed provider configured."),
                f"{field} unavailable reason",
            ),
        }

    provenance = _text(_get(metric, "provenance"), f"{field} provenance")
    license_confirmed = _get(metric, "license_confirmed", False)
    if provenance != "licensed_market_data_provider" or license_confirmed is not True:
        raise PublicProjectionError(
            f"{field} is numeric but lacks licensed-provider provenance"
        )
    return {
        "status": "known",
        "value": _number(value, field),
        "unit": _text(_get(metric, "unit"), f"{field} unit"),
        "as_of": _text(_get(metric, "as_of"), f"{field} as_of"),
        "provider": _text(_get(metric, "provider"), f"{field} provider"),
        "unavailable_reason": "",
    }


def _project_prediction(prediction: Any) -> dict[str, Any]:
    conditions = _first(
        prediction,
        ("falsification_conditions", "falsification_condition"),
        default=[],
    )
    if isinstance(conditions, str):
        projected_conditions = [_text(conditions, "falsification condition")]
    else:
        projected_conditions = [
            _text(item, "falsification condition")
            for item in _list(conditions, "falsification_conditions")
        ]
    event_window = _get(prediction, "event_window")
    return {
        "prediction_id": _text(_get(prediction, "prediction_id"), "prediction_id"),
        "direction": _text(_get(prediction, "direction"), "prediction direction"),
        "horizon": _text(_get(prediction, "horizon"), "prediction horizon"),
        "confidence": _number(_get(prediction, "confidence"), "prediction confidence"),
        "falsification_conditions": projected_conditions,
        "event_window": {
            "starts_at": _text(
                _get(event_window, "starts_at"), "event window starts_at"
            ),
            "ends_at": _text(_get(event_window, "ends_at"), "event window ends_at"),
            "benchmark": _text(
                _get(event_window, "benchmark"), "event window benchmark"
            ),
            "anchor_basis": _text(
                _get(event_window, "anchor_basis"), "event window anchor_basis"
            ),
        },
        "evidence_ids": [
            _text(item, "prediction evidence ID")
            for item in _list(
                _get(prediction, "evidence_ids", []), "prediction evidence_ids"
            )
        ],
    }


def _project_news(item: Any) -> dict[str, Any]:
    score = _get(item, "attention")
    market_impact = _get(item, "market_impact", None)
    if market_impact is None:
        numeric_score = Decimal(_number(_get(item, "computed_score"), "news score"))
        market_impact = (
            "High" if numeric_score >= 8 else "Medium" if numeric_score >= 5 else "Low"
        )
    return {
        "news_id": _text(_get(item, "news_id"), "news_id"),
        "title": _text(_get(item, "title"), "news title"),
        "event_date": _text(_get(item, "event_date"), "news event_date"),
        "market_impact": _text(market_impact, "news market_impact"),
        "affected_sectors": [
            _text(sector, "affected sector")
            for sector in _list(_get(item, "affected_sectors"), "affected_sectors")
        ],
        "summary": _text(_get(item, "summary"), "news summary"),
        "bull_case": _text(_get(item, "bull_case"), "news bull_case"),
        "bear_case": _text(_get(item, "bear_case"), "news bear_case"),
        "attention_score": _number(_get(item, "computed_score"), "news score"),
        "score_components": _project_score_components(
            score,
            (
                "market_size_impact",
                "earnings_impact",
                "macro_importance",
                "sector_influence",
                "short_term_trading_relevance",
            ),
        ),
        "impact": _project_impact(_get(item, "impact_analysis")),
        "sources": _project_sources(_get(item, "sources")),
    }


def _project_earnings_candidate(candidate: Any) -> dict[str, Any]:
    selection = _get(candidate, "selection")
    release_timing = _text(_get(candidate, "release_timing"), "release_timing")
    release_labels = {
        "before_market": "Before market (exact time unconfirmed)",
        "during_market": "During market (exact time unconfirmed)",
        "after_market": "After market (exact time unconfirmed)",
        "time_not_confirmed": "Release time not confirmed",
    }
    if release_timing not in release_labels:
        raise PublicProjectionError("earnings release timing is not publishable")
    earnings_at = _text(
        _get(candidate, "earnings_at", None),
        "earnings_at",
        optional=True,
    )
    release_label = (
        f"Confirmed at {earnings_at}" if earnings_at else release_labels[release_timing]
    )
    return {
        "ticker": _text(_get(candidate, "ticker"), "earnings ticker"),
        "company_name": _text(_get(candidate, "company_name"), "earnings company_name"),
        "sector": _text(_get(candidate, "sector"), "earnings sector"),
        "release_timing": release_timing,
        "release_timing_label": release_label,
        "earnings_at": earnings_at,
        "current_price": _project_metric(
            _get(candidate, "current_price"), "current_price"
        ),
        "market_cap": _project_metric(_get(candidate, "market_cap"), "market_cap"),
        "expected_eps": _project_metric(
            _get(candidate, "expected_eps"), "expected_eps"
        ),
        "expected_revenue": _project_metric(
            _get(candidate, "expected_revenue"), "expected_revenue"
        ),
        "selection_score": _number(
            _get(candidate, "computed_score"), "earnings selection score"
        ),
        "selection_components": _project_score_components(
            selection,
            (
                "business_quality",
                "revenue_growth",
                "earnings_surprise_potential",
                "market_expectation_gap",
                "risk_reward_asymmetry",
            ),
        ),
        "market_attention": "Yes"
        if _get(candidate, "market_attention") is True
        else "No",
        "why_important": _text(
            _get(candidate, "why_important"), "earnings why_important"
        ),
        "bull_case": _text(_get(candidate, "bull_case"), "earnings bull_case"),
        "base_case": _text(_get(candidate, "base_case"), "earnings base_case"),
        "bear_case": _text(_get(candidate, "bear_case"), "earnings bear_case"),
        "risk": _text(_get(candidate, "risk"), "earnings risk"),
        "risk_level": _text(_get(candidate, "risk_level"), "earnings risk_level"),
        "prediction": _project_prediction(_get(candidate, "prediction")),
        "impact": _project_impact(_get(candidate, "impact_analysis")),
        "sources": _project_sources(_get(candidate, "sources")),
    }


def _project_confirmed_earnings_event(event: Any) -> dict[str, Any]:
    release_timing = _text(_get(event, "release_timing"), "confirmed release timing")
    release_labels = {
        "before_market": "Before market (exact time not confirmed)",
        "during_market": "During market",
        "after_market": "After market (exact time not confirmed)",
        "time_not_confirmed": "Release time not confirmed",
    }
    if release_timing not in release_labels:
        raise PublicProjectionError("confirmed earnings timing is not publishable")
    scheduled_release_at = _text(
        _get(event, "scheduled_release_at", None),
        "confirmed scheduled release",
        optional=True,
    )
    conference_call_at = _text(
        _get(event, "conference_call_at", None),
        "confirmed conference call",
        optional=True,
    )
    return {
        "event_id": _text(_get(event, "event_id"), "confirmed earnings event_id"),
        "ticker": _text(_get(event, "ticker"), "confirmed earnings ticker"),
        "company_name": _text(
            _get(event, "company_name"), "confirmed earnings company_name"
        ),
        "cik": _text(_get(event, "cik"), "confirmed earnings CIK"),
        "form": _text(_get(event, "form"), "confirmed earnings form"),
        "announced_on": _text(
            _get(event, "announced_on"), "confirmed earnings announced_on"
        ),
        "target_date": _text(
            _get(event, "target_date"), "confirmed earnings target_date"
        ),
        "confirmation_basis": _text(
            _get(event, "confirmation_basis"), "confirmed earnings basis"
        ),
        "release_timing": release_timing,
        "release_timing_label": (
            f"Confirmed release: {scheduled_release_at}"
            if scheduled_release_at
            else release_labels[release_timing]
        ),
        "scheduled_release_at": scheduled_release_at,
        "conference_call_at": conference_call_at,
        "confirmation_summary": _text(
            _get(event, "confirmation_summary"),
            "confirmed earnings summary",
        ),
        "sources": _project_sources(_get(event, "sources")),
    }


def _project_earnings(section: Any) -> dict[str, Any]:
    status = _text(_get(section, "status"), "earnings status")
    allowed_statuses = {
        "available",
        "market_closed",
        "no_qualifying_candidates",
        "confirmed_events_available",
        "no_confirmed_events_in_bounded_scan",
        "data_unavailable",
        "unavailable",
    }
    if status not in allowed_statuses:
        raise PublicProjectionError("earnings status is not publishable")
    coverage = _text(_get(section, "universe_coverage"), "earnings universe_coverage")
    if coverage not in {
        "not_applicable",
        "unavailable",
        "bounded_research",
        "authoritative_full_calendar",
    }:
        raise PublicProjectionError("earnings universe coverage is not publishable")
    if status == "data_unavailable" and coverage != "unavailable":
        raise PublicProjectionError(
            "data-unavailable earnings must disclose unavailable coverage"
        )
    if coverage == "unavailable" and status != "data_unavailable":
        raise PublicProjectionError(
            "unavailable earnings coverage requires data-unavailable status"
        )
    return {
        "status": status,
        "target_date": _text(_get(section, "target_date"), "earnings target_date"),
        "universe_coverage": coverage,
        "message": _text(
            _get(section, "message", None), "earnings message", optional=True
        ),
        "next_open_session": _text(
            _get(section, "next_open_session", None),
            "earnings next_open_session",
            optional=True,
        ),
        "candidates": [
            _project_earnings_candidate(candidate)
            for candidate in _list(_get(section, "candidates"), "earnings candidates")
        ],
        "confirmed_events": [
            _project_confirmed_earnings_event(event)
            for event in _list(
                _get(section, "confirmed_events", []),
                "confirmed earnings events",
            )
        ],
    }


def _project_macro(event: Any) -> dict[str, Any] | None:
    if event is None:
        return None
    return {
        "event_id": _text(_get(event, "event_id"), "macro event_id"),
        "title": _text(_get(event, "title"), "macro title"),
        "event_date": _text(_get(event, "event_date"), "macro event_date"),
        "summary": _text(_get(event, "summary"), "macro summary"),
        "market_impact": _text(
            _get(event, "market_impact", "See six-question analysis"),
            "macro market_impact",
        ),
        "affected_assets": [
            _text(item, "affected asset")
            for item in _list(
                _get(event, "affected_asset_classes"), "affected_asset_classes"
            )
        ],
        "impact": _project_impact(_get(event, "impact_analysis")),
        "sources": _project_sources(_get(event, "sources")),
    }


def _project_research(discovery: Any) -> dict[str, Any] | None:
    if discovery is None:
        return None
    impact = _get(discovery, "impact_analysis", None)
    return {
        "discovery_id": _text(_get(discovery, "discovery_id"), "discovery_id"),
        "title": _text(_get(discovery, "title"), "research title"),
        "discovered_on": _text(
            _get(discovery, "discovered_on"), "research discovered_on"
        ),
        "plain_explanation": _text(
            _get(discovery, "plain_explanation"), "research plain_explanation"
        ),
        "changed_belief": _text(
            _get(discovery, "changed_belief"), "research changed_belief"
        ),
        "applications": [
            {
                "category": _text(
                    _get(item, "category"), "research application category"
                ),
                "application": _text(_get(item, "application"), "research application"),
            }
            for item in _list(_get(discovery, "applications"), "research applications")
        ],
        "impact": _project_impact(impact) if impact is not None else None,
        "sources": _project_sources(_get(discovery, "sources")),
    }


def _project_knowledge(knowledge: Any) -> dict[str, str] | None:
    if knowledge is None:
        return None
    return {
        "concept": _text(_get(knowledge, "concept"), "knowledge concept"),
        "background": _text(_get(knowledge, "background"), "knowledge background"),
        "explanation": _text(_get(knowledge, "explanation"), "knowledge explanation"),
        "current_relevance": _text(
            _get(knowledge, "current_relevance"), "knowledge current_relevance"
        ),
        "example": _text(_get(knowledge, "example"), "knowledge example"),
    }


def _project_market_context(context: Any) -> dict[str, str]:
    tomorrow_is_session = _get(context, "tomorrow_is_session")
    report_date_is_session = _get(context, "report_date_is_session")
    if not isinstance(tomorrow_is_session, bool) or not isinstance(
        report_date_is_session, bool
    ):
        raise PublicProjectionError("market session flags must be booleans")
    return {
        "market_status": (
            "NYSE SESSION TODAY" if report_date_is_session else "NYSE CLOSED TODAY"
        ),
        "previous_session": _text(
            _get(context, "previous_session"), "previous session"
        ),
        "tomorrow_date": _text(_get(context, "tomorrow_date"), "tomorrow date"),
        "tomorrow_status": "NYSE SESSION" if tomorrow_is_session else "NYSE CLOSED",
        "next_open_session": _text(
            _get(context, "next_open_session"), "next open session"
        ),
    }


def _project_section_state(state: Any) -> dict[str, str]:
    return {
        "status": _text(_get(state, "status"), "section status"),
        "detail": _text(_get(state, "detail", None), "section detail", optional=True),
    }


def _project_section_statuses(statuses: Any) -> dict[str, dict[str, str]]:
    return {
        "market_news": _project_section_state(_get(statuses, "market_news")),
        "global_macro": _project_section_state(_get(statuses, "global_macro")),
        "earnings": _project_section_state(
            _get(statuses, "earnings", {"status": "available", "detail": None})
        ),
        "research_discovery": _project_section_state(
            _get(statuses, "research_discovery")
        ),
        "knowledge_refresh": _project_section_state(
            _get(statuses, "knowledge_refresh")
        ),
    }


def _report_warnings(report: Any) -> list[str]:
    return [
        _text(warning, "report warning")
        for warning in _list(_get(report, "warnings", []), "report warnings")
    ]


def _field_is_present(obj: Any, name: str) -> bool:
    return name in obj if isinstance(obj, Mapping) else hasattr(obj, name)


def _earnings_provider_runs(
    report: Any,
) -> list[tuple[str, tuple[str, ...] | None]]:
    runs: list[tuple[str, tuple[str, ...] | None]] = []
    for run in _list(_get(report, "provider_runs", []), "provider runs"):
        if _text(_get(run, "section"), "provider run section") != "earnings":
            continue
        warning_codes = None
        if _field_is_present(run, "warning_codes"):
            warning_codes = tuple(
                _text(code, "earnings provider warning code")
                for code in _list(
                    _get(run, "warning_codes"),
                    "earnings provider warning codes",
                )
            )
        runs.append(
            (
                _text(_get(run, "status"), "earnings provider run status"),
                warning_codes,
            )
        )
    return runs


def _validate_degraded_earnings_provider_runs(
    runs: list[tuple[str, tuple[str, ...] | None]],
    *,
    detail: str,
) -> None:
    degraded_runs = [run for run in runs if run[0] == "degraded"]
    if not degraded_runs:
        raise PublicProjectionError(
            "degraded earnings must have degraded provider metadata"
        )

    exposed_codes = [codes for _, codes in degraded_runs if codes is not None]
    if not exposed_codes:
        return
    warning_codes = tuple(
        dict.fromkeys(code for codes in exposed_codes for code in codes)
    )
    if not warning_codes:
        raise PublicProjectionError(
            "degraded earnings provider metadata must expose a recognized SEC warning code"
        )
    if any(code not in _SEC_EARNINGS_WARNING_LABELS for code in warning_codes):
        raise PublicProjectionError(
            "degraded earnings provider metadata contains an unrecognized warning code"
        )
    labels = "; ".join(_SEC_EARNINGS_WARNING_LABELS[code] for code in warning_codes)
    expected_detail = (
        "Bounded SEC earnings coverage is degraded because the following source "
        f"material was unavailable: {labels}. This is not a complete market calendar."
    )
    if detail != expected_detail:
        raise PublicProjectionError(
            "degraded earnings detail does not match provider warning codes"
        )


def project_public_report(report: Any) -> dict[str, Any]:
    """Create a recursively allowlisted, JSON-compatible public view."""

    report_id = _text(_get(report, "report_id"), "report_id")
    if _SAFE_REPORT_ID_RE.fullmatch(report_id) is None:
        raise PublicProjectionError("report_id contains unsupported characters")
    report_date = _text(_get(report, "report_date"), "report_date")
    try:
        parsed_date = date.fromisoformat(report_date)
    except ValueError as exc:
        raise PublicProjectionError("report_date must use YYYY-MM-DD") from exc
    if parsed_date.isoformat() != report_date:
        raise PublicProjectionError("report_date must use canonical YYYY-MM-DD")

    earnings = _project_earnings(_get(report, "earnings"))
    section_statuses = _project_section_statuses(_get(report, "section_statuses"))
    validation_status = _text(_get(report, "validation_status"), "validation_status")
    for required_section in ("market_news", "global_macro"):
        required_state = section_statuses[required_section]
        if required_state["status"] == "unavailable":
            raise PublicProjectionError(
                f"required section cannot be unavailable: {required_section}"
            )
        if required_state["status"] == "degraded" and validation_status != "degraded":
            raise PublicProjectionError(
                f"degraded {required_section} must mark the report degraded"
            )
    earnings_state = section_statuses["earnings"]
    report_warnings = _report_warnings(report)
    earnings_provider_runs = _earnings_provider_runs(report)
    earnings_provider_statuses = [status for status, _ in earnings_provider_runs]
    if earnings["status"] == "data_unavailable":
        if earnings_state["status"] != "degraded":
            raise PublicProjectionError(
                "data-unavailable earnings must have degraded section status"
            )
        if earnings_state["detail"] != earnings["message"]:
            raise PublicProjectionError(
                "data-unavailable earnings detail must match its public message"
            )
        if validation_status != "degraded":
            raise PublicProjectionError(
                "data-unavailable earnings must mark the report degraded"
            )
        if earnings["message"] not in report_warnings:
            raise PublicProjectionError(
                "data-unavailable earnings message must match a report warning"
            )
        if "degraded" in earnings_provider_statuses:
            raise PublicProjectionError(
                "data-unavailable earnings cannot have degraded provider metadata"
            )
    elif earnings_state["status"] == "degraded":
        if validation_status != "degraded":
            raise PublicProjectionError(
                "degraded earnings must mark the report degraded"
            )
        if earnings_state["detail"] not in report_warnings:
            raise PublicProjectionError(
                "degraded earnings detail must match a report warning"
            )
        _validate_degraded_earnings_provider_runs(
            earnings_provider_runs,
            detail=earnings_state["detail"],
        )
    elif earnings_state["status"] != "available":
        raise PublicProjectionError(
            "complete earnings coverage must have available section status"
        )
    elif "degraded" in earnings_provider_statuses:
        raise PublicProjectionError(
            "degraded earnings provider metadata must mark the section degraded"
        )

    return {
        "schema_version": _text(_get(report, "schema_version"), "schema_version"),
        "report_id": report_id,
        "report_date": report_date,
        "generated_at": _text(_get(report, "generated_at"), "generated_at"),
        "timezone": _text(_get(report, "timezone"), "timezone"),
        "source_cutoff_at": _text(_get(report, "source_cutoff_at"), "source_cutoff_at"),
        "market_context": _project_market_context(_get(report, "market_context")),
        "market_news": [
            _project_news(item)
            for item in _list(_get(report, "market_news"), "market_news")
        ],
        "earnings": earnings,
        "global_macro": _project_macro(_get(report, "global_macro")),
        "research_discovery": _project_research(_get(report, "research_discovery")),
        "knowledge_refresh": _project_knowledge(_get(report, "knowledge_refresh")),
        "section_statuses": section_statuses,
        "validation_status": validation_status,
    }


class ReportRenderer:
    """Render public view models with autoescaping and strict template fields."""

    def __init__(self, template_directory: Path | None = None) -> None:
        template_root = template_directory or Path(__file__).with_name("templates")
        if template_root.is_symlink() or not template_root.is_dir():
            raise ValueError("template directory must be a real directory")
        self._environment = Environment(
            loader=FileSystemLoader(str(template_root)),
            autoescape=select_autoescape(
                enabled_extensions=("html", "htm", "xml", "j2"),
                default_for_string=True,
                default=True,
            ),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            auto_reload=False,
        )

    def render(self, report: Any) -> str:
        public_report = project_public_report(report)
        template = self._environment.get_template("report.html.j2")
        html = template.render(report=public_report)
        assert_standalone_html(html)
        if public_report["report_id"] not in html:
            raise PublicProjectionError(
                "rendered HTML is missing the immutable report ID"
            )
        return html

    def render_index(self, manifest: Mapping[str, Any]) -> str:
        # ``manifest`` is built locally from fixed hrefs.  The index template
        # still autoescapes every value as defense in depth.
        template = self._environment.get_template("index.html.j2")
        html = template.render(manifest=manifest)
        assert_standalone_html(html, label="index HTML")
        return html


__all__ = [
    "PUBLIC_IMPACT_FIELDS",
    "PUBLIC_REPORT_FIELDS",
    "PUBLIC_SOURCE_FIELDS",
    "PublicProjectionError",
    "ReportRenderer",
    "project_public_report",
]
