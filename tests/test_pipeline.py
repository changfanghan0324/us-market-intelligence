from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from market_intelligence.domain.models import (
    CLOSED_MARKET_MESSAGE,
    NO_QUALIFYING_EARNINGS_MESSAGE,
    MarketContext,
)
from market_intelligence.errors import ReportValidationError
from market_intelligence.pipeline import (
    DailyReportPipeline,
    PipelinePolicy,
    ReportContext,
)
from market_intelligence.providers.base import (
    ProviderResult,
    ProviderRunMetadata,
    ProviderUsage,
    ResearchSection,
    TransientProviderError,
)
from market_intelligence.providers.market_data import (
    EarningsMarketData,
    MarketDataEvidence,
    MarketMetric,
)
from market_intelligence.providers.openai_research import (
    EarningsResponse,
    GlobalMacroResponse,
    KnowledgeRefreshResponse,
    MarketNewsResponse,
    ResearchDiscoveryResponse,
)

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def source(
    index: int,
    *,
    publisher: str = "Reuters",
    domain: str = "www.reuters.com",
) -> dict[str, Any]:
    return {
        "title": f"Evidence document number {index} supporting this analysis",
        "publisher": publisher,
        "url": f"https://{domain}/markets/evidence-{index}/",
        "published_at": "2026-08-19T11:00:00Z",
        "evidence_role": "primary",
        "quoted_fragment": None,
    }


def impact(label: str = "policy") -> dict[str, Any]:
    return {
        "what_changed": (
            f"The verified {label} development changed the market's prior baseline assumption."
        ),
        "why_it_matters": (
            "Discount rates and expected cash-flow paths now imply a different valuation range."
        ),
        "beneficiaries": [
            {
                "kind": "named_entity",
                "name": "US investment-grade issuers",
                "rationale": "Lower financing pressure can improve free-cash-flow resilience.",
            }
        ],
        "losers": [
            {
                "kind": "named_entity",
                "name": "Highly leveraged rate-sensitive firms",
                "rationale": "Adverse rate repricing would increase their refinancing burden.",
            }
        ],
        "professional_investor_reaction": (
            "Portfolio managers would trim crowded exposures and rebalance duration deliberately."
        ),
        "indicators_to_monitor_next": [
            "Two-year Treasury yield after the opening auction",
            "Changes in rate-futures implied probabilities",
        ],
    }


def news_data(count: int = 3) -> MarketNewsResponse:
    candidates = []
    for index in range(count):
        candidates.append(
            {
                "title": f"Material US market catalyst number {index} changes expectations",
                "event_date": "2026-08-19",
                "market_impact": "high",
                "affected_sectors": ["Technology", "Financials"],
                "summary": (
                    f"Verified event {index} altered cross-asset pricing before the US session."
                ),
                "bullish_case": "Easing financial conditions can support forward valuation multiples.",
                "bearish_case": "Crowded positioning can reverse if follow-through data disappoints.",
                "attention_components": {
                    "market_size_impact": float(7 + index),
                    "earnings_impact": 7.0,
                    "macro_importance": 8.0,
                    "sector_influence": 8.0,
                    "short_term_trading_relevance": 8.0,
                },
                "impact_analysis": impact(f"policy-{index}"),
                "sources": [source(index)],
            }
        )
    return MarketNewsResponse.model_validate({"candidates": candidates})


def earnings_data(*, qualifying: bool = True) -> EarningsResponse:
    if not qualifying:
        return EarningsResponse.model_validate({"candidates": []})
    return EarningsResponse.model_validate(
        {
            "candidates": [
                {
                    "ticker": "ACME",
                    "company_name": "Acme Semiconductor",
                    "sector": "Semiconductors",
                    "earnings_date": "2026-08-20",
                    "release_timing": "after_market",
                    "scheduled_release_at": None,
                    "why_important": (
                        "Its order commentary is a timely read-through for AI infrastructure demand."
                    ),
                    "bullish_case": (
                        "Accelerating demand and margin expansion could support a positive revision."
                    ),
                    "base_case": (
                        "Results near expectations would leave the existing valuation debate intact."
                    ),
                    "bearish_case": (
                        "Weak guidance or supply constraints could expose overly optimistic estimates."
                    ),
                    "risk_level": "high",
                    "risk_analysis": (
                        "Expectations are elevated and the reaction may be nonlinear around guidance."
                    ),
                    "selection_components": {
                        "business_quality": 8.0,
                        "revenue_growth": 8.0,
                        "earnings_surprise_potential": 8.0,
                        "market_expectation_gap": 7.0,
                        "risk_reward_asymmetry": 8.0,
                    },
                    "market_attention": True,
                    "prediction": {
                        "direction": "two_sided",
                        "horizon": "The first full trading session after the release",
                        "confidence": 0.62,
                        "falsification_conditions": [
                            "Guidance and gross margin both remain exactly within consensus ranges."
                        ],
                        "event_window": "First post-release trading session",
                    },
                    "impact_analysis": impact("earnings"),
                    "sources": [source(20)],
                }
            ]
        }
    )


