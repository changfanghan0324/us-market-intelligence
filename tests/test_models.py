from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from market_intelligence.domain.models import (
    CLOSED_MARKET_MESSAGE,
    EARNINGS_CONFIRMED_EVENTS_MESSAGE,
    EARNINGS_DATA_UNAVAILABLE_MESSAGE,
    NO_QUALIFYING_EARNINGS_MESSAGE,
    DailyReport,
    EarningsCandidate,
    EarningsSection,
    EarningsSelectionComponents,
    EventWindow,
    GlobalMacroEvent,
    ImpactAnalysis,
    KnowledgeRefresh,
    MarketContext,
    MarketNewsItem,
    NamedMarketActor,
    NewsAttentionComponents,
    NoneIdentifiedActor,
    Prediction,
    ResearchDiscovery,
    SectionState,
    SectionStatuses,
    SourcedMetric,
    SourceEvidence,
)
from market_intelligence.domain.validation import validate_source_domains
from market_intelligence.errors import EvidenceValidationError

NOW = datetime(2026, 8, 21, 12, 15, tzinfo=UTC)


def evidence(evidence_id: str = "src_sec_filing") -> SourceEvidence:
    return SourceEvidence(
        evidence_id=evidence_id,
        title="Issuer filing and operating update",
        publisher="U.S. Securities and Exchange Commission",
        tier=1,
        url="https://www.sec.gov/Archives/example",
        published_at=datetime(2026, 8, 20, 10, 0, tzinfo=UTC),
        accessed_at=NOW,
        evidence_role="primary",
        quoted_fragment="Revenue increased during the quarter.",
    )


def impact() -> ImpactAnalysis:
    return ImpactAnalysis(
        what_changed=(
            "The issuer raised its full-year demand outlook after reporting stronger orders."
        ),
        why_it_matters=(
            "Higher forward demand changes expected cash flows and may reset sector valuations."
        ),
        beneficiaries=[
            NamedMarketActor(
                name="US semiconductor equipment sector",
                rationale="Order visibility supports revenue expectations across named suppliers.",
            )
        ],
        losers=[
            NoneIdentifiedActor(
                rationale=(
                    "No direct losing entity is supported by the currently available evidence."
                )
            )
        ],
        professional_investor_reaction=(
            "Portfolio managers may raise exposure selectively while hedging valuation risk."
        ),
        indicators_to_monitor_next=[
            "Monitor order growth, cancellation rates, and management guidance revisions."
        ],
    )


def prediction(source_id: str = "src_sec_filing") -> Prediction:
    return Prediction(
        prediction_id="pred_example",
        direction="up",
        horizon="one session",
        confidence=0.65,
        falsification_conditions=[
            "The thesis fails if management cuts guidance during the earnings call."
        ],
        event_window=EventWindow(
            starts_at=datetime(2026, 8, 21, 20, 0, tzinfo=UTC),
            ends_at=datetime(2026, 8, 24, 20, 0, tzinfo=UTC),
            benchmark="SPY ETF",
        ),
        evidence_ids=[source_id],
    )


def selection(score: float = 7.0) -> EarningsSelectionComponents:
    return EarningsSelectionComponents(
        business_quality=score,
        revenue_growth=score,
        earnings_surprise_potential=score,
        market_expectation_gap=score,
        risk_reward_asymmetry=score,
    )


def earnings_candidate(
    *, score: float = 7.0, attention: bool = True
) -> EarningsCandidate:
    source = evidence()
    return EarningsCandidate(
        ticker="EXM",
        company_name="Example Semiconductor Inc.",
        sector="Semiconductor equipment",
        why_important=(
            "Its results provide a timely read on capital spending and advanced-node demand."
        ),
        release_timing="after_market",
        earnings_at=datetime(2026, 8, 21, 20, 5, tzinfo=UTC),
        selection=selection(score),
        market_attention=attention,
        bull_case="Orders accelerate and management raises its forward revenue guidance materially.",
        base_case="Results meet consensus while management reiterates its current annual outlook.",
        bear_case="Customers delay projects and management lowers near-term revenue expectations.",
        risk_level="medium",
        risk="Elevated valuation leaves the shares sensitive to even a modest guidance miss.",
        prediction=prediction(),
        impact_analysis=impact(),
        sources=[source],
    )


