"""Strict canonical models for data that may reach the public report.

The AI-facing response schemas intentionally live outside this module.  In
particular, market prices, market capitalisation, and consensus estimates can
enter these models only after enrichment by a licensed market-data adapter.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, date, datetime, timedelta
from difflib import SequenceMatcher
from typing import Annotated, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
    model_validator,
)

CLOSED_MARKET_MESSAGE = (
    "Tomorrow US market is closed. No earnings trading analysis available."
)
NO_QUALIFYING_EARNINGS_MESSAGE = (
    "US market is open tomorrow, but no candidates in the configured bounded "
    "research universe met the selection threshold and market-attention requirement."
)
UNLICENSED_METRIC_MESSAGE = "Unavailable (no licensed provider configured)"

_BIDI_OR_INVISIBLE = frozenset(
    {
        "\u061c",
        "\u200b",
        "\u200c",
        "\u200d",
        "\u200e",
        "\u200f",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2060",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
        "\ufeff",
    }
)
_FORBIDDEN_PROVIDER_WORDS = re.compile(
    r"(?:openai|chatgpt|anthropic|claude|gemini|language[ -]?model|\bllm\b)",
    re.IGNORECASE,
)
_GENERIC_ACTORS = frozenset(
    {
        "beneficiaries",
        "companies",
        "consumers",
        "investors",
        "losers",
        "market participants",
        "markets",
        "people",
        "shareholders",
        "traders",
        "公司",
        "受益者",
        "市場",
        "市場參與者",
        "投資人",
        "投資者",
        "消費者",
        "股東",
        "交易員",
        "輸家",
    }
)


def validate_named_market_actor_name(value: str) -> str:
    """Require a concrete entity name at every model boundary."""
    if value.casefold() in _GENERIC_ACTORS:
        raise ValueError("use a named company, sector, country, asset, or institution")
    if not any(character.isalnum() for character in value):
        raise ValueError("named actor must contain an entity name")
    return value


def sanitize_untrusted_text(value: object) -> object:
    """Remove control, bidi, and invisible characters from external text."""

    if not isinstance(value, str):
        return value
    normalized = unicodedata.normalize("NFKC", value)
    cleaned = "".join(
        character
        for character in normalized
        if character not in _BIDI_OR_INVISIBLE
        and not (
            unicodedata.category(character) in {"Cc", "Cf"}
            and character not in {"\n", "\t"}
        )
    )
    return cleaned.strip()


def _semantic_units(value: str) -> int:
    """Measure substance without penalizing information-dense CJK text."""

    return sum(
        2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
        for character in value
        if character.isalnum()
    )


def _require_narrative_substance(value: str) -> str:
    if _semantic_units(value) < 12:
        raise ValueError("narrative text is too short to be meaningful")
    return value


def _require_compact_substance(value: str) -> str:
    if _semantic_units(value) < 6:
        raise ValueError("compact text is too short to be meaningful")
    return value


ShortText = Annotated[
    str,
    BeforeValidator(sanitize_untrusted_text),
    StringConstraints(strict=True, strip_whitespace=True, min_length=2, max_length=200),
]
Identifier = Annotated[
    str,
    BeforeValidator(sanitize_untrusted_text),
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=3,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
NarrativeText = Annotated[
    str,
    BeforeValidator(sanitize_untrusted_text),
    StringConstraints(strict=True, strip_whitespace=True, min_length=2, max_length=2_500),
    AfterValidator(_require_narrative_substance),
]
NewsSummary = Annotated[
    str,
    BeforeValidator(sanitize_untrusted_text),
    StringConstraints(strict=True, strip_whitespace=True, min_length=2, max_length=100),
    AfterValidator(_require_narrative_substance),
]
CompactNarrative = Annotated[
    str,
    BeforeValidator(sanitize_untrusted_text),
    StringConstraints(strict=True, strip_whitespace=True, min_length=2, max_length=600),
    AfterValidator(_require_compact_substance),
]
OptionalNarrative = Annotated[
    str,
    BeforeValidator(sanitize_untrusted_text),
    StringConstraints(strict=True, strip_whitespace=True, min_length=2, max_length=1_000),
    AfterValidator(_require_compact_substance),
]
Score = Annotated[float, Field(strict=True, ge=0.0, le=10.0, allow_inf_nan=False)]
Confidence = Annotated[float, Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False)]
MetricNumber = Annotated[float, Field(strict=True, allow_inf_nan=False)]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _normalized_for_similarity(value: str) -> str:
    return "".join(
        character.casefold()
        for character in unicodedata.normalize("NFKC", value)
        if character.isalnum()
    )


def _too_similar(first: str, second: str, *, threshold: float = 0.90) -> bool:
    left = _normalized_for_similarity(first)
    right = _normalized_for_similarity(second)
    if not left or not right:
        return False
    if left == right:
        return True
    return SequenceMatcher(None, left, right).ratio() >= threshold


def _generated_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class StrictModel(BaseModel):
    """Base model that blocks coercion and unknown/publicly unsafe fields."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class SourceEvidence(StrictModel):
    evidence_id: Identifier = Field(default_factory=lambda: _generated_id("src"))
    title: ShortText
    publisher: ShortText
    tier: Literal[1, 2]
    url: HttpUrl
    published_at: datetime | None = None
    accessed_at: datetime
    evidence_role: Literal["primary", "corroborating", "context"]
    quoted_fragment: Annotated[
        str,
        BeforeValidator(sanitize_untrusted_text),
        StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=160),
    ] | None = None

    @field_validator("published_at", "accessed_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @field_validator("url")
    @classmethod
    def url_is_safe_https(cls, value: HttpUrl) -> HttpUrl:
        host = (value.host or "").rstrip(".").casefold()
        if value.scheme != "https":
            raise ValueError("evidence URL must use https")
        if value.username or value.password:
            raise ValueError("evidence URL cannot contain user information")
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            raise ValueError("local evidence hosts are not allowed")
        if value.port not in {None, 443}:
            raise ValueError("nonstandard evidence URL ports are not allowed")
        return value

    @field_validator("quoted_fragment")
    @classmethod
    def quote_is_only_a_short_fragment(cls, value: str | None) -> str | None:
        if value is not None and len(value.split()) > 25:
            raise ValueError("quoted fragment must contain no more than 25 words")
        return value

    @model_validator(mode="after")
    def publication_order_is_plausible(self) -> SourceEvidence:
        if self.published_at and self.published_at > self.accessed_at + timedelta(minutes=5):
            raise ValueError("published_at cannot be later than accessed_at")
        return self


class NamedMarketActor(StrictModel):
    kind: Literal["named_entity"] = "named_entity"
    name: ShortText
    rationale: CompactNarrative

    @field_validator("name")
    @classmethod
    def actor_is_named_not_generic(cls, value: str) -> str:
        return validate_named_market_actor_name(value)


class NoneIdentifiedActor(StrictModel):
    kind: Literal["none_identified"] = "none_identified"
    rationale: NarrativeText


type ImpactActor = Annotated[
    NamedMarketActor | NoneIdentifiedActor,
    Field(discriminator="kind"),
]


class ImpactAnalysis(StrictModel):
    """The required six-question analysis, with padding controls."""

    what_changed: NarrativeText
    why_it_matters: NarrativeText
    beneficiaries: Annotated[list[ImpactActor], Field(min_length=1, max_length=8)]
    losers: Annotated[list[ImpactActor], Field(min_length=1, max_length=8)]
    professional_investor_reaction: NarrativeText
    indicators_to_monitor_next: Annotated[
        list[CompactNarrative], Field(min_length=1, max_length=10)
    ]

    @field_validator("beneficiaries", "losers")
    @classmethod
    def sentinel_cannot_pad_named_actors(cls, value: list[ImpactActor]) -> list[ImpactActor]:
        sentinel_count = sum(actor.kind == "none_identified" for actor in value)
        if sentinel_count and len(value) != 1:
            raise ValueError("none_identified must be the only actor in its list")
        names = [
            actor.name.casefold()
            for actor in value
            if isinstance(actor, NamedMarketActor)
        ]
        if len(names) != len(set(names)):
            raise ValueError("named actors cannot be duplicated")
        return value

    @field_validator("indicators_to_monitor_next")
    @classmethod
    def indicators_are_unique(cls, value: list[str]) -> list[str]:
        normalized = [_normalized_for_similarity(indicator) for indicator in value]
        if len(normalized) != len(set(normalized)):
            raise ValueError("indicators to monitor cannot be duplicated")
        return value

    @model_validator(mode="after")
    def answers_are_distinct(self) -> ImpactAnalysis:
        if _too_similar(self.what_changed, self.why_it_matters):
            raise ValueError("what_changed and why_it_matters must be distinct analyses")
        if _too_similar(self.why_it_matters, self.professional_investor_reaction):
            raise ValueError(
                "why_it_matters and professional_investor_reaction must be distinct"
            )
        return self


class EventWindow(StrictModel):
    starts_at: datetime
    ends_at: datetime
    benchmark: ShortText
    anchor_basis: Literal["confirmed_release_time", "market_window_proxy"] = (
        "confirmed_release_time"
    )

    @field_validator("starts_at", "ends_at")
    @classmethod
    def event_times_are_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def ends_after_start(self) -> EventWindow:
        if self.ends_at <= self.starts_at:
            raise ValueError("event window must end after it starts")
        return self


class Prediction(StrictModel):
    prediction_id: Identifier = Field(default_factory=lambda: _generated_id("pred"))
    direction: Literal["up", "down", "flat", "mixed"]
    horizon: ShortText
    confidence: Confidence
    falsification_conditions: Annotated[
        list[CompactNarrative], Field(min_length=1, max_length=8)
    ]
    event_window: EventWindow
    evidence_ids: Annotated[list[Identifier], Field(min_length=1, max_length=20)]

    @field_validator("evidence_ids")
    @classmethod
    def evidence_ids_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("prediction evidence IDs must be unique")
        return value


class SourcedMetric(StrictModel):
    """Licensed numeric market data or an explicit unavailable value.

    `provenance` is deliberately a closed literal.  Values attributed to a model
    cannot validate and therefore cannot cross into the public domain object.
    """

    value: MetricNumber | None = None
    unit: ShortText
    as_of: datetime | None = None
    provider: ShortText | None = None
    provenance: Literal["licensed_market_data_provider", "unavailable"] = "unavailable"
    source_evidence_id: Identifier | None = None
    license_confirmed: bool = False
    unavailable_reason: OptionalNarrative | None = UNLICENSED_METRIC_MESSAGE

    @classmethod
    def unavailable(
        cls,
        unit: str,
        reason: str = UNLICENSED_METRIC_MESSAGE,
    ) -> SourcedMetric:
        if reason.strip().casefold() in {
            "no licensed market-data provider configured.",
            "no licensed market data provider configured.",
        }:
            reason = UNLICENSED_METRIC_MESSAGE
        return cls(unit=unit, unavailable_reason=reason)

    @field_validator("as_of")
    @classmethod
    def metric_timestamp_is_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def provenance_matches_value(self) -> SourcedMetric:
        if self.value is None:
            if self.provenance != "unavailable":
                raise ValueError("a missing numeric value must have unavailable provenance")
            if self.unavailable_reason is None:
                raise ValueError("an unavailable metric requires a reason")
            if any((self.as_of, self.provider, self.source_evidence_id)):
                raise ValueError("an unavailable metric cannot claim provider provenance")
            if self.license_confirmed:
                raise ValueError("an unavailable metric cannot claim a display license")
            return self

        if self.provenance != "licensed_market_data_provider":
            raise ValueError("numeric market data cannot have model provenance")
        if not self.license_confirmed:
            raise ValueError("numeric market data requires a confirmed public-display license")
        if self.as_of is None or self.provider is None or self.source_evidence_id is None:
            raise ValueError("numeric market data requires provider, as_of, and source evidence")
        if self.unavailable_reason is not None:
            raise ValueError("an available metric cannot include an unavailable reason")
        if _FORBIDDEN_PROVIDER_WORDS.search(self.provider):
            raise ValueError("an AI model cannot be a market-data provider")
        return self


class NewsAttentionComponents(StrictModel):
    market_size_impact: Score
    earnings_impact: Score
    macro_importance: Score
    sector_influence: Score
    short_term_trading_relevance: Score


class EarningsSelectionComponents(StrictModel):
    business_quality: Score
    revenue_growth: Score
    earnings_surprise_potential: Score
    market_expectation_gap: Score
    risk_reward_asymmetry: Score


class MarketNewsItem(StrictModel):
    news_id: Identifier = Field(default_factory=lambda: _generated_id("news"))
    title: ShortText
    event_date: date
    market_impact: Literal["high", "medium", "low"]
    affected_sectors: Annotated[list[ShortText], Field(min_length=1, max_length=12)]
    summary: NewsSummary
    bull_case: NarrativeText
    bear_case: NarrativeText
    attention: NewsAttentionComponents
    computed_score: Annotated[
        float, Field(strict=True, ge=0.0, le=10.0, allow_inf_nan=False)
    ] | None = None
    impact_analysis: ImpactAnalysis
    sources: Annotated[list[SourceEvidence], Field(min_length=1, max_length=20)]

    @model_validator(mode="after")
    def score_is_computed_not_trusted(self) -> MarketNewsItem:
        from market_intelligence.domain.scoring import news_attention_score

        expected = news_attention_score(self.attention)
        if self.computed_score is not None and self.computed_score != expected:
            raise ValueError("model-provided news total does not match code-computed score")
        object.__setattr__(self, "computed_score", expected)
        return self

    @model_validator(mode="after")
    def analysis_is_not_summary_padding(self) -> MarketNewsItem:
        if _too_similar(self.summary, self.impact_analysis.why_it_matters):
            raise ValueError("summary and why_it_matters must be substantively different")
        return self


class EarningsCandidate(StrictModel):
    ticker: Annotated[
        str,
        BeforeValidator(sanitize_untrusted_text),
        StringConstraints(
            strict=True,
            strip_whitespace=True,
            to_upper=True,
            min_length=1,
            max_length=10,
            pattern=r"^[A-Z][A-Z0-9.-]*$",
        ),
    ]
    company_name: ShortText
    sector: ShortText
    why_important: NarrativeText
    release_timing: Literal[
        "before_market",
        "during_market",
        "after_market",
        "time_not_confirmed",
    ]
    earnings_at: datetime | None = None
    current_price: SourcedMetric = Field(
        default_factory=lambda: SourcedMetric.unavailable("USD per share")
    )
    market_cap: SourcedMetric = Field(
        default_factory=lambda: SourcedMetric.unavailable("USD")
    )
    expected_eps: SourcedMetric = Field(
        default_factory=lambda: SourcedMetric.unavailable("USD per share")
    )
    expected_revenue: SourcedMetric = Field(
        default_factory=lambda: SourcedMetric.unavailable("USD")
    )
    selection: EarningsSelectionComponents
    computed_score: Annotated[
        float, Field(strict=True, ge=0.0, le=10.0, allow_inf_nan=False)
    ] | None = None
    market_attention: bool
    bull_case: NarrativeText
    base_case: NarrativeText
    bear_case: NarrativeText
    risk_level: Literal["high", "medium", "low"]
    risk: NarrativeText
    prediction: Prediction
    impact_analysis: ImpactAnalysis
    sources: Annotated[list[SourceEvidence], Field(min_length=1, max_length=20)]

    @field_validator("earnings_at")
    @classmethod
    def earnings_time_is_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def release_time_is_not_invented(self) -> EarningsCandidate:
        if self.release_timing == "time_not_confirmed" and self.earnings_at is not None:
            raise ValueError("an unconfirmed earnings time cannot claim an exact timestamp")
        expected_basis = (
            "confirmed_release_time"
            if self.earnings_at is not None
            else "market_window_proxy"
        )
        if self.prediction.event_window.anchor_basis != expected_basis:
            raise ValueError("prediction window basis does not match release-time confidence")
        return self

    @model_validator(mode="after")
    def score_is_computed_not_trusted(self) -> EarningsCandidate:
        from market_intelligence.domain.scoring import earnings_selection_score

        expected = earnings_selection_score(self.selection)
        if self.computed_score is not None and self.computed_score != expected:
            raise ValueError("model-provided earnings total does not match code-computed score")
        object.__setattr__(self, "computed_score", expected)
        return self

    @model_validator(mode="after")
    def numeric_and_prediction_sources_exist(self) -> EarningsCandidate:
        source_ids = {source.evidence_id for source in self.sources}
        for metric_name in (
            "current_price",
            "market_cap",
            "expected_eps",
            "expected_revenue",
        ):
            metric = getattr(self, metric_name)
            if metric.value is not None and metric.source_evidence_id not in source_ids:
                raise ValueError(f"{metric_name} source evidence is not attached")
        missing_prediction_sources = set(self.prediction.evidence_ids) - source_ids
        if missing_prediction_sources:
            raise ValueError("prediction references unattached evidence")
        if self.current_price.value is not None and self.current_price.value < 0:
            raise ValueError("current price cannot be negative")
        if self.market_cap.value is not None and self.market_cap.value < 0:
            raise ValueError("market cap cannot be negative")
        if self.expected_revenue.value is not None and self.expected_revenue.value < 0:
            raise ValueError("expected revenue cannot be negative")
        return self


class EarningsSection(StrictModel):
    target_date: date
    universe_coverage: Literal[
        "not_applicable",
        "bounded_research",
        "authoritative_full_calendar",
    ]
    status: Literal[
        "available",
        "market_closed",
        "no_qualifying_candidates",
        "unavailable",
    ]
    candidates: Annotated[list[EarningsCandidate], Field(max_length=20)] = Field(
        default_factory=list
    )
    message: OptionalNarrative | None = None
    next_open_session: date | None = None

    @model_validator(mode="after")
    def state_has_unambiguous_copy_and_data(self) -> EarningsSection:
        if self.status == "market_closed":
            if self.universe_coverage != "not_applicable":
                raise ValueError("closed-market earnings coverage must be not applicable")
            if self.candidates:
                raise ValueError("a closed market cannot have an earnings watchlist")
            if self.message != CLOSED_MARKET_MESSAGE:
                raise ValueError("closed market must use the authoritative sentence")
            if self.next_open_session is not None and self.next_open_session <= self.target_date:
                raise ValueError("next open session must follow the closed target date")
        elif self.status == "no_qualifying_candidates":
            if self.universe_coverage == "not_applicable":
                raise ValueError("open-market earnings requires a coverage classification")
            if self.candidates:
                raise ValueError("no-qualifying state cannot contain candidates")
            if self.message != NO_QUALIFYING_EARNINGS_MESSAGE:
                raise ValueError("no-qualifying state must use its dedicated message")
        elif self.status == "available":
            if self.universe_coverage == "not_applicable":
                raise ValueError("available earnings requires a coverage classification")
            if not self.candidates:
                raise ValueError("available earnings section requires candidates")
            if self.message is not None:
                raise ValueError("available earnings section cannot carry unavailable copy")
            if any(
                candidate.computed_score < 7.0 or not candidate.market_attention
                for candidate in self.candidates
            ):
                raise ValueError("published earnings candidates must pass score and attention")
            for candidate in self.candidates:
                if candidate.earnings_at is not None:
                    local_date = candidate.earnings_at.astimezone(
                        ZoneInfo("America/New_York")
                    ).date()
                    if local_date != self.target_date:
                        raise ValueError("confirmed earnings time must match the target date")
        else:
            if self.universe_coverage == "not_applicable":
                raise ValueError("open-market earnings requires a coverage classification")
            if self.candidates:
                raise ValueError("unavailable earnings section cannot contain candidates")
            if self.message is None:
                raise ValueError("unavailable earnings section requires a reason")
        return self


class GlobalMacroEvent(StrictModel):
    event_id: Identifier = Field(default_factory=lambda: _generated_id("macro"))
    title: ShortText
    event_date: date
    market_impact: Literal["high", "medium", "low"]
    summary: NarrativeText
    affected_asset_classes: Annotated[list[ShortText], Field(min_length=1, max_length=12)]
    impact_analysis: ImpactAnalysis
    sources: Annotated[list[SourceEvidence], Field(min_length=1, max_length=20)]

    @field_validator("affected_asset_classes")
    @classmethod
    def covers_required_asset_classes(cls, value: list[str]) -> list[str]:
        normalized = {item.casefold() for item in value}
        required = {"stocks", "bonds", "usd", "commodities"}
        if not required.issubset(normalized):
            raise ValueError(
                "global macro must cover stocks, bonds, USD, and commodities"
            )
        return value

    @model_validator(mode="after")
    def analysis_is_not_summary_padding(self) -> GlobalMacroEvent:
        if _too_similar(self.summary, self.impact_analysis.why_it_matters):
            raise ValueError("summary and why_it_matters must be substantively different")
        return self


class ResearchApplication(StrictModel):
    category: Literal["finance", "investing", "business_strategy", "technology"]
    application: CompactNarrative


class ResearchDiscovery(StrictModel):
    discovery_id: Identifier = Field(default_factory=lambda: _generated_id("research"))
    title: ShortText
    discovered_on: date
    plain_explanation: NarrativeText
    changed_belief: NarrativeText
    applications: Annotated[list[ResearchApplication], Field(min_length=4, max_length=4)]
    impact_analysis: ImpactAnalysis | None = None
    sources: Annotated[list[SourceEvidence], Field(min_length=1, max_length=20)]

    @field_validator("applications")
    @classmethod
    def applications_cover_all_required_categories(
        cls, value: list[ResearchApplication]
    ) -> list[ResearchApplication]:
        categories = [item.category for item in value]
        if set(categories) != {
            "finance",
            "investing",
            "business_strategy",
            "technology",
        }:
            raise ValueError("research applications must cover all four categories")
        if len(categories) != len(set(categories)):
            raise ValueError("research application categories cannot be duplicated")
        return value


class KnowledgeRefresh(StrictModel):
    concept: ShortText
    background: NarrativeText
    explanation: NarrativeText
    current_relevance: NarrativeText
    example: NarrativeText


class SectionState(StrictModel):
    status: Literal["available", "degraded", "unavailable"]
    detail: OptionalNarrative | None = None

    @model_validator(mode="after")
    def nonavailable_state_has_detail(self) -> SectionState:
        if self.status != "available" and self.detail is None:
            raise ValueError("degraded or unavailable section requires a safe explanation")
        return self


class SectionStatuses(StrictModel):
    market_news: SectionState
    global_macro: SectionState
    research_discovery: SectionState
    knowledge_refresh: SectionState


class MarketContext(StrictModel):
    report_date: date
    report_date_is_session: bool
    previous_session: date
    tomorrow_date: date
    tomorrow_is_session: bool
    next_open_session: date
    tomorrow_opens_at: datetime | None = None
    tomorrow_closes_at: datetime | None = None

    @field_validator("tomorrow_opens_at", "tomorrow_closes_at")
    @classmethod
    def market_times_are_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def dates_and_session_times_agree(self) -> MarketContext:
        if self.tomorrow_date != self.report_date + timedelta(days=1):
            raise ValueError("tomorrow_date must be the next calendar day")
        if self.previous_session >= self.report_date:
            raise ValueError("previous_session must precede report_date")
        if self.next_open_session < self.tomorrow_date:
            raise ValueError("next_open_session cannot precede tomorrow")
        if self.tomorrow_is_session:
            if self.next_open_session != self.tomorrow_date:
                raise ValueError("an open tomorrow must be its own next open session")
            if self.tomorrow_opens_at is None or self.tomorrow_closes_at is None:
                raise ValueError("open tomorrow requires session times")
            if self.tomorrow_closes_at <= self.tomorrow_opens_at:
                raise ValueError("market close must follow market open")
        elif self.tomorrow_opens_at is not None or self.tomorrow_closes_at is not None:
            raise ValueError("closed tomorrow cannot have session times")
        return self


class ProviderRunMetadata(StrictModel):
    provider: ShortText
    section: ShortText
    model: ShortText | None = None
    status: Literal["success", "degraded", "failed", "skipped"]
    attempts: Annotated[int, Field(strict=True, ge=0, le=10)]
    duration_ms: Annotated[int, Field(strict=True, ge=0)]
    source_count: Annotated[int, Field(strict=True, ge=0)] = 0
    web_search_calls: Annotated[int, Field(strict=True, ge=0)] = 0
    request_id: Identifier | None = None
    request_ids: Annotated[list[Identifier], Field(max_length=10)] = Field(
        default_factory=list
    )
    input_tokens: Annotated[int, Field(strict=True, ge=0)] | None = None
    output_tokens: Annotated[int, Field(strict=True, ge=0)] | None = None

    @field_validator("request_ids")
    @classmethod
    def request_ids_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("provider request IDs cannot be duplicated")
        return value


class DailyReport(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    report_id: Identifier
    report_date: date
    generated_at: datetime
    timezone: Literal["America/New_York"] = "America/New_York"
    source_cutoff_at: datetime
    market_context: MarketContext
    market_news: Annotated[list[MarketNewsItem], Field(min_length=3, max_length=3)]
    earnings: EarningsSection
    global_macro: GlobalMacroEvent
    research_discovery: ResearchDiscovery | None
    knowledge_refresh: KnowledgeRefresh | None
    section_statuses: SectionStatuses
    provider_runs: Annotated[list[ProviderRunMetadata], Field(max_length=30)] = Field(
        default_factory=list
    )
    validation_status: Literal["valid", "degraded"]
    warnings: Annotated[list[OptionalNarrative], Field(max_length=20)] = Field(
        default_factory=list
    )

    @field_validator("generated_at", "source_cutoff_at")
    @classmethod
    def report_times_are_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def report_is_publishable(self) -> DailyReport:
        if self.report_id != f"daily-market-report-{self.report_date.isoformat()}":
            raise ValueError("report_id must be the date-keyed idempotency key")
        if (
            self.generated_at.astimezone(ZoneInfo(self.timezone)).date()
            != self.report_date
        ):
            raise ValueError("report_date must match the New York generation date")
        if self.market_context.report_date != self.report_date:
            raise ValueError("market context date does not match report date")
        if self.earnings.target_date != self.market_context.tomorrow_date:
            raise ValueError("earnings target must be tomorrow's calendar date")
        if self.source_cutoff_at > self.generated_at:
            raise ValueError("source cutoff cannot be later than generation time")
        sources = [
            source
            for item in self.market_news
            for source in item.sources
        ]
        sources.extend(
            source
            for candidate in self.earnings.candidates
            for source in candidate.sources
        )
        sources.extend(self.global_macro.sources)
        if self.research_discovery is not None:
            sources.extend(self.research_discovery.sources)
        if any(
            source.published_at is not None
            and source.published_at > self.source_cutoff_at
            for source in sources
        ):
            raise ValueError("evidence cannot be published after the source cutoff")
        if self.section_statuses.market_news.status != "available":
            raise ValueError("three evidence-valid market news items are required")
        if self.section_statuses.global_macro.status != "available":
            raise ValueError("global macro is a required section")

        tomorrow_open = self.market_context.tomorrow_is_session
        if tomorrow_open and self.earnings.status in {"market_closed", "unavailable"}:
            raise ValueError("earnings is required when tomorrow is an open session")
        if not tomorrow_open and self.earnings.status != "market_closed":
            raise ValueError("closed tomorrow requires the closed-market earnings state")

        optional_pairs = (
            (self.research_discovery, self.section_statuses.research_discovery),
            (self.knowledge_refresh, self.section_statuses.knowledge_refresh),
        )
        for value, state in optional_pairs:
            if value is None and state.status == "available":
                raise ValueError("available optional section must contain content")
            if value is not None and state.status == "unavailable":
                raise ValueError("unavailable optional section cannot contain content")

        has_degradation = any(
            state.status != "available"
            for state in (
                self.section_statuses.research_discovery,
                self.section_statuses.knowledge_refresh,
            )
        )
        if has_degradation and self.validation_status != "degraded":
            raise ValueError("optional section failure must mark the report degraded")
        if has_degradation and not self.warnings:
            raise ValueError("a degraded report must contain a safe warning")
        if not has_degradation and self.validation_status != "valid":
            raise ValueError("a complete report must have valid status")
        return self


def build_report_id(report_date: date) -> str:
    """Return the stable, date-keyed idempotency key used by scheduled runs."""

    return f"daily-market-report-{report_date.isoformat()}"
