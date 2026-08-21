from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from openai.lib._pydantic import to_strict_json_schema

from market_intelligence.providers.base import (
    OPENAI_KEY_CONFIGURATION_MESSAGE,
    AuthenticationProviderError,
    ConfigurationError,
    EvidenceValidationError,
    ResearchSection,
)
from market_intelligence.providers.market_data import (
    DisabledMarketDataProvider,
    MarketMetricStatus,
)
from market_intelligence.providers.openai_research import (
    BANNED_MARKET_DATA_FIELDS,
    AIEvidence,
    EarningsResponse,
    GlobalMacroResponse,
    KnowledgeRefreshResponse,
    MarketNewsResponse,
    OpenAIResearchProvider,
    OpenAIResearchSettings,
    ResearchDiscoveryResponse,
    ResearchRequest,
    assert_ai_schema_has_no_market_data_fields,
    iter_evidence,
)

API_KEY = "sk-" + ("x" * 40)
NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


class FakeResponses:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, outcomes: list[Any]) -> None:
        self.responses = FakeResponses(outcomes)


class FakeHTTPError(Exception):
    def __init__(self, status_code: int, *, retry_after: str | None = None) -> None:
        super().__init__("raw response body must never be copied")
        self.status_code = status_code
        self.response = SimpleNamespace(
            status_code=status_code,
            headers={} if retry_after is None else {"retry-after": retry_after},
        )


class LengthFinishReasonError(Exception):
    pass


class ResponseThenTransientError:
    usage = SimpleNamespace(input_tokens=100, output_tokens=50, total_tokens=150)
    output: tuple[Any, ...] = ()
    _request_id = "req_first_response"

    @property
    def output_parsed(self) -> Any:
        raise FakeHTTPError(429, retry_after="0")


class ResponseThenStructuredValidationError:
    usage = SimpleNamespace(input_tokens=100, output_tokens=50, total_tokens=150)
    output: tuple[Any, ...] = ()
    _request_id = "req_invalid_semantics"

    @property
    def output_parsed(self) -> Any:
        return MarketNewsResponse.model_validate({"candidates": []})


def evidence(
    *,
    publisher: str = "Reuters",
    url: str = "https://www.reuters.com/markets/us/example-article/",
) -> dict[str, Any]:
    return {
        "title": "Authoritative evidence for the reported market event",
        "publisher": publisher,
        "url": url,
        "published_at": "2026-08-21T11:15:00Z",
        "evidence_role": "primary",
        "quoted_fragment": None,
    }


def impact() -> dict[str, Any]:
    return {
        "what_changed": "Policy expectations shifted after officials supplied new guidance.",
        "why_it_matters": "The change alters discount rates and near-term positioning decisions.",
        "beneficiaries": [
            {
                "kind": "named_entity",
                "name": "US investment-grade bond issuers",
                "rationale": "Lower benchmark yields can reduce refinancing costs.",
            }
        ],
        "losers": [
            {
                "kind": "named_entity",
                "name": "Highly leveraged rate-sensitive companies",
                "rationale": "A renewed rate selloff would pressure their financing burden.",
            }
        ],
        "professional_investor_reaction": (
            "Professional investors would rebalance duration and reduce crowded exposures."
        ),
        "indicators_to_monitor_next": ["Two-year Treasury yield", "Fed funds futures"],
    }


def news_response(*, source: dict[str, Any] | None = None, title_prefix: str = "Event") -> Any:
    source = source or evidence()
    candidates = []
    for index in range(3):
        candidates.append(
            {
                "title": f"{title_prefix} {index} materially changes US market expectations",
                "event_date": "2026-08-21",
                "market_impact": "high",
                "affected_sectors": ["Financials", "Technology"],
                "summary": "A verified development changed cross-asset pricing before the session.",
                "bullish_case": "Lower financing pressure may support earnings multiples.",
                "bearish_case": "Positioning may reverse if follow-through evidence disappoints.",
                "attention_components": {
                    "market_size_impact": 8,
                    "earnings_impact": 7,
                    "macro_importance": 9,
                    "sector_influence": 8,
                    "short_term_trading_relevance": 8,
                },
                "impact_analysis": impact(),
                "sources": [
                    {
                        **source,
                        "url": str(source["url"]).replace(
                            "example-article", f"example-article-{index}"
                        ),
                    }
                ],
            }
        )
    parsed = MarketNewsResponse.model_validate({"candidates": candidates})
    return fake_sdk_response(parsed)