def news(news_id: str, title: str) -> MarketNewsItem:
    return MarketNewsItem(
        news_id=news_id,
        title=title,
        event_date=date(2026, 8, 21),
        market_impact="high",
        affected_sectors=["Semiconductor equipment"],
        summary=(
            "New order disclosures indicate resilient spending despite earlier demand concerns."
        ),
        bull_case="Stronger order momentum could support estimate increases across the supply chain.",
        bear_case="The disclosed orders may be temporary and could reverse as customers normalize.",
        attention=NewsAttentionComponents(
            market_size_impact=8.0,
            earnings_impact=8.0,
            macro_importance=6.0,
            sector_influence=8.0,
            short_term_trading_relevance=8.0,
        ),
        impact_analysis=impact(),
        sources=[evidence(f"src_{news_id}")],
    )


def macro() -> GlobalMacroEvent:
    return GlobalMacroEvent(
        event_id="macro_policy",
        title="Policy expectations shift after official release",
        event_date=date(2026, 8, 21),
        market_impact="high",
        summary=(
            "The official release changed the expected policy path across global rate markets."
        ),
        affected_asset_classes=["Stocks", "Bonds", "USD", "Commodities"],
        impact_analysis=impact(),
        sources=[evidence("src_macro")],
    )


def test_impact_analysis_rejects_padding_and_generic_actors() -> None:
    base = impact().model_dump(exclude={"computed_score"})
    base["why_it_matters"] = base["what_changed"]
    with pytest.raises(ValidationError, match="distinct"):
        ImpactAnalysis.model_validate(base)

    base = impact().model_dump()
    base["beneficiaries"] = [
        {
            "kind": "named_entity",
            "name": "investors",
            "rationale": "The group could receive a direct benefit from higher expected returns.",
        }
    ]
    with pytest.raises(ValidationError, match="named company"):
        ImpactAnalysis.model_validate(base)


def test_none_identified_is_typed_and_cannot_pad_named_entities() -> None:
    base = impact().model_dump()
    base["losers"] = [
        {
            "kind": "none_identified",
            "rationale": "No losing entity is supported by the evidence available at cutoff time.",
        },
        {
            "kind": "named_entity",
            "name": "Legacy memory suppliers",
            "rationale": "The shift could reduce pricing power for older technology suppliers.",
        },
    ]
    with pytest.raises(ValidationError, match="only actor"):
        ImpactAnalysis.model_validate(base)


def test_six_answers_enforce_minimum_substance() -> None:
    base = impact().model_dump()
    base["what_changed"] = "Rates rose."
    with pytest.raises(ValidationError):
        ImpactAnalysis.model_validate(base)


def test_six_answers_accept_concise_traditional_chinese() -> None:
    base = impact().model_dump()
    base["what_changed"] = "政策預期明顯轉向"
    base["why_it_matters"] = "折現率下降重估風險資產"
    base["professional_investor_reaction"] = "機構調整久期與風險曝險"
    base["indicators_to_monitor_next"] = ["兩年期殖利率", "期貨定價"]

    validated = ImpactAnalysis.model_validate(base)

    assert validated.what_changed == "政策預期明顯轉向"
    assert validated.indicators_to_monitor_next == ["兩年期殖利率", "期貨定價"]
    base = impact().model_dump()
    base["indicators_to_monitor_next"] = []
    with pytest.raises(ValidationError):
        ImpactAnalysis.model_validate(base)


def test_bidi_and_control_characters_are_stripped() -> None:
    source = evidence().model_copy(
        update={"title": "Issuer\u202e filing\x00 and operating update"}
    )
    # model_copy deliberately skips validation; canonical re-validation strips it.
    validated = SourceEvidence.model_validate(source.model_dump())
    assert "\u202e" not in validated.title
    assert "\x00" not in validated.title


