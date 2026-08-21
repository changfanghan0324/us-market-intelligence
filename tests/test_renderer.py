from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from market_intelligence.domain.models import (
    CLOSED_MARKET_MESSAGE,
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
    assert "src=\"http" not in html.casefold()


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
    assert "not an authoritative full earnings calendar" in html
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


def test_public_projection_drops_private_top_level_and_nested_payloads() -> None:
    report = sample_report().model_dump(mode="json")
    report["private_payload"] = {
        "portfolio_holdings": "LEAK_SENTINEL_9281",
        "account_number": "123-PRIVATE",
    }
    report["earnings"]["candidates"][0]["journal_entries"] = [
        "LEAK_SENTINEL_9281"
    ]

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
        ("unavailable", "Earnings provider failed after bounded retries.", "analysis unavailable"),
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