def fake_sdk_response(parsed: Any) -> Any:
    sources = [SimpleNamespace(url=str(item.url)) for item in iter_evidence(parsed)]
    return SimpleNamespace(
        output_parsed=parsed,
        usage=SimpleNamespace(input_tokens=100, output_tokens=50, total_tokens=150),
        output=(
            [
                SimpleNamespace(
                    type="web_search_call",
                    action=SimpleNamespace(sources=sources),
                )
            ]
            if sources
            else []
        ),
        _request_id="req_test_123",
    )


def request() -> ResearchRequest:
    return ResearchRequest(
        report_date=date(2026, 8, 21),
        previous_session_date=date(2026, 8, 20),
        target_date=date(2026, 8, 22),
        generated_at=NOW,
        timezone_name="America/New_York",
    )


def settings(**overrides: Any) -> OpenAIResearchSettings:
    values = {
        "max_attempts": 3,
        "base_retry_delay_seconds": 0,
        "max_retry_delay_seconds": 0,
    }
    values.update(overrides)
    return OpenAIResearchSettings(**values)


def test_missing_key_fails_preflight_with_fixed_instructions() -> None:
    client = FakeClient([news_response()])

    with pytest.raises(ConfigurationError) as raised:
        OpenAIResearchProvider.from_env(settings(), environ={}, client=client)

    assert str(raised.value) == OPENAI_KEY_CONFIGURATION_MESSAGE
    assert client.responses.calls == []


def test_responses_call_is_stateless_bounded_and_domain_filtered() -> None:
    client = FakeClient([news_response()])
    provider = OpenAIResearchProvider(settings(), api_key=API_KEY, client=client, now=lambda: NOW)

    result = provider.market_news(request())

    call = client.responses.calls[0]
    assert call["store"] is False
    assert call["max_output_tokens"] <= OpenAIResearchSettings.MAX_OUTPUT_TOKEN_CEILING
    assert call["max_tool_calls"] <= OpenAIResearchSettings.MAX_TOOL_CALL_CEILING
    assert call["tools"][0]["type"] == "web_search"
    assert "filters" in call["tools"][0]
    assert "reuters.com" in call["tools"][0]["filters"]["allowed_domains"]
    assert result.metadata.usage.web_search_calls == 1
    assert result.metadata.attempts == 1


def test_knowledge_refresh_has_no_web_tool() -> None:
    parsed = KnowledgeRefreshResponse.model_validate(
        {
            "knowledge": {
                "concept": "Base-rate neglect",
                "historical_background": (
                    "The idea grew from foundational behavioral decision research."
                ),
                "simple_explanation": (
                    "People often focus on vivid case details and overlook how common an "
                    "outcome is in the broader population."
                ),
                "why_it_still_matters_today": (
                    "Investment narratives remain vulnerable to neglected prior probabilities."
                ),
                "real_world_example": (
                    "A forecaster compares a startup story with the base rate of similar firms."
                ),
            }
        }
    )
    client = FakeClient([fake_sdk_response(parsed)])
    provider = OpenAIResearchProvider(settings(), api_key=API_KEY, client=client, now=lambda: NOW)

    provider.knowledge_refresh(request())

    call = client.responses.calls[0]
    assert "tools" not in call
    assert "max_tool_calls" not in call


def test_transient_error_retries_but_is_strictly_bounded() -> None:
    sleeps: list[float] = []
    client = FakeClient([FakeHTTPError(429, retry_after="0"), news_response()])
    provider = OpenAIResearchProvider(
        settings(), api_key=API_KEY, client=client, sleep=sleeps.append, now=lambda: NOW
    )

    result = provider.market_news(request())

    assert len(client.responses.calls) == 2
    assert sleeps == [0]
    assert result.metadata.attempts == 2