def test_model_authored_numeric_market_data_is_rejected() -> None:
    with pytest.raises(ValidationError, match="provenance"):
        SourcedMetric(
            value=123.0,
            unit="USD",
            as_of=NOW,
            provider="OpenAI model",
            provenance="model",  # type: ignore[arg-type]
            source_evidence_id="src_sec_filing",
            license_confirmed=True,
            unavailable_reason=None,
        )


def test_known_numeric_market_data_requires_license_and_attached_evidence() -> None:
    with pytest.raises(ValidationError, match="license"):
        SourcedMetric(
            value=123.0,
            unit="USD",
            as_of=NOW,
            provider="Example Data Feed",
            provenance="licensed_market_data_provider",
            source_evidence_id="src_sec_filing",
            license_confirmed=False,
            unavailable_reason=None,
        )

    metric = SourcedMetric(
        value=123.0,
        unit="USD",
        as_of=NOW,
        provider="Example Data Feed",
        provenance="licensed_market_data_provider",
        source_evidence_id="src_not_attached",
        license_confirmed=True,
        unavailable_reason=None,
    )
    payload = earnings_candidate().model_dump(exclude={"computed_score"})
    payload["current_price"] = metric.model_dump()
    with pytest.raises(ValidationError, match="not attached"):
        EarningsCandidate.model_validate(payload)


def test_unconfigured_metrics_have_explicit_unavailable_representation() -> None:
    candidate = earnings_candidate()
    assert candidate.current_price.value is None
    assert candidate.current_price.provenance == "unavailable"
    assert candidate.current_price.unavailable_reason == (
        "Unavailable (no licensed provider configured)"
    )


def test_computed_score_cannot_be_injected() -> None:
    payload = earnings_candidate().model_dump(exclude={"computed_score"})
    payload["computed_score"] = 10.0
    with pytest.raises(ValidationError, match="code-computed"):
        EarningsCandidate.model_validate(payload)


def test_earnings_states_are_distinct_and_closed_copy_is_exact() -> None:
    closed = EarningsSection(
        target_date=date(2026, 8, 22),
        universe_coverage="not_applicable",
        status="market_closed",
        message=CLOSED_MARKET_MESSAGE,
        next_open_session=date(2026, 8, 24),
    )
    assert closed.candidates == []
    no_candidates = EarningsSection(
        target_date=date(2026, 8, 21),
        universe_coverage="bounded_research",
        status="no_qualifying_candidates",
        message=NO_QUALIFYING_EARNINGS_MESSAGE,
        next_open_session=date(2026, 8, 21),
    )
    assert no_candidates.status != closed.status
    data_unavailable = EarningsSection(
        target_date=date(2026, 8, 21),
        universe_coverage="unavailable",
        status="data_unavailable",
        message=EARNINGS_DATA_UNAVAILABLE_MESSAGE,
    )
    assert data_unavailable.candidates == []
    assert data_unavailable.status not in {
        closed.status,
        no_candidates.status,
    }
    with pytest.raises(ValidationError, match="explicitly unavailable"):
        EarningsSection(
            target_date=date(2026, 8, 21),
            universe_coverage="bounded_research",
            status="data_unavailable",
            message=EARNINGS_DATA_UNAVAILABLE_MESSAGE,
        )
    with pytest.raises(ValidationError, match="transparent fixed message"):
        EarningsSection(
            target_date=date(2026, 8, 21),
            universe_coverage="unavailable",
            status="data_unavailable",
            message="No companies are reporting tomorrow.",
        )
    with pytest.raises(ValidationError, match="authoritative"):
        EarningsSection(
            target_date=date(2026, 8, 22),
            universe_coverage="not_applicable",
            status="market_closed",
            message="The market is closed tomorrow for the weekend.",
        )


def test_available_earnings_enforces_score_and_attention_boundary() -> None:
    EarningsSection(
        target_date=date(2026, 8, 21),
        universe_coverage="bounded_research",
        status="available",
        candidates=[earnings_candidate(score=7.0)],
    )
    with pytest.raises(ValidationError, match="pass score"):
        EarningsSection(
            target_date=date(2026, 8, 21),
            universe_coverage="bounded_research",
            status="available",
            candidates=[earnings_candidate(score=6.99)],
        )
    with pytest.raises(ValidationError, match="pass score"):
        EarningsSection(
            target_date=date(2026, 8, 21),
            universe_coverage="bounded_research",
            status="available",
            candidates=[earnings_candidate(score=9.0, attention=False)],
        )