def macro_data() -> GlobalMacroResponse:
    return GlobalMacroResponse.model_validate(
        {
            "event": {
                "global_event": "Central-bank guidance changes the expected policy path",
                "event_date": "2026-08-19",
                "market_impact": "high",
                "summary": (
                    "Officials supplied new guidance that moved sovereign yields and currencies."
                ),
                "affected_assets": ["stocks", "bonds", "usd", "commodities"],
                "impact_analysis": impact("macro"),
                "sources": [
                    source(
                        30,
                        publisher="Federal Reserve",
                        domain="www.federalreserve.gov",
                    )
                ],
            }
        }
    )


def research_data() -> ResearchDiscoveryResponse:
    return ResearchDiscoveryResponse.model_validate(
        {
            "discovery": {
                "research_title": "A new empirical result on information diffusion",
                "research_date": "2026-08-19",
                "simple_explanation": (
                    "The paper shows in plain terms how delayed information changes decisions "
                    "across connected groups."
                ),
                "key_insight": (
                    "Network position can matter as much as information quality for adoption speed."
                ),
                "applications": [
                    {
                        "category": "finance",
                        "application": "Estimate revenue durability from independent diffusion paths.",
                    },
                    {
                        "category": "investing",
                        "application": "Test adoption breadth before increasing portfolio exposure.",
                    },
                    {
                        "category": "business_strategy",
                        "application": "Map which customer networks independently validate a product.",
                    },
                    {
                        "category": "technology",
                        "application": "Build telemetry that distinguishes independent adoption paths.",
                    },
                ],
                "has_current_market_implication": False,
                "impact_analysis": None,
                "sources": [source(40, publisher="NBER", domain="www.nber.org")],
            }
        }
    )


def knowledge_data() -> KnowledgeRefreshResponse:
    return KnowledgeRefreshResponse.model_validate(
        {
            "knowledge": {
                "concept": "Bayesian updating",
                "historical_background": (
                    "The method developed from foundational work on conditional probability."
                ),
                "simple_explanation": (
                    "Start with a prior belief and revise it in proportion to how diagnostic "
                    "new evidence actually is."
                ),
                "why_it_still_matters_today": (
                    "Investors continuously combine incomplete evidence with existing expectations."
                ),
                "real_world_example": (
                    "An analyst updates a demand forecast after a reliable but noisy channel check."
                ),
            }
        }
    )


def provider_result(data: Any, section: ResearchSection, source_count: int) -> ProviderResult[Any]:
    return ProviderResult(
        data=data,
        accessed_at=NOW,
        metadata=ProviderRunMetadata(
            provider="openai",
            model="gpt-5.6-terra",
            section=section,
            attempts=1,
            duration_ms=12,
            request_id=f"req_{section.value}",
            source_count=source_count,
            usage=ProviderUsage(input_tokens=100, output_tokens=50, total_tokens=150),
        ),
    )