def test_observed_response_usage_survives_a_later_retry() -> None:
    observed: list[tuple[ResearchSection, int, int, str | None]] = []
    client = FakeClient([ResponseThenTransientError(), news_response()])
    provider = OpenAIResearchProvider(
        settings(),
        api_key=API_KEY,
        client=client,
        sleep=lambda _delay: None,
        now=lambda: NOW,
        usage_observer=lambda section, attempt, usage, _at, request_id: observed.append(
            (section, attempt, usage.input_tokens, request_id)
        ),
    )

    result = provider.market_news(request())

    assert observed == [
        (ResearchSection.MARKET_NEWS, 1, 100, "req_first_response"),
        (ResearchSection.MARKET_NEWS, 2, 100, "req_test_123"),
    ]
    assert result.metadata.usage.input_tokens == 200
    assert result.metadata.usage.output_tokens == 100
    assert result.metadata.request_ids == (
        "req_first_response",
        "req_test_123",
    )


def test_output_length_finish_is_retried_within_the_attempt_cap() -> None:
    client = FakeClient([LengthFinishReasonError(), news_response()])
    provider = OpenAIResearchProvider(
        settings(), api_key=API_KEY, client=client, sleep=lambda _delay: None
    )

    result = provider.market_news(request())

    assert len(client.responses.calls) == 2
    assert result.metadata.attempts == 2


def test_semantic_validation_failure_is_retried_and_usage_is_preserved() -> None:
    observed: list[tuple[int, str | None]] = []
    client = FakeClient([ResponseThenStructuredValidationError(), news_response()])
    provider = OpenAIResearchProvider(
        settings(),
        api_key=API_KEY,
        client=client,
        sleep=lambda _delay: None,
        now=lambda: NOW,
        usage_observer=lambda _section, attempt, _usage, _at, request_id: (
            observed.append((attempt, request_id))
        ),
    )

    result = provider.market_news(request())

    assert observed == [(1, "req_invalid_semantics"), (2, "req_test_123")]
    assert result.metadata.attempts == 2
    assert result.metadata.usage.input_tokens == 200
    assert result.metadata.usage.output_tokens == 100


def test_authentication_error_is_never_retried_or_leaked() -> None:
    client = FakeClient([FakeHTTPError(401)])
    provider = OpenAIResearchProvider(settings(), api_key=API_KEY, client=client)

    with pytest.raises(AuthenticationProviderError) as raised:
        provider.market_news(request())

    assert len(client.responses.calls) == 1
    assert "raw response body" not in str(raised.value)
    assert API_KEY not in str(raised.value)


@pytest.mark.parametrize(
    ("publisher", "url"),
    [
        ("Reuters", "https://evil.example/markets/fabricated"),
        ("Reuters", "https://www.cnbc.com/2026/08/21/mismatch.html"),
    ],
)
def test_source_domain_and_publisher_host_are_independently_validated(
    publisher: str, url: str
) -> None:
    client = FakeClient([news_response(source=evidence(publisher=publisher, url=url))])
    provider = OpenAIResearchProvider(settings(), api_key=API_KEY, client=client)

    with pytest.raises(EvidenceValidationError):
        provider.market_news(request())

    assert len(client.responses.calls) == 1


def test_cited_url_must_appear_in_web_search_source_lineage() -> None:
    response = news_response()
    response.output[0].action.sources = [
        SimpleNamespace(url="https://www.reuters.com/markets/us/a-different-article/")
    ]
    client = FakeClient([response])
    provider = OpenAIResearchProvider(settings(), api_key=API_KEY, client=client)

    with pytest.raises(EvidenceValidationError):
        provider.market_news(request())


def test_control_and_bidi_characters_are_removed_from_model_text() -> None:
    client = FakeClient([news_response(title_prefix="Safe\u202e\x00Event")])
    provider = OpenAIResearchProvider(settings(), api_key=API_KEY, client=client, now=lambda: NOW)

    result = provider.market_news(request())

    title = result.data.candidates[0].title
    assert "\u202e" not in title
    assert "\x00" not in title
    assert "SafeEvent" in title


def test_all_ai_schemas_exclude_licensed_numeric_fields() -> None:
    schemas = (
        MarketNewsResponse,
        EarningsResponse,
        GlobalMacroResponse,
        ResearchDiscoveryResponse,
        KnowledgeRefreshResponse,
    )
    serialized = " ".join(str(schema.model_json_schema()) for schema in schemas)

    for schema in schemas:
        assert_ai_schema_has_no_market_data_fields(schema)
    for field_name in BANNED_MARKET_DATA_FIELDS:
        assert f"'{field_name}'" not in serialized