def test_summary_cannot_duplicate_why_it_matters() -> None:
    payload = news("news_one", "First evidence-backed event").model_dump(
        exclude={"computed_score"}
    )
    payload["summary"] = payload["impact_analysis"]["why_it_matters"]
    with pytest.raises(ValidationError, match="substantively different"):
        MarketNewsItem.model_validate(payload)


def test_daily_report_enforces_required_and_degradable_sections() -> None:
    context = MarketContext(
        report_date=date(2026, 8, 20),
        report_date_is_session=True,
        previous_session=date(2026, 8, 19),
        tomorrow_date=date(2026, 8, 21),
        tomorrow_is_session=True,
        next_open_session=date(2026, 8, 21),
        tomorrow_opens_at=datetime(2026, 8, 21, 13, 30, tzinfo=UTC),
        tomorrow_closes_at=datetime(2026, 8, 21, 20, 0, tzinfo=UTC),
    )
    report = DailyReport(
        report_id="daily-market-report-2026-08-20",
        report_date=date(2026, 8, 20),
        generated_at=datetime(2026, 8, 20, 12, 15, tzinfo=UTC),
        source_cutoff_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        market_context=context,
        market_news=[
            news("news_one", "First evidence-backed market event"),
            news("news_two", "Second evidence-backed market event"),
            news("news_three", "Third evidence-backed market event"),
        ],
        earnings=EarningsSection(
            target_date=date(2026, 8, 21),
            universe_coverage="bounded_research",
            status="no_qualifying_candidates",
            message=NO_QUALIFYING_EARNINGS_MESSAGE,
            next_open_session=date(2026, 8, 21),
        ),
        global_macro=macro(),
        research_discovery=None,
        knowledge_refresh=KnowledgeRefresh(
            concept="Duration risk",
            background="Bond duration describes sensitivity to changes in market interest rates.",
            explanation="Longer-duration cash flows usually move more when discount rates change.",
            current_relevance="Rate-path uncertainty keeps duration exposure important for portfolios.",
            example="A long-duration growth stock can fall sharply when real yields move higher.",
        ),
        section_statuses=SectionStatuses(
            market_news=SectionState(status="available"),
            global_macro=SectionState(status="available"),
            research_discovery=SectionState(
                status="unavailable",
                detail="Research discovery was unavailable after bounded provider retries.",
            ),
            knowledge_refresh=SectionState(status="available"),
        ),
        validation_status="degraded",
        warnings=["Research discovery is unavailable for this report."],
    )
    assert report.validation_status == "degraded"
    round_trip = DailyReport.model_validate_json(report.model_dump_json())
    assert round_trip.report_id == report.report_id
    assert (
        round_trip.market_news[0].computed_score == report.market_news[0].computed_score
    )
    coverage_warning = (
        "Official source coverage is degraded. This feed was unavailable after "
        "bounded retries: BLS Latest Numbers."
    )
    degraded_payload = report.model_dump(exclude={"computed_score"})
    degraded_payload["section_statuses"]["market_news"] = {
        "status": "degraded",
        "detail": coverage_warning,
    }
    degraded_payload["warnings"].append(coverage_warning)
    degraded_payload["provider_runs"] = [
        {
            "provider": "official public sources",
            "section": "market_news",
            "status": "degraded",
            "attempts": 2,
            "duration_ms": 10,
            "source_count": 3,
            "warning_codes": ["official_feed_unavailable_bls_latest"],
        }
    ]
    degraded_required = DailyReport.model_validate(degraded_payload)
    assert degraded_required.section_statuses.market_news.status == "degraded"

    missing_warning = deepcopy(degraded_payload)
    missing_warning["warnings"].remove(coverage_warning)
    with pytest.raises(ValidationError, match="matching warning"):
        DailyReport.model_validate(missing_warning)

    mismatched_run = deepcopy(degraded_payload)
    mismatched_run["provider_runs"][0]["status"] = "success"
    with pytest.raises(ValidationError, match="degraded provider metadata"):
        DailyReport.model_validate(mismatched_run)

    duplicate_warning = deepcopy(degraded_payload)
    duplicate_warning["warnings"].append(coverage_warning)
    with pytest.raises(ValidationError, match="cannot be duplicated"):
        DailyReport.model_validate(duplicate_warning)

    payload = report.model_dump(exclude={"computed_score"})
    payload["section_statuses"]["global_macro"] = {
        "status": "unavailable",
        "detail": "Global macro could not be researched safely.",
    }
    with pytest.raises(ValidationError, match="required section"):
        DailyReport.model_validate(payload)

    payload = report.model_dump(exclude={"computed_score"})
    payload["market_news"][0]["sources"][0]["published_at"] = datetime(
        2026, 8, 20, 12, 1, tzinfo=UTC
    )
    with pytest.raises(ValidationError, match="source cutoff"):
        DailyReport.model_validate(payload)

    confirmed_source = evidence("src_confirmed_event").model_dump()
    confirmed_source["published_at"] = datetime(2026, 8, 20, 12, 1, tzinfo=UTC)
    confirmed_event = {
        "event_id": "earnings_event_example",
        "ticker": "EXM",
        "company_name": "Example Semiconductor Inc.",
        "cik": "0000000001",
        "form": "8-K",
        "announced_on": date(2026, 8, 20),
        "target_date": date(2026, 8, 21),
        "confirmation_basis": "scheduled_results_release",
        "release_timing": "after_market",
        "scheduled_release_at": datetime(2026, 8, 21, 20, 5, tzinfo=UTC),
        "conference_call_at": None,
        "confirmation_summary": (
            "The issuer filing confirms a results release after the market close."
        ),
        "sources": [confirmed_source],
    }
    confirmed_payload = report.model_dump(exclude={"computed_score"})
    confirmed_payload["earnings"] = {
        "target_date": date(2026, 8, 21),
        "universe_coverage": "bounded_research",
        "status": "confirmed_events_available",
        "candidates": [],
        "confirmed_events": [confirmed_event],
        "message": EARNINGS_CONFIRMED_EVENTS_MESSAGE,
        "next_open_session": date(2026, 8, 21),
    }
    with pytest.raises(ValidationError, match="source cutoff"):
        DailyReport.model_validate(confirmed_payload)

    confirmed_source["published_at"] = datetime(2026, 8, 20, 11, 59, tzinfo=UTC)
    confirmed_source["url"] = "https://evil.example/phish"
    report_with_unapproved_event_source = DailyReport.model_validate(confirmed_payload)
    with pytest.raises(
        EvidenceValidationError, match="outside the configured allowlist"
    ):
        validate_source_domains(
            report_with_unapproved_event_source,
            allowed_domains=("sec.gov", "reuters.com"),
        )