class FakeResearchProvider:
    def __init__(self, outcomes: dict[ResearchSection, Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[ResearchSection] = []

    def _result(self, section: ResearchSection) -> Any:
        self.calls.append(section)
        outcome = self.outcomes[section]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def market_news(self, request: Any) -> Any:
        return self._result(ResearchSection.MARKET_NEWS)

    def earnings(self, request: Any) -> Any:
        return self._result(ResearchSection.EARNINGS)

    def global_macro(self, request: Any) -> Any:
        return self._result(ResearchSection.GLOBAL_MACRO)

    def research_discovery(self, request: Any) -> Any:
        return self._result(ResearchSection.RESEARCH_DISCOVERY)

    def knowledge_refresh(self, request: Any) -> Any:
        return self._result(ResearchSection.KNOWLEDGE_REFRESH)


def open_market_context() -> ReportContext:
    market = MarketContext(
        report_date=date(2026, 8, 19),
        report_date_is_session=True,
        previous_session=date(2026, 8, 18),
        tomorrow_date=date(2026, 8, 20),
        tomorrow_is_session=True,
        next_open_session=date(2026, 8, 20),
        tomorrow_opens_at=datetime(2026, 8, 20, 13, 30, tzinfo=UTC),
        tomorrow_closes_at=datetime(2026, 8, 20, 20, 0, tzinfo=UTC),
    )
    return ReportContext(market_context=market, generated_at=NOW)


def closed_market_context() -> ReportContext:
    generated = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    market = MarketContext(
        report_date=date(2026, 8, 21),
        report_date_is_session=True,
        previous_session=date(2026, 8, 20),
        tomorrow_date=date(2026, 8, 22),
        tomorrow_is_session=False,
        next_open_session=date(2026, 8, 24),
        tomorrow_opens_at=None,
        tomorrow_closes_at=None,
    )
    return ReportContext(market_context=market, generated_at=generated)


def complete_outcomes(*, earnings_qualifying: bool = True) -> dict[ResearchSection, Any]:
    return {
        ResearchSection.MARKET_NEWS: provider_result(
            news_data(), ResearchSection.MARKET_NEWS, 3
        ),
        ResearchSection.EARNINGS: provider_result(
            earnings_data(qualifying=earnings_qualifying), ResearchSection.EARNINGS, 1
        ),
        ResearchSection.GLOBAL_MACRO: provider_result(
            macro_data(), ResearchSection.GLOBAL_MACRO, 1
        ),
        ResearchSection.RESEARCH_DISCOVERY: provider_result(
            research_data(), ResearchSection.RESEARCH_DISCOVERY, 1
        ),
        ResearchSection.KNOWLEDGE_REFRESH: provider_result(
            knowledge_data(), ResearchSection.KNOWLEDGE_REFRESH, 0
        ),
    }


def test_complete_open_market_report_has_required_sections_and_unavailable_metrics() -> None:
    provider = FakeResearchProvider(complete_outcomes())

    report = DailyReportPipeline(provider).generate(open_market_context())

    assert report.validation_status == "valid"
    assert report.report_id == "daily-market-report-2026-08-19"
    assert len(report.market_news) == 3
    assert report.market_news[0].computed_score >= report.market_news[-1].computed_score
    assert report.earnings.status == "available"
    candidate = report.earnings.candidates[0]
    assert candidate.sector == "Semiconductors"
    assert candidate.risk_level == "high"
    assert candidate.release_timing == "after_market"
    assert candidate.earnings_at is None
    assert candidate.prediction.event_window.anchor_basis == "market_window_proxy"
    assert candidate.current_price.value is None
    assert candidate.expected_eps.provenance == "unavailable"
    assert len(provider.calls) == 5
    assert len(report.provider_runs) == 5


def test_closed_market_skips_earnings_call_and_uses_exact_sentence() -> None:
    outcomes = complete_outcomes()
    news = outcomes[ResearchSection.MARKET_NEWS].data
    closed_day_news = news.model_copy(
        update={
            "candidates": [
                item.model_copy(update={"event_date": date(2026, 8, 20)})
                for item in news.candidates
            ]
        }
    )
    outcomes[ResearchSection.MARKET_NEWS] = provider_result(
        closed_day_news, ResearchSection.MARKET_NEWS, 3
    )
    provider = FakeResearchProvider(outcomes)

    report = DailyReportPipeline(provider).generate(closed_market_context())

    assert ResearchSection.EARNINGS not in provider.calls
    assert report.earnings.status == "market_closed"
    assert report.earnings.message == CLOSED_MARKET_MESSAGE
    assert report.earnings.candidates == []


def test_open_market_with_no_qualifying_candidates_is_not_closed_or_failed() -> None:
    provider = FakeResearchProvider(complete_outcomes(earnings_qualifying=False))

    report = DailyReportPipeline(provider).generate(open_market_context())

    assert report.earnings.status == "no_qualifying_candidates"
    assert report.earnings.message == NO_QUALIFYING_EARNINGS_MESSAGE
    assert report.validation_status == "valid"


def test_optional_failures_become_explicit_degraded_sections() -> None:
    outcomes = complete_outcomes()
    outcomes[ResearchSection.RESEARCH_DISCOVERY] = TransientProviderError(
        "safe research failure", section=ResearchSection.RESEARCH_DISCOVERY
    )
    outcomes[ResearchSection.KNOWLEDGE_REFRESH] = TransientProviderError(
        "safe knowledge failure", section=ResearchSection.KNOWLEDGE_REFRESH
    )
    provider = FakeResearchProvider(outcomes)

    report = DailyReportPipeline(provider).generate(open_market_context())

    assert report.validation_status == "degraded"
    assert report.research_discovery is None
    assert report.knowledge_refresh is None
    assert report.section_statuses.research_discovery.status == "unavailable"
    assert report.section_statuses.knowledge_refresh.status == "unavailable"
    assert len(report.warnings) == 2


def test_required_failure_stops_before_optional_calls() -> None:
    outcomes = complete_outcomes()
    outcomes[ResearchSection.GLOBAL_MACRO] = TransientProviderError(
        "safe macro failure", section=ResearchSection.GLOBAL_MACRO
    )
    provider = FakeResearchProvider(outcomes)

    with pytest.raises(TransientProviderError):
        DailyReportPipeline(provider).generate(open_market_context())

    assert ResearchSection.RESEARCH_DISCOVERY not in provider.calls
    assert ResearchSection.KNOWLEDGE_REFRESH not in provider.calls


def test_fewer_than_three_valid_news_items_fails_closed() -> None:
    outcomes = complete_outcomes()
    outcomes[ResearchSection.MARKET_NEWS] = provider_result(
        news_data(), ResearchSection.MARKET_NEWS, 3
    )
    # The response model has three items, but move one outside the allowed event window.
    news = outcomes[ResearchSection.MARKET_NEWS].data
    altered = news.model_copy(
        update={
            "candidates": [
                *news.candidates[:2],
                news.candidates[2].model_copy(update={"event_date": date(2026, 8, 1)}),
            ]
        }
    )
    outcomes[ResearchSection.MARKET_NEWS] = provider_result(
        altered, ResearchSection.MARKET_NEWS, 3
    )

    with pytest.raises(ReportValidationError):
        DailyReportPipeline(FakeResearchProvider(outcomes)).generate(open_market_context())


class LicensedMarketData:
    def get_earnings_metrics(
        self, tickers: list[str], *, target_date: date, as_of: datetime
    ) -> dict[str, EarningsMarketData]:
        del target_date
        evidence = MarketDataEvidence(
            evidence_id="src_vendor_acme",
            title="Licensed Acme consensus and delayed quote record",
            publisher="Licensed Data Vendor",
            tier=2,
            url="https://data.example.com/acme",
            published_at=as_of,
            accessed_at=as_of,
        )
        return {
            "ACME": EarningsMarketData(
                ticker="ACME",
                current_price=MarketMetric.known(
                    Decimal("123.45"),
                    unit="USD per share",
                    provider="Licensed Data Vendor",
                    source_evidence_id=evidence.evidence_id,
                    as_of=as_of,
                ),
                market_cap=MarketMetric.unavailable("Market capitalization is unavailable."),
                expected_eps=MarketMetric.unavailable("Consensus EPS is unavailable."),
                expected_revenue=MarketMetric.unavailable("Consensus revenue is unavailable."),
                evidence=(evidence,),
            )
        }


def test_numeric_value_enters_only_through_licensed_market_data_adapter() -> None:
    report = DailyReportPipeline(
        FakeResearchProvider(complete_outcomes()),
        market_data_provider=LicensedMarketData(),
    ).generate(open_market_context())

    metric = report.earnings.candidates[0].current_price
    assert metric.value == pytest.approx(123.45)
    assert metric.provenance == "licensed_market_data_provider"
    assert metric.license_confirmed is True
    assert metric.source_evidence_id in {
        item.evidence_id for item in report.earnings.candidates[0].sources
    }


def test_pipeline_policy_guards_total_section_call_count() -> None:
    with pytest.raises(ValueError):
        PipelinePolicy(max_section_calls=6)


def test_forced_rerun_versions_prediction_id_but_keeps_date_report_id() -> None:
    first = DailyReportPipeline(FakeResearchProvider(complete_outcomes())).generate(
        open_market_context()
    )
    original = open_market_context()
    later = original.generated_at + timedelta(minutes=5)
    second_context = ReportContext(
        market_context=original.market_context,
        generated_at=later,
        source_cutoff_at=later,
    )
    second = DailyReportPipeline(FakeResearchProvider(complete_outcomes())).generate(
        second_context
    )

    assert first.report_id == second.report_id
    assert (
        first.earnings.candidates[0].prediction.prediction_id
        != second.earnings.candidates[0].prediction.prediction_id
    )
