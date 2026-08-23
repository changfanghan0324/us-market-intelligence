"""Daily report orchestration and explicit partial-failure policy."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from market_intelligence.domain.models import (
    CLOSED_MARKET_MESSAGE,
    EARNINGS_CONFIRMED_EVENTS_MESSAGE,
    EARNINGS_DATA_UNAVAILABLE_MESSAGE,
    NO_CONFIRMED_EARNINGS_EVENTS_MESSAGE,
    NO_QUALIFYING_EARNINGS_MESSAGE,
    ConfirmedEarningsEvent,
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
    ResearchApplication,
    ResearchDiscovery,
    SectionState,
    SectionStatuses,
    SourcedMetric,
    SourceEvidence,
    build_report_id,
)
from market_intelligence.domain.models import (
    ProviderRunMetadata as CanonicalProviderRunMetadata,
)
from market_intelligence.domain.scoring import (
    earnings_selection_score,
    select_top_news,
)
from market_intelligence.errors import ReportValidationError
from market_intelligence.providers.base import (
    ProviderError,
    ProviderResult,
    ProviderRunMetadata,
    ResearchProvider,
    ResearchSection,
)
from market_intelligence.providers.market_data import (
    DisabledMarketDataProvider,
    EarningsMarketData,
    MarketDataEvidence,
    MarketDataProvider,
    MarketMetric,
    MarketMetricStatus,
)
from market_intelligence.providers.openai_research import (
    AIConfirmedEarningsEvent,
    AIEarningsCandidate,
    AIEvidence,
    AIGlobalMacroEvent,
    AIImpactAnalysis,
    AIKnowledgeRefresh,
    AIMarketNewsItem,
    AIResearchDiscovery,
    EarningsResponse,
    GlobalMacroResponse,
    KnowledgeRefreshResponse,
    MarketNewsResponse,
    ResearchDiscoveryResponse,
    ResearchRequest,
    source_id_for,
    source_publisher_for,
    source_tier,
)

_OFFICIAL_FEED_WARNING_LABELS = {
    "official_feed_unavailable_fed_press": "Federal Reserve press releases",
    "official_feed_unavailable_bls_latest": "BLS Latest Numbers",
    "official_feed_unavailable_sec_press": "SEC press releases",
    "official_feed_unavailable_fed_research": "Federal Reserve FEDS working papers",
    "official_document_unavailable": "one or more official release documents",
}
_EARNINGS_WARNING_LABELS = {
    "sec_earnings_search_unavailable": "SEC filing search",
    "sec_earnings_documents_incomplete": "one or more matching SEC filing documents",
}


@dataclass(frozen=True, slots=True)
class ReportContext:
    """Trusted, code-generated dates and cutoff supplied to the pipeline."""

    market_context: MarketContext
    generated_at: datetime
    source_cutoff_at: datetime | None = None
    timezone_name: Literal["America/New_York"] = "America/New_York"

    def __post_init__(self) -> None:
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        cutoff = self.source_cutoff_at or self.generated_at
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("source_cutoff_at must be timezone-aware")
        if cutoff > self.generated_at:
            raise ValueError("source cutoff cannot be after generated_at")
        object.__setattr__(self, "source_cutoff_at", cutoff)


@dataclass(frozen=True, slots=True)
class PipelinePolicy:
    top_news_count: Literal[3] = 3
    minimum_earnings_score: float = 7.0
    max_workers: int = 3
    max_section_calls: int = 5
    prediction_window_hours: int = 24
    earnings_calendar_available: bool = True
    market_news_lookback_days: int = 1
    global_macro_lookback_days: int = 30

    def __post_init__(self) -> None:
        if not isinstance(self.earnings_calendar_available, bool):
            raise TypeError("earnings calendar capability must be a boolean")
        if isinstance(self.market_news_lookback_days, bool) or not isinstance(
            self.market_news_lookback_days, int
        ):
            raise TypeError("market news lookback must be an integer")
        if not 1 <= self.market_news_lookback_days <= 30:
            raise ValueError("market news lookback must be from 1 to 30 days")
        if isinstance(self.global_macro_lookback_days, bool) or not isinstance(
            self.global_macro_lookback_days, int
        ):
            raise TypeError("global macro lookback must be an integer")
        if not 1 <= self.global_macro_lookback_days <= 90:
            raise ValueError("global macro lookback must be from 1 to 90 days")
        if self.top_news_count != 3:
            raise ValueError(
                "the publication contract requires exactly three news items"
            )
        if not 0 <= self.minimum_earnings_score <= 10:
            raise ValueError("minimum earnings score must be between zero and ten")
        if not 1 <= self.max_workers <= 3:
            raise ValueError("pipeline worker count must be between one and three")
        if not 3 <= self.max_section_calls <= 5:
            raise ValueError("pipeline section-call cap must be between three and five")
        if not 1 <= self.prediction_window_hours <= 168:
            raise ValueError("prediction window is outside the safety bound")


class DailyReportPipeline:
    """Build a canonical report while failing closed on required sections.

    Market news and global macro are always required. Earnings research is
    required only when tomorrow is an NYSE session and the configured mode has
    an earnings-calendar capability. A mode without that capability publishes
    an explicit data-unavailable panel and never treats missing data as an empty
    calendar. Research discovery and knowledge refresh are degradable.
    """

    def __init__(
        self,
        research_provider: ResearchProvider,
        market_data_provider: MarketDataProvider | None = None,
        policy: PipelinePolicy | None = None,
    ) -> None:
        self.research_provider = research_provider
        self.market_data_provider = market_data_provider or DisabledMarketDataProvider()
        self.policy = policy or PipelinePolicy()

    def generate(self, context: ReportContext) -> DailyReport:
        request = ResearchRequest(
            report_date=context.market_context.report_date,
            previous_session_date=context.market_context.previous_session,
            target_date=context.market_context.tomorrow_date,
            generated_at=cast(datetime, context.source_cutoff_at),
            timezone_name=context.timezone_name,
        )

        required_calls: dict[ResearchSection, Callable[[], ProviderResult[Any]]] = {
            ResearchSection.MARKET_NEWS: lambda: self.research_provider.market_news(
                request
            ),
            ResearchSection.GLOBAL_MACRO: lambda: self.research_provider.global_macro(
                request
            ),
        }
        if (
            context.market_context.tomorrow_is_session
            and self.policy.earnings_calendar_available
        ):
            required_calls[ResearchSection.EARNINGS] = lambda: (
                self.research_provider.earnings(request)
            )
        self._assert_call_cap(len(required_calls) + 2)
        required_results = self._run_phase(required_calls)
        self._raise_required_failure(required_results)

        optional_calls: dict[ResearchSection, Callable[[], ProviderResult[Any]]] = {
            ResearchSection.RESEARCH_DISCOVERY: (
                lambda: self.research_provider.research_discovery(request)
            ),
            ResearchSection.KNOWLEDGE_REFRESH: (
                lambda: self.research_provider.knowledge_refresh(request)
            ),
        }
        optional_results = self._run_phase(optional_calls)

        provider_runs: list[CanonicalProviderRunMetadata] = []
        warnings: list[str] = []

        news_result = cast(
            ProviderResult[MarketNewsResponse],
            required_results[ResearchSection.MARKET_NEWS],
        )
        news_items = self._market_news(news_result, context)
        news_coverage_warning = _official_feed_coverage_warning(news_result.metadata)
        _append_warning(warnings, news_coverage_warning)
        provider_runs.append(
            _canonical_provider_run(
                news_result.metadata,
                expected_section=ResearchSection.MARKET_NEWS,
            )
        )

        macro_result = cast(
            ProviderResult[GlobalMacroResponse],
            required_results[ResearchSection.GLOBAL_MACRO],
        )
        global_macro = self._global_macro(macro_result, context)
        macro_coverage_warning = _official_feed_coverage_warning(macro_result.metadata)
        _append_warning(warnings, macro_coverage_warning)
        provider_runs.append(
            _canonical_provider_run(
                macro_result.metadata,
                expected_section=ResearchSection.GLOBAL_MACRO,
            )
        )

        if (
            context.market_context.tomorrow_is_session
            and self.policy.earnings_calendar_available
        ):
            earnings_result = cast(
                ProviderResult[EarningsResponse],
                required_results[ResearchSection.EARNINGS],
            )
            earnings = self._earnings(earnings_result, context)
            earnings_coverage_warning = _earnings_coverage_warning(
                earnings_result.metadata
            )
            _append_warning(warnings, earnings_coverage_warning)
            provider_runs.append(
                _canonical_provider_run(
                    earnings_result.metadata,
                    expected_section=ResearchSection.EARNINGS,
                )
            )
        elif context.market_context.tomorrow_is_session:
            earnings = EarningsSection(
                target_date=context.market_context.tomorrow_date,
                universe_coverage="unavailable",
                status="data_unavailable",
                candidates=[],
                confirmed_events=[],
                message=EARNINGS_DATA_UNAVAILABLE_MESSAGE,
            )
            warnings.append(EARNINGS_DATA_UNAVAILABLE_MESSAGE)
            provider_runs.append(
                CanonicalProviderRunMetadata(
                    provider="deterministic policy",
                    section=ResearchSection.EARNINGS.value,
                    status="skipped",
                    attempts=0,
                    duration_ms=0,
                    source_count=0,
                )
            )
        else:
            earnings = EarningsSection(
                target_date=context.market_context.tomorrow_date,
                universe_coverage="not_applicable",
                status="market_closed",
                candidates=[],
                confirmed_events=[],
                message=CLOSED_MARKET_MESSAGE,
                next_open_session=context.market_context.next_open_session,
            )
            provider_runs.append(
                CanonicalProviderRunMetadata(
                    provider="deterministic policy",
                    section=ResearchSection.EARNINGS.value,
                    status="skipped",
                    attempts=0,
                    duration_ms=0,
                    source_count=0,
                )
            )

        research, research_state, research_run, research_warning = (
            self._optional_research(
                optional_results[ResearchSection.RESEARCH_DISCOVERY],
                context,
                fallback_provider=news_result.metadata.provider,
            )
        )
        provider_runs.append(research_run)
        if research_warning:
            _append_warning(warnings, research_warning)

        knowledge, knowledge_state, knowledge_run, knowledge_warning = (
            self._optional_knowledge(
                optional_results[ResearchSection.KNOWLEDGE_REFRESH],
                fallback_provider=news_result.metadata.provider,
            )
        )
        provider_runs.append(knowledge_run)
        if knowledge_warning:
            _append_warning(warnings, knowledge_warning)

        news_window_disclosure = (
            "This run may use genuine source releases from the prior "
            f"{self.policy.market_news_lookback_days} calendar days to fill "
            "the Top 3 when fewer same-session items are available; every "
            "card shows its event date."
            if self.policy.market_news_lookback_days > 1
            else None
        )
        macro_window_disclosure = (
            "This run considers genuine source releases from the prior "
            f"{self.policy.global_macro_lookback_days} calendar days for Global "
            "Macro; the selected event date remains visible."
        )

        section_statuses = SectionStatuses(
            market_news=SectionState(
                status="degraded" if news_coverage_warning else "available",
                detail=_join_disclosures(
                    news_window_disclosure,
                    news_coverage_warning,
                ),
            ),
            global_macro=SectionState(
                status="degraded" if macro_coverage_warning else "available",
                detail=_join_disclosures(
                    macro_window_disclosure,
                    macro_coverage_warning,
                ),
            ),
            earnings=(
                SectionState(
                    status="degraded",
                    detail=(
                        EARNINGS_DATA_UNAVAILABLE_MESSAGE
                        if earnings.status == "data_unavailable"
                        else earnings_coverage_warning
                    ),
                )
                if earnings.status == "data_unavailable"
                or (
                    context.market_context.tomorrow_is_session
                    and self.policy.earnings_calendar_available
                    and earnings_coverage_warning is not None
                )
                else SectionState(status="available")
            ),
            research_discovery=research_state,
            knowledge_refresh=knowledge_state,
        )
        validation_status: Literal["valid", "degraded"] = (
            "degraded" if warnings else "valid"
        )

        try:
            return DailyReport(
                report_id=build_report_id(context.market_context.report_date),
                report_date=context.market_context.report_date,
                generated_at=context.generated_at,
                timezone=context.timezone_name,
                source_cutoff_at=cast(datetime, context.source_cutoff_at),
                market_context=context.market_context,
                market_news=news_items,
                earnings=earnings,
                global_macro=global_macro,
                research_discovery=research,
                knowledge_refresh=knowledge,
                section_statuses=section_statuses,
                provider_runs=provider_runs,
                validation_status=validation_status,
                warnings=warnings,
            )
        except ValidationError as exc:
            raise ReportValidationError(
                "canonical report did not pass publication validation"
            ) from exc

    def _run_phase(
        self,
        calls: Mapping[ResearchSection, Callable[[], ProviderResult[Any]]],
    ) -> dict[ResearchSection, ProviderResult[Any] | Exception]:
        results: dict[ResearchSection, ProviderResult[Any] | Exception] = {}
        with ThreadPoolExecutor(
            max_workers=min(self.policy.max_workers, len(calls))
        ) as pool:
            futures = {pool.submit(call): section for section, call in calls.items()}
            for future in as_completed(futures):
                section = futures[future]
                try:
                    results[section] = future.result()
                except Exception as exc:  # noqa: BLE001 - handled by section policy
                    results[section] = exc
        return results

    @staticmethod
    def _raise_required_failure(
        results: Mapping[ResearchSection, ProviderResult[Any] | Exception],
    ) -> None:
        for section in (
            ResearchSection.MARKET_NEWS,
            ResearchSection.GLOBAL_MACRO,
            ResearchSection.EARNINGS,
        ):
            value = results.get(section)
            if isinstance(value, Exception):
                if isinstance(value, ProviderError):
                    raise value
                raise ReportValidationError(
                    f"required section {section.value} failed safely"
                ) from value

    def _market_news(
        self,
        result: ProviderResult[MarketNewsResponse],
        context: ReportContext,
    ) -> list[MarketNewsItem]:
        valid: list[MarketNewsItem] = []
        minimum_event_date = min(
            context.market_context.previous_session,
            context.market_context.report_date
            - timedelta(days=self.policy.market_news_lookback_days),
        )
        for item in result.data.candidates:
            if not (
                minimum_event_date
                <= item.event_date
                <= context.market_context.report_date
            ):
                continue
            try:
                valid.append(_market_news_item(item, result.accessed_at))
            except (ValidationError, ValueError):
                continue
        selected = select_top_news(valid, count=self.policy.top_news_count)
        return cast(list[MarketNewsItem], selected)

    def _global_macro(
        self,
        result: ProviderResult[GlobalMacroResponse],
        context: ReportContext,
    ) -> GlobalMacroEvent:
        item = result.data.event
        minimum_event_date = context.market_context.report_date - timedelta(
            days=self.policy.global_macro_lookback_days
        )
        if (
            not minimum_event_date
            <= item.event_date
            <= context.market_context.report_date
        ):
            raise ReportValidationError(
                "global macro event date is outside the configured source window"
            )
        try:
            return _global_macro_event(item, result.accessed_at)
        except (ValidationError, ValueError) as exc:
            raise ReportValidationError(
                "global macro did not pass canonical validation"
            ) from exc

    def _earnings(
        self,
        result: ProviderResult[EarningsResponse],
        context: ReportContext,
    ) -> EarningsSection:
        target_date = context.market_context.tomorrow_date
        if any(
            event.target_date != target_date for event in result.data.confirmed_events
        ):
            raise ReportValidationError(
                "confirmed earnings research returned the wrong target date"
            )
        confirmed_events: list[ConfirmedEarningsEvent] = []
        for event in result.data.confirmed_events:
            try:
                confirmed_events.append(
                    _confirmed_earnings_event(event, result.accessed_at)
                )
            except (ValidationError, ValueError):
                continue
        correct_date = [
            candidate
            for candidate in result.data.candidates
            if candidate.earnings_date == target_date
        ]
        if result.data.candidates and not correct_date:
            raise ReportValidationError(
                "earnings research returned the wrong target date"
            )

        qualifying: list[tuple[AIEarningsCandidate, EarningsSelectionComponents]] = []
        for candidate in correct_date:
            selection = _earnings_selection(candidate)
            if (
                candidate.market_attention
                and earnings_selection_score(selection)
                >= self.policy.minimum_earnings_score
            ):
                qualifying.append((candidate, selection))

        if not qualifying:
            if confirmed_events:
                return EarningsSection(
                    target_date=target_date,
                    universe_coverage="bounded_research",
                    status="confirmed_events_available",
                    candidates=[],
                    confirmed_events=confirmed_events,
                    message=EARNINGS_CONFIRMED_EVENTS_MESSAGE,
                )
            if result.metadata.provider == "official_public_sources":
                return EarningsSection(
                    target_date=target_date,
                    universe_coverage="bounded_research",
                    status="no_confirmed_events_in_bounded_scan",
                    candidates=[],
                    confirmed_events=[],
                    message=NO_CONFIRMED_EARNINGS_EVENTS_MESSAGE,
                )
            return EarningsSection(
                target_date=target_date,
                universe_coverage="bounded_research",
                status="no_qualifying_candidates",
                candidates=[],
                confirmed_events=[],
                message=NO_QUALIFYING_EARNINGS_MESSAGE,
            )

        metric_data = self.market_data_provider.get_earnings_metrics(
            [candidate.ticker for candidate, _ in qualifying],
            target_date=target_date,
            as_of=cast(datetime, context.source_cutoff_at),
        )
        candidates: list[EarningsCandidate] = []
        for candidate, selection in qualifying:
            snapshot = metric_data.get(candidate.ticker)
            if snapshot is None:
                snapshot = _unavailable_market_data(candidate.ticker)
            try:
                candidates.append(
                    _earnings_candidate(
                        candidate,
                        selection=selection,
                        market_data=snapshot,
                        accessed_at=result.accessed_at,
                        context=context,
                        prediction_window_hours=self.policy.prediction_window_hours,
                    )
                )
            except (ValidationError, ValueError):
                continue

        if not candidates:
            raise ReportValidationError(
                "qualifying earnings candidates did not pass canonical validation"
            )
        candidates.sort(
            key=lambda candidate: (-candidate.computed_score, candidate.ticker)
        )
        return EarningsSection(
            target_date=target_date,
            universe_coverage="bounded_research",
            status="available",
            candidates=candidates,
            confirmed_events=confirmed_events,
        )

    def _optional_research(
        self,
        outcome: ProviderResult[Any] | Exception,
        context: ReportContext,
        *,
        fallback_provider: str,
    ) -> tuple[
        ResearchDiscovery | None,
        SectionState,
        CanonicalProviderRunMetadata,
        str | None,
    ]:
        warning = (
            "Research discovery is unavailable because its provider output did not pass "
            "the publication boundary."
        )
        if isinstance(outcome, Exception):
            attempts = outcome.attempts if isinstance(outcome, ProviderError) else 0
            return (
                None,
                SectionState(status="unavailable", detail=warning),
                (
                    _failed_provider_run(
                        ResearchSection.RESEARCH_DISCOVERY,
                        provider=fallback_provider,
                        attempts=attempts,
                    )
                ),
                warning,
            )
        result = cast(ProviderResult[ResearchDiscoveryResponse], outcome)
        try:
            value = _research_discovery(
                result.data.discovery, result.accessed_at, context
            )
        except (ValidationError, ValueError):
            return (
                None,
                SectionState(status="unavailable", detail=warning),
                (
                    _failed_provider_run(
                        ResearchSection.RESEARCH_DISCOVERY,
                        provider=result.metadata.provider,
                        attempts=result.metadata.attempts,
                        duration_ms=result.metadata.duration_ms,
                    )
                ),
                warning,
            )
        coverage_warning = _official_feed_coverage_warning(result.metadata)
        return (
            value,
            SectionState(
                status="degraded" if coverage_warning else "available",
                detail=coverage_warning,
            ),
            (
                _canonical_provider_run(
                    result.metadata,
                    expected_section=ResearchSection.RESEARCH_DISCOVERY,
                )
            ),
            coverage_warning,
        )

    @staticmethod
    def _optional_knowledge(
        outcome: ProviderResult[Any] | Exception,
        *,
        fallback_provider: str,
    ) -> tuple[
        KnowledgeRefresh | None,
        SectionState,
        CanonicalProviderRunMetadata,
        str | None,
    ]:
        warning = (
            "Knowledge refresh is unavailable because its provider output did not pass "
            "the publication boundary."
        )
        if isinstance(outcome, Exception):
            attempts = outcome.attempts if isinstance(outcome, ProviderError) else 0
            return (
                None,
                SectionState(status="unavailable", detail=warning),
                (
                    _failed_provider_run(
                        ResearchSection.KNOWLEDGE_REFRESH,
                        provider=fallback_provider,
                        attempts=attempts,
                    )
                ),
                warning,
            )
        result = cast(ProviderResult[KnowledgeRefreshResponse], outcome)
        try:
            value = _knowledge_refresh(result.data.knowledge)
        except (ValidationError, ValueError):
            return (
                None,
                SectionState(status="unavailable", detail=warning),
                (
                    _failed_provider_run(
                        ResearchSection.KNOWLEDGE_REFRESH,
                        provider=result.metadata.provider,
                        attempts=result.metadata.attempts,
                        duration_ms=result.metadata.duration_ms,
                    )
                ),
                warning,
            )
        coverage_warning = _official_feed_coverage_warning(result.metadata)
        return (
            value,
            SectionState(
                status="degraded" if coverage_warning else "available",
                detail=coverage_warning,
            ),
            (
                _canonical_provider_run(
                    result.metadata,
                    expected_section=ResearchSection.KNOWLEDGE_REFRESH,
                )
            ),
            coverage_warning,
        )

    def _assert_call_cap(self, call_count: int) -> None:
        if call_count > self.policy.max_section_calls:
            raise ReportValidationError("configured section-call cap would be exceeded")


def _market_news_item(item: AIMarketNewsItem, accessed_at: datetime) -> MarketNewsItem:
    return MarketNewsItem(
        news_id=_stable_id("news", item.event_date.isoformat(), item.title),
        title=item.title,
        event_date=item.event_date,
        market_impact=item.market_impact,
        affected_sectors=item.affected_sectors,
        summary=item.summary,
        bull_case=item.bullish_case,
        bear_case=item.bearish_case,
        attention=NewsAttentionComponents(**item.attention_components.model_dump()),
        impact_analysis=_impact_analysis(item.impact_analysis),
        sources=[_source_evidence(source, accessed_at) for source in item.sources],
    )


def _earnings_selection(candidate: AIEarningsCandidate) -> EarningsSelectionComponents:
    return EarningsSelectionComponents(**candidate.selection_components.model_dump())


def _confirmed_earnings_event(
    event: AIConfirmedEarningsEvent,
    accessed_at: datetime,
) -> ConfirmedEarningsEvent:
    return ConfirmedEarningsEvent(
        event_id=_stable_id(
            "earnings_event",
            event.ticker,
            event.target_date.isoformat(),
            event.confirmation_basis,
            event.sources[0].url,
        ),
        ticker=event.ticker,
        company_name=event.company_name,
        cik=event.cik,
        form=event.form,
        announced_on=event.filing_date,
        target_date=event.target_date,
        confirmation_basis=event.confirmation_basis,
        release_timing=event.release_timing,
        scheduled_release_at=event.scheduled_release_at,
        conference_call_at=event.conference_call_at,
        confirmation_summary=event.confirmation_summary,
        sources=[_source_evidence(source, accessed_at) for source in event.sources],
    )


def _earnings_candidate(
    candidate: AIEarningsCandidate,
    *,
    selection: EarningsSelectionComponents,
    market_data: EarningsMarketData,
    accessed_at: datetime,
    context: ReportContext,
    prediction_window_hours: int,
) -> EarningsCandidate:
    ai_sources = [_source_evidence(source, accessed_at) for source in candidate.sources]
    market_sources = [_market_data_source(source) for source in market_data.evidence]
    sources = _deduplicate_sources([*ai_sources, *market_sources])
    evidence_ids = [source.evidence_id for source in ai_sources]
    earnings_at = _confirmed_earnings_at(candidate, context)
    window_anchor = _reaction_window_anchor(candidate, context, earnings_at)
    direction_map = {
        "bullish": "up",
        "bearish": "down",
        "neutral": "flat",
        "two_sided": "mixed",
    }
    prediction = Prediction(
        prediction_id=_stable_id(
            "pred",
            candidate.ticker,
            candidate.earnings_date.isoformat(),
            cast(datetime, context.source_cutoff_at).isoformat(),
        ),
        direction=direction_map[candidate.prediction.direction],
        horizon=candidate.prediction.horizon,
        confidence=candidate.prediction.confidence,
        falsification_conditions=candidate.prediction.falsification_conditions,
        event_window=EventWindow(
            starts_at=window_anchor,
            ends_at=window_anchor + timedelta(hours=prediction_window_hours),
            benchmark="S&P 500 Index",
            anchor_basis=(
                "confirmed_release_time"
                if earnings_at is not None
                else "market_window_proxy"
            ),
        ),
        evidence_ids=evidence_ids,
    )
    return EarningsCandidate(
        ticker=candidate.ticker,
        company_name=candidate.company_name,
        sector=candidate.sector,
        release_timing=candidate.release_timing,
        earnings_at=earnings_at,
        current_price=_sourced_metric(market_data.current_price, "USD per share"),
        market_cap=_sourced_metric(market_data.market_cap, "USD"),
        expected_eps=_sourced_metric(market_data.expected_eps, "USD per share"),
        expected_revenue=_sourced_metric(market_data.expected_revenue, "USD"),
        selection=selection,
        market_attention=candidate.market_attention,
        why_important=candidate.why_important,
        bull_case=candidate.bullish_case,
        base_case=candidate.base_case,
        bear_case=candidate.bearish_case,
        risk_level=candidate.risk_level,
        risk=f"{candidate.risk_level.title()} risk. {candidate.risk_analysis}",
        prediction=prediction,
        impact_analysis=_impact_analysis(candidate.impact_analysis),
        sources=sources,
    )


def _global_macro_event(
    item: AIGlobalMacroEvent, accessed_at: datetime
) -> GlobalMacroEvent:
    return GlobalMacroEvent(
        event_id=_stable_id("macro", item.event_date.isoformat(), item.global_event),
        title=item.global_event,
        event_date=item.event_date,
        market_impact=item.market_impact,
        summary=item.summary,
        affected_asset_classes=item.affected_assets,
        impact_analysis=_impact_analysis(item.impact_analysis),
        sources=[_source_evidence(source, accessed_at) for source in item.sources],
    )


def _research_discovery(
    item: AIResearchDiscovery,
    accessed_at: datetime,
    context: ReportContext,
) -> ResearchDiscovery:
    minimum_date = context.market_context.report_date - timedelta(days=90)
    if not minimum_date <= item.research_date <= context.market_context.report_date:
        raise ValueError("research date must be within the last 90 calendar days")
    return ResearchDiscovery(
        discovery_id=_stable_id(
            "research", item.research_date.isoformat(), item.research_title
        ),
        title=item.research_title,
        discovered_on=item.research_date,
        plain_explanation=item.simple_explanation,
        changed_belief=item.key_insight,
        applications=[
            ResearchApplication(
                category=application.category,
                application=application.application,
            )
            for application in item.applications
        ],
        impact_analysis=(
            _impact_analysis(item.impact_analysis)
            if item.impact_analysis is not None
            else None
        ),
        sources=[_source_evidence(source, accessed_at) for source in item.sources],
    )


def _knowledge_refresh(item: AIKnowledgeRefresh) -> KnowledgeRefresh:
    return KnowledgeRefresh(
        concept=item.concept,
        background=item.historical_background,
        explanation=item.simple_explanation,
        current_relevance=item.why_it_still_matters_today,
        example=item.real_world_example,
    )


def _impact_analysis(item: AIImpactAnalysis) -> ImpactAnalysis:
    beneficiaries = []
    for actor in item.beneficiaries:
        if actor.kind == "none_identified":
            beneficiaries.append(
                NoneIdentifiedActor(kind="none_identified", rationale=actor.rationale)
            )
        else:
            beneficiaries.append(
                NamedMarketActor(
                    kind="named_entity", name=actor.name, rationale=actor.rationale
                )
            )
    losers = []
    for actor in item.losers:
        if actor.kind == "none_identified":
            losers.append(
                NoneIdentifiedActor(kind="none_identified", rationale=actor.rationale)
            )
        else:
            losers.append(
                NamedMarketActor(
                    kind="named_entity", name=actor.name, rationale=actor.rationale
                )
            )
    return ImpactAnalysis(
        what_changed=item.what_changed,
        why_it_matters=item.why_it_matters,
        beneficiaries=beneficiaries,
        losers=losers,
        professional_investor_reaction=item.professional_investor_reaction,
        indicators_to_monitor_next=item.indicators_to_monitor_next,
    )


def _source_evidence(source: AIEvidence, accessed_at: datetime) -> SourceEvidence:
    return SourceEvidence(
        evidence_id=source_id_for(source),
        title=source.title,
        publisher=source_publisher_for(source),
        tier=source_tier(source),
        url=source.url,
        published_at=source.published_at,
        accessed_at=accessed_at,
        evidence_role=source.evidence_role,
        quoted_fragment=source.quoted_fragment,
    )


def _market_data_source(source: MarketDataEvidence) -> SourceEvidence:
    return SourceEvidence(
        evidence_id=source.evidence_id,
        title=source.title,
        publisher=source.publisher,
        tier=source.tier,
        url=source.url,
        published_at=source.published_at,
        accessed_at=source.accessed_at,
        evidence_role=source.evidence_role,
    )


def _sourced_metric(metric: MarketMetric, unit: str) -> SourcedMetric:
    if metric.status is MarketMetricStatus.UNAVAILABLE:
        return SourcedMetric.unavailable(
            unit,
            reason=metric.unavailable_reason
            or "Unavailable because the licensed market-data adapter returned no value.",
        )
    if metric.value is None:
        raise ValueError("known market metric has no value")
    return SourcedMetric(
        value=float(metric.value),
        unit=metric.unit or unit,
        as_of=metric.as_of,
        provider=metric.provider,
        provenance="licensed_market_data_provider",
        source_evidence_id=metric.source_evidence_id,
        license_confirmed=True,
        unavailable_reason=None,
    )


def _confirmed_earnings_at(
    candidate: AIEarningsCandidate,
    context: ReportContext,
) -> datetime | None:
    if candidate.scheduled_release_at is None:
        return None
    value = candidate.scheduled_release_at
    local_date = value.astimezone(ZoneInfo(context.timezone_name)).date()
    if local_date != context.market_context.tomorrow_date:
        raise ValueError("scheduled earnings time is outside the target date")
    return value.astimezone(UTC)


def _reaction_window_anchor(
    candidate: AIEarningsCandidate,
    context: ReportContext,
    confirmed_time: datetime | None,
) -> datetime:
    if confirmed_time is not None:
        return confirmed_time
    if candidate.release_timing in {"before_market", "during_market"}:
        value = context.market_context.tomorrow_opens_at
    else:
        value = context.market_context.tomorrow_closes_at
    if value is None:
        raise ValueError("open-session earnings candidate requires session times")
    return value


def _unavailable_market_data(ticker: str) -> EarningsMarketData:
    reason = "No licensed market-data value was returned for this company."

    def metric() -> MarketMetric:
        return MarketMetric.unavailable(reason)

    return EarningsMarketData(
        ticker=ticker,
        current_price=metric(),
        market_cap=metric(),
        expected_eps=metric(),
        expected_revenue=metric(),
    )


def _deduplicate_sources(sources: list[SourceEvidence]) -> list[SourceEvidence]:
    unique: dict[str, SourceEvidence] = {}
    for source in sources:
        unique.setdefault(source.evidence_id, source)
    return list(unique.values())


def _canonical_provider_run(
    metadata: ProviderRunMetadata,
    *,
    expected_section: ResearchSection,
) -> CanonicalProviderRunMetadata:
    if metadata.section != expected_section:
        raise ReportValidationError(
            "provider metadata section does not match the invoked section"
        )
    warning_codes = _validated_warning_codes(
        metadata,
        expected_section=expected_section,
    )
    request_ids = [
        normalized
        for value in metadata.request_ids
        if (normalized := _canonical_request_id(value)) is not None
    ]
    return CanonicalProviderRunMetadata(
        provider=metadata.provider,
        model=metadata.model,
        section=metadata.section.value,
        status=("degraded" if warning_codes else "success"),
        attempts=metadata.attempts,
        duration_ms=metadata.duration_ms,
        source_count=metadata.source_count,
        request_id=_canonical_request_id(metadata.request_id),
        request_ids=list(dict.fromkeys(request_ids)),
        warning_codes=list(warning_codes),
        input_tokens=metadata.usage.input_tokens,
        output_tokens=metadata.usage.output_tokens,
        web_search_calls=metadata.usage.web_search_calls,
    )


def _validated_warning_codes(
    metadata: ProviderRunMetadata,
    *,
    expected_section: ResearchSection,
) -> tuple[str, ...]:
    codes = tuple(dict.fromkeys(metadata.warning_codes))
    allowed = (
        _EARNINGS_WARNING_LABELS
        if expected_section == ResearchSection.EARNINGS
        else _OFFICIAL_FEED_WARNING_LABELS
    )
    if any(code not in allowed for code in codes):
        raise ReportValidationError(
            "provider returned an unknown warning code for the invoked section"
        )
    return codes


def _official_feed_warning_codes(metadata: ProviderRunMetadata) -> tuple[str, ...]:
    codes = tuple(
        dict.fromkeys(
            code for code in metadata.warning_codes if code.startswith("official_")
        )
    )
    unknown = [code for code in codes if code not in _OFFICIAL_FEED_WARNING_LABELS]
    if unknown:
        raise ReportValidationError(
            "provider returned an unknown official feed warning"
        )
    return codes


def _official_feed_coverage_warning(
    metadata: ProviderRunMetadata,
) -> str | None:
    labels = list(
        dict.fromkeys(
            _OFFICIAL_FEED_WARNING_LABELS[code]
            for code in _official_feed_warning_codes(metadata)
        )
    )
    if not labels:
        return None
    return (
        "Official source coverage is degraded after bounded retries. "
        f"Unavailable source material: {'; '.join(labels)}."
    )


def _earnings_coverage_warning(metadata: ProviderRunMetadata) -> str | None:
    codes = tuple(
        dict.fromkeys(
            code for code in metadata.warning_codes if code.startswith("sec_earnings_")
        )
    )
    unknown = [code for code in codes if code not in _EARNINGS_WARNING_LABELS]
    if unknown:
        raise ReportValidationError("provider returned an unknown SEC earnings warning")
    if not codes:
        return None
    labels = "; ".join(_EARNINGS_WARNING_LABELS[code] for code in codes)
    return (
        "Bounded SEC earnings coverage is degraded because the following source "
        f"material was unavailable: {labels}. This is not a complete market calendar."
    )


def _append_warning(warnings: list[str], warning: str | None) -> None:
    if warning is not None and warning not in warnings:
        warnings.append(warning)


def _join_disclosures(*values: str | None) -> str | None:
    present = [value for value in values if value]
    return " ".join(present) if present else None


def _failed_provider_run(
    section: ResearchSection,
    *,
    provider: str,
    attempts: int = 0,
    duration_ms: int = 0,
) -> CanonicalProviderRunMetadata:
    return CanonicalProviderRunMetadata(
        provider=provider,
        section=section.value,
        status="degraded",
        attempts=attempts,
        duration_ms=duration_ms,
        source_count=0,
    )


def _canonical_request_id(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"[^A-Za-z0-9_.:]", "_", value)[:100]
    return normalized if len(normalized) >= 3 else None


def _stable_id(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:20]}"


__all__ = ["DailyReportPipeline", "PipelinePolicy", "ReportContext"]