def test_unknown_fields_and_coercion_are_forbidden() -> None:
    with pytest.raises(ValidationError):
        SourceEvidence(
            evidence_id="src_example",
            title="Issuer filing and operating update",
            publisher="Issuer relations",
            tier="1",  # type: ignore[arg-type]
            url="https://example.com/filing",
            accessed_at=NOW,
            evidence_role="primary",
            unexpected="not allowed",  # type: ignore[call-arg]
        )


def test_research_impact_is_optional_only_without_market_claim() -> None:
    discovery = ResearchDiscovery(
        discovery_id="research_example",
        title="A robust portfolio construction method",
        discovered_on=date(2026, 8, 21),
        plain_explanation="The method balances forecast strength against estimation uncertainty.",
        changed_belief="It shows why the highest point estimate need not receive the largest weight.",
        applications=[
            {
                "category": "finance",
                "application": "Use the method when sizing correlated financing exposures.",
            },
            {
                "category": "investing",
                "application": "Use the method when sizing exposures across equity factors.",
            },
            {
                "category": "business_strategy",
                "application": "Balance strategic forecasts against estimation uncertainty.",
            },
            {
                "category": "technology",
                "application": "Apply uncertainty-aware weights in automated decision systems.",
            },
        ],
        impact_analysis=None,
        sources=[evidence("src_research")],
    )
    assert discovery.impact_analysis is None
