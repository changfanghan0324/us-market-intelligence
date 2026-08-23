from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from market_intelligence.domain.models import (
    CLOSED_MARKET_MESSAGE,
    EARNINGS_CONFIRMED_EVENTS_MESSAGE,
    EARNINGS_DATA_UNAVAILABLE_MESSAGE,
    NO_CONFIRMED_EARNINGS_EVENTS_MESSAGE,
    NO_QUALIFYING_EARNINGS_MESSAGE,
    DailyReport,
)
from market_intelligence.reporting.renderer import (
    PUBLIC_REPORT_FIELDS,
    PublicProjectionError,
    ReportRenderer,
    project_public_report,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_report.json"


def sample_report() -> DailyReport:
    return DailyReport.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def mark_sec_earnings_degraded(
    report: dict[str, object], *, warning: str, warning_code: str
) -> None:
    section_statuses = report["section_statuses"]
    assert isinstance(section_statuses, dict)
    section_statuses["earnings"] = {"status": "degraded", "detail": warning}
    report["validation_status"] = "degraded"
    warnings = report["warnings"]
    assert isinstance(warnings, list)
    warnings.append(warning)
    provider_runs = report["provider_runs"]
    assert isinstance(provider_runs, list)
    provider_runs.append(
        {
            "provider": "official_public_sources",
            "section": "earnings",
            "model": "deterministic-official-v2",
            "status": "degraded",
            "attempts": 1,
            "duration_ms": 100,
            "source_count": 1,
            "web_search_calls": 0,
            "request_id": None,
            "request_ids": [],
            "warning_codes": [warning_code],
            "input_tokens": None,
            "output_tokens": None,
        }
    )


def test_renderer_is_standalone_mobile_safe_and_answers_all_six_questions() -> None:
    html = ReportRenderer().render(sample_report())

    assert '<meta name="viewport"' in html
    assert "Content-Security-Policy" in html
    assert "noindex,nofollow,noarchive,nosnippet" in html
    assert "daily-market-report-2026-08-19" in html
    assert "2026-08-19T12:05:00+00:00" in html
    assert html.count("What changed?") == 5
    assert html.count("Why does it matter?") == 5
    assert html.count("Who benefits?") == 5
    assert html.count("Who loses?") == 5
    assert html.count("How would professional investors react?") == 5
    assert html.count("What indicators should be monitored next?") == 5
    assert "For personal research and education only" in html
    assert "not investment advice" in html
    assert "<script" not in html.casefold()
    assert "<link" not in html.casefold()
    assert 'src="http' not in html.casefold()


def test_renderer_autoescapes_untrusted_model_text() -> None:
    report = sample_report()
    report.market_news[0].title = '<script data-test="unsafe">alert(1)</script>'

    html = ReportRenderer().render(report)

    assert "<script" not in html.casefold()
    assert "&lt;script data-test=&#34;unsafe&#34;&gt;" in html


def test_earnings_renders_required_decision_fields_and_unavailable_metrics() -> None:
    html = ReportRenderer().render(sample_report())

    assert "ACME · Acme Semiconductor · Semiconductors" in html
    assert "Why important:" in html
    assert "bellwether for advanced packaging demand" in html
    assert "Market attention: Yes" in html
    assert "Risk level: high" in html
    assert "Expectations are elevated" in html
    assert "Earnings release · Confirmed at 2026-08-20T20:05:00+00:00" in html
    assert "not a complete market earnings calendar" in html
    assert "Business Strategy:" in html
    assert html.count("Unavailable (no licensed provider configured)") == 4


def test_unconfirmed_earnings_time_is_not_rendered_as_an_exact_timestamp() -> None:
    report = sample_report().model_dump(mode="json")
    candidate = report["earnings"]["candidates"][0]
    candidate["release_timing"] = "time_not_confirmed"
    candidate["earnings_at"] = None
    candidate["prediction"]["event_window"]["anchor_basis"] = "market_window_proxy"

    html = ReportRenderer().render(report)

    assert "Earnings release · Release time not confirmed" in html
    assert "market-window proxy; exact release time unconfirmed" in html


def test_degraded_confirmed_sec_event_projects_and_renders_release_and_call() -> None:
    report = deepcopy(sample_report().model_dump(mode="json"))
    source = deepcopy(report["earnings"]["candidates"][0]["sources"][0])
    report["earnings"].update(
        {
            "status": "confirmed_events_available",
            "universe_coverage": "bounded_research",
            "message": EARNINGS_CONFIRMED_EVENTS_MESSAGE,
            "candidates": [],
            "confirmed_events": [
                {
                    "event_id": "earnings_event_xpev",
                    "ticker": "XPEV",
                    "company_name": "XPENG INC.",
                    "cik": "0001810997",
                    "form": "6-K",
                    "announced_on": "2026-08-19",
                    "target_date": "2026-08-20",
                    "confirmation_basis": "board_meeting_results",
                    "release_timing": "time_not_confirmed",
                    "scheduled_release_at": None,
                    "conference_call_at": "2026-08-20T12:00:00+00:00",
                    "confirmation_summary": (
                        "The SEC filing confirms a board meeting and a separate "
                        "conference call, but not the results-release time."
                    ),
                    "sources": [source],
                }
            ],
        }
    )
    warning = (
        "Bounded SEC earnings coverage is degraded because the following source "
        "material was unavailable: one or more matching SEC filing documents. "
        "This is not a complete market calendar."
    )
    mark_sec_earnings_degraded(
        report,
        warning=warning,
        warning_code="sec_earnings_documents_incomplete",
    )

    projected = project_public_report(report)
    html = ReportRenderer().render(report)

    assert projected["section_statuses"]["earnings"] == {
        "status": "degraded",
        "detail": warning,
    }
    assert "provider_runs" not in projected
    assert "warnings" not in projected
    assert "XPEV · XPENG INC." in html
    assert "Release time</dt><dd>Not confirmed" in html
    assert "Conference call</dt><dd>2026-08-20T12:00:00+00:00" in html
    assert "Never inferred from call time" in html
    assert "What the filing confirms / 申報文件確認內容" in html


def test_degraded_unavailable_sec_search_projects_and_renders_honestly() -> None:
    report = deepcopy(sample_report().model_dump(mode="json"))
    report["earnings"].update(
        {
            "status": "no_confirmed_events_in_bounded_scan",
            "universe_coverage": "bounded_research",
            "message": NO_CONFIRMED_EARNINGS_EVENTS_MESSAGE,
            "candidates": [],
            "confirmed_events": [],
        }
    )
    warning = (
        "Bounded SEC earnings coverage is degraded because the following source "
        "material was unavailable: SEC filing search. This is not a complete "
        "market calendar."
    )
    mark_sec_earnings_degraded(
        report,
        warning=warning,
        warning_code="sec_earnings_search_unavailable",
    )

    projected = project_public_report(report)
    html = ReportRenderer().render(report)

    assert projected["earnings"]["status"] == ("no_confirmed_events_in_bounded_scan")
    assert projected["section_statuses"]["earnings"]["detail"] == warning
    assert "No confirmed event found in bounded scan" in html
    assert "does not mean that no company reports earnings" in html


def test_degraded_earnings_projection_requires_matching_report_warning() -> None:
    report = deepcopy(sample_report().model_dump(mode="json"))
    warning = "Bounded SEC earnings coverage is degraded after bounded retries."
    mark_sec_earnings_degraded(
        report,
        warning=warning,
        warning_code="sec_earnings_documents_incomplete",
    )
    report["warnings"] = ["A different section produced a separate warning."]

    with pytest.raises(PublicProjectionError, match="match a report warning"):
        project_public_report(report)


def test_degraded_earnings_projection_rejects_warning_substring_match() -> None:
    report = deepcopy(sample_report().model_dump(mode="json"))
    warning = (
        "Bounded SEC earnings coverage is degraded because the following source "
        "material was unavailable: one or more matching SEC filing documents. "
        "This is not a complete market calendar."
    )
    mark_sec_earnings_degraded(
        report,
        warning=warning,
        warning_code="sec_earnings_documents_incomplete",
    )
    report["warnings"] = ["Bounded SEC earnings coverage is degraded"]

    with pytest.raises(PublicProjectionError, match="match a report warning"):
        project_public_report(report)


def test_degraded_earnings_projection_requires_degraded_provider_metadata() -> None:
    report = deepcopy(sample_report().model_dump(mode="json"))
    warning = "Bounded SEC earnings coverage is degraded after bounded retries."
    mark_sec_earnings_degraded(
        report,
        warning=warning,
        warning_code="sec_earnings_documents_incomplete",
    )
    report["provider_runs"] = [
        run for run in report["provider_runs"] if run["section"] != "earnings"
    ]

    with pytest.raises(PublicProjectionError, match="degraded provider metadata"):
        project_public_report(report)


def test_degraded_earnings_projection_rejects_unknown_provider_warning_code() -> None:
    report = deepcopy(sample_report().model_dump(mode="json"))
    warning = (
        "Bounded SEC earnings coverage is degraded because the following source "
        "material was unavailable: one or more matching SEC filing documents. "
        "This is not a complete market calendar."
    )
    mark_sec_earnings_degraded(
        report,
        warning=warning,
        warning_code="sec_earnings_unknown_failure",
    )

    with pytest.raises(PublicProjectionError, match="unrecognized warning code"):
        project_public_report(report)


def test_degraded_earnings_projection_correlates_warning_code_and_detail() -> None:
    report = deepcopy(sample_report().model_dump(mode="json"))
    warning = (
        "Bounded SEC earnings coverage is degraded because the following source "
        "material was unavailable: one or more matching SEC filing documents. "
        "This is not a complete market calendar."
    )
    mark_sec_earnings_degraded(
        report,
        warning=warning,
        warning_code="sec_earnings_search_unavailable",
    )

    with pytest.raises(PublicProjectionError, match="match provider warning codes"):
        project_public_report(report)


def test_public_projection_drops_private_top_level_and_nested_payloads() -> None:
    report = sample_report().model_dump(mode="json")
    report["private_payload"] = {
        "portfolio_holdings": "LEAK_SENTINEL_9281",
        "account_number": "123-PRIVATE",
    }
    report["earnings"]["candidates"][0]["journal_entries"] = ["LEAK_SENTINEL_9281"]

    projected = project_public_report(report)
    html = ReportRenderer().render(report)
    serialized = repr(projected) + html

    assert "private_payload" not in serialized
    assert "portfolio_holdings" not in serialized
    assert "journal_entries" not in serialized
    assert "LEAK_SENTINEL_9281" not in serialized
    assert "provider_runs" not in PUBLIC_REPORT_FIELDS
    assert "warnings" not in PUBLIC_REPORT_FIELDS


def test_numeric_market_data_without_licensed_provenance_is_rejected() -> None:
    report = sample_report().model_dump(mode="json")
    report["earnings"]["candidates"][0]["current_price"] = {
        "value": 123.45,
        "unit": "USD per share",
        "as_of": "2026-08-19T12:00:00Z",
        "provider": "model",
        "provenance": "model",
        "source_evidence_id": "src_earnings",
        "license_confirmed": False,
        "unavailable_reason": None,
    }

    with pytest.raises(PublicProjectionError, match="licensed-provider provenance"):
        project_public_report(report)


@pytest.mark.parametrize(
    ("status", "message", "expected"),
    [
        ("market_closed", CLOSED_MARKET_MESSAGE, CLOSED_MARKET_MESSAGE),
        (
            "no_qualifying_candidates",
            NO_QUALIFYING_EARNINGS_MESSAGE,
            "No qualifying candidates",
        ),
        (
            "unavailable",
            "Earnings provider failed after bounded retries.",
            "analysis unavailable",
        ),
    ],
)
def test_earnings_states_have_distinct_copy(
    status: str, message: str, expected: str
) -> None:
    report = deepcopy(sample_report().model_dump(mode="json"))
    report["earnings"].update(
        {
            "status": status,
            "message": message,
            "candidates": [],
            "next_open_session": "2026-08-24" if status == "market_closed" else None,
        }
    )
    html = ReportRenderer().render(report)

    assert expected in html


def test_data_unavailable_earnings_explains_free_calendar_limit() -> None:
    report = deepcopy(sample_report().model_dump(mode="json"))
    report["earnings"].update(
        {
            "status": "data_unavailable",
            "universe_coverage": "unavailable",
            "message": EARNINGS_DATA_UNAVAILABLE_MESSAGE,
            "candidates": [],
            "next_open_session": None,
        }
    )
    report["section_statuses"]["earnings"] = {
        "status": "degraded",
        "detail": EARNINGS_DATA_UNAVAILABLE_MESSAGE,
    }
    report["validation_status"] = "degraded"
    report["warnings"].append(EARNINGS_DATA_UNAVAILABLE_MESSAGE)

    projected = project_public_report(report)
    html = ReportRenderer().render(report)

    assert projected["earnings"]["status"] == "data_unavailable"
    assert projected["section_statuses"]["earnings"]["status"] == "degraded"
    assert "Calendar data unavailable / 財報日曆資料不可用" in html
    assert "does not mean that no companies report" in html
    assert "No qualifying candidates" not in html


def test_data_unavailable_projection_requires_exact_report_warning() -> None:
    report = deepcopy(sample_report().model_dump(mode="json"))
    report["earnings"].update(
        {
            "status": "data_unavailable",
            "universe_coverage": "unavailable",
            "message": EARNINGS_DATA_UNAVAILABLE_MESSAGE,
            "candidates": [],
        }
    )
    report["section_statuses"]["earnings"] = {
        "status": "degraded",
        "detail": EARNINGS_DATA_UNAVAILABLE_MESSAGE,
    }
    report["validation_status"] = "degraded"
    report["warnings"] = [f"{EARNINGS_DATA_UNAVAILABLE_MESSAGE} Extra context."]

    with pytest.raises(PublicProjectionError, match="message must match"):
        project_public_report(report)


def test_data_unavailable_projection_rejects_degraded_earnings_run() -> None:
    report = deepcopy(sample_report().model_dump(mode="json"))
    report["earnings"].update(
        {
            "status": "data_unavailable",
            "universe_coverage": "unavailable",
            "message": EARNINGS_DATA_UNAVAILABLE_MESSAGE,
            "candidates": [],
        }
    )
    report["section_statuses"]["earnings"] = {
        "status": "degraded",
        "detail": EARNINGS_DATA_UNAVAILABLE_MESSAGE,
    }
    report["validation_status"] = "degraded"
    report["warnings"].append(EARNINGS_DATA_UNAVAILABLE_MESSAGE)
    report["provider_runs"].append(
        {
            "section": "earnings",
            "status": "degraded",
            "warning_codes": ["sec_earnings_search_unavailable"],
        }
    )

    with pytest.raises(PublicProjectionError, match="cannot have degraded provider"):
        project_public_report(report)


def test_data_unavailable_projection_requires_unavailable_coverage() -> None:
    report = deepcopy(sample_report().model_dump(mode="json"))
    report["earnings"].update(
        {
            "status": "data_unavailable",
            "message": EARNINGS_DATA_UNAVAILABLE_MESSAGE,
            "candidates": [],
        }
    )

    with pytest.raises(PublicProjectionError, match="disclose unavailable coverage"):
        project_public_report(report)


def test_data_unavailable_projection_requires_matching_degraded_section() -> None:
    report = deepcopy(sample_report().model_dump(mode="json"))
    report["earnings"].update(
        {
            "status": "data_unavailable",
            "universe_coverage": "unavailable",
            "message": EARNINGS_DATA_UNAVAILABLE_MESSAGE,
            "candidates": [],
        }
    )

    with pytest.raises(PublicProjectionError, match="degraded section status"):
        project_public_report(report)


def test_data_unavailable_projection_requires_degraded_report_status() -> None:
    report = deepcopy(sample_report().model_dump(mode="json"))
    report["earnings"].update(
        {
            "status": "data_unavailable",
            "universe_coverage": "unavailable",
            "message": EARNINGS_DATA_UNAVAILABLE_MESSAGE,
            "candidates": [],
        }
    )
    report["section_statuses"]["earnings"] = {
        "status": "degraded",
        "detail": EARNINGS_DATA_UNAVAILABLE_MESSAGE,
    }

    with pytest.raises(PublicProjectionError, match="mark the report degraded"):
        project_public_report(report)


def test_market_news_extended_source_window_is_visibly_disclosed() -> None:
    report = deepcopy(sample_report().model_dump(mode="json"))
    report["section_statuses"]["market_news"]["detail"] = (
        "This run may use genuine source releases from the prior 14 calendar days "
        "to fill the Top 3 when fewer same-session items are available; every card "
        "shows its event date."
    )

    html = ReportRenderer().render(report)

    assert "Source-window disclosure" in html
    assert "prior 14 calendar days" in html


def test_required_partial_feed_coverage_is_visibly_degraded() -> None:
    report = deepcopy(sample_report().model_dump(mode="json"))
    warning = (
        "Official source coverage is degraded. This feed was unavailable after "
        "bounded retries: BLS Latest Numbers."
    )
    report["section_statuses"]["market_news"] = {
        "status": "degraded",
        "detail": warning,
    }
    report["section_statuses"]["global_macro"] = {
        "status": "degraded",
        "detail": (
            "This run considers genuine source releases from the prior 30 calendar "
            f"days for Global Macro; the selected event date remains visible. {warning}"
        ),
    }
    report["validation_status"] = "degraded"

    projected = project_public_report(report)
    html = ReportRenderer().render(report)

    assert projected["section_statuses"]["market_news"]["status"] == "degraded"
    assert projected["section_statuses"]["global_macro"]["status"] == "degraded"
    assert html.count("Official-feed coverage degraded") == 2
    assert "BLS Latest Numbers" in html
    assert "official_feed_unavailable" not in html
    assert "prior 30 calendar days" in html


@pytest.mark.parametrize("section", ["market_news", "global_macro"])
def test_degraded_required_projection_requires_degraded_report(section: str) -> None:
    report = deepcopy(sample_report().model_dump(mode="json"))
    report["section_statuses"][section] = {
        "status": "degraded",
        "detail": "Official source coverage is degraded after bounded feed retries.",
    }

    with pytest.raises(PublicProjectionError, match=f"degraded {section}"):
        project_public_report(report)