def test_model_facing_wire_schemas_match_supported_structured_output_subset() -> None:
    schemas = (
        MarketNewsResponse,
        EarningsResponse,
        GlobalMacroResponse,
        ResearchDiscoveryResponse,
        KnowledgeRefreshResponse,
    )
    forbidden_keys = {
        "oneOf",
        "discriminator",
        "allOf",
        "not",
        "dependentRequired",
        "dependentSchemas",
        "if",
        "then",
        "else",
        "patternProperties",
        "default",
    }
    allowed_formats = {
        "date-time",
        "time",
        "date",
        "duration",
        "email",
        "hostname",
        "ipv4",
        "ipv6",
        "uuid",
    }

    def assert_supported(node: object) -> None:
        if isinstance(node, dict):
            assert forbidden_keys.isdisjoint(node)
            if "format" in node:
                assert node["format"] in allowed_formats
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert set(node.get("required", [])) == set(node.get("properties", {}))
            for value in node.values():
                assert_supported(value)
        elif isinstance(node, list):
            for value in node:
                assert_supported(value)

    for schema in schemas:
        assert_supported(to_strict_json_schema(schema))


@pytest.mark.parametrize(
    "url",
    [
        "http://www.reuters.com/markets/us/example-article/",
        "https://user@example.com/article",
        "https://example.com:444/article",
        "https:///missing-host",
    ],
)
def test_model_facing_evidence_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(ValueError, match="evidence URL"):
        AIEvidence.model_validate(evidence(url=url))


def test_model_facing_evidence_canonicalizes_equivalent_urls() -> None:
    without_slash = AIEvidence.model_validate(
        evidence(url="https://www.reuters.com")
    )
    uppercase_with_default_port = AIEvidence.model_validate(
        evidence(url="HTTPS://WWW.REUTERS.COM:443")
    )

    assert without_slash.url == "https://www.reuters.com/"
    assert uppercase_with_default_port.url == without_slash.url


def test_model_facing_analysis_accepts_concise_traditional_chinese() -> None:
    payload = news_response().output_parsed.model_dump(mode="json")
    candidate = payload["candidates"][0]
    candidate.update(
        {
            "title": "政策預期轉向",
            "summary": "政策訊號改變市場定價",
            "bullish_case": "資金成本下降有利估值",
            "bearish_case": "通膨反彈可能壓抑估值",
        }
    )
    candidate["impact_analysis"].update(
        {
            "what_changed": "政策預期明顯轉向",
            "why_it_matters": "折現率下降重估風險資產",
            "professional_investor_reaction": "機構調整久期與風險曝險",
            "indicators_to_monitor_next": ["兩年期殖利率", "期貨定價"],
        }
    )

    parsed = MarketNewsResponse.model_validate(payload)

    assert parsed.candidates[0].bullish_case == "資金成本下降有利估值"
    assert parsed.candidates[0].impact_analysis.indicators_to_monitor_next == [
        "兩年期殖利率",
        "期貨定價",
    ]


def test_disabled_market_data_adapter_returns_only_unavailable_metrics() -> None:
    result = DisabledMarketDataProvider().get_earnings_metrics(
        ["NVDA", "MSFT"], target_date=date(2026, 8, 22), as_of=NOW
    )

    assert set(result) == {"NVDA", "MSFT"}
    for snapshot in result.values():
        for metric in (
            snapshot.current_price,
            snapshot.market_cap,
            snapshot.expected_eps,
            snapshot.expected_revenue,
        ):
            assert metric.status is MarketMetricStatus.UNAVAILABLE
            assert metric.value is None
            assert metric.provenance == "unavailable"


def test_cost_caps_cannot_be_configured_above_ceiling() -> None:
    with pytest.raises(ValueError):
        OpenAIResearchSettings(
            max_output_tokens=OpenAIResearchSettings.MAX_OUTPUT_TOKEN_CEILING + 1
        )
    with pytest.raises(ValueError):
        OpenAIResearchSettings(max_tool_calls=OpenAIResearchSettings.MAX_TOOL_CALL_CEILING + 1)
