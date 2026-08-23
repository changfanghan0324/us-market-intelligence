from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from market_intelligence.domain.models import (
    EarningsSelectionComponents,
    NewsAttentionComponents,
)
from market_intelligence.domain.scoring import (
    earnings_selection_score,
    news_attention_score,
    select_earnings_candidates,
    select_top_news,
)
from market_intelligence.errors import ReportValidationError


def test_news_score_is_exact_arithmetic_mean() -> None:
    components = NewsAttentionComponents(
        market_size_impact=10.0,
        earnings_impact=8.0,
        macro_importance=6.0,
        sector_influence=4.0,
        short_term_trading_relevance=2.0,
    )
    assert news_attention_score(components) == 6.0


def test_earnings_score_uses_exact_required_weights() -> None:
    components = EarningsSelectionComponents(
        business_quality=10.0,
        revenue_growth=8.0,
        earnings_surprise_potential=6.0,
        market_expectation_gap=4.0,
        risk_reward_asymmetry=2.0,
    )
    assert earnings_selection_score(components) == 6.7


def test_each_earnings_weight_is_regression_guarded() -> None:
    expected = {
        "business_quality": 3.0,
        "revenue_growth": 2.0,
        "earnings_surprise_potential": 2.0,
        "market_expectation_gap": 1.5,
        "risk_reward_asymmetry": 1.5,
    }
    for field, contribution in expected.items():
        values = {name: 0.0 for name in expected}
        values[field] = 10.0
        assert (
            earnings_selection_score(EarningsSelectionComponents(**values))
            == contribution
        )


def test_earnings_boundary_requires_score_and_attention() -> None:
    candidates = [
        SimpleNamespace(ticker="PASS", computed_score=7.0, market_attention=True),
        SimpleNamespace(ticker="LOW", computed_score=6.999, market_attention=True),
        SimpleNamespace(ticker="QUIET", computed_score=9.0, market_attention=False),
        SimpleNamespace(ticker="HIGH", computed_score=8.0, market_attention=True),
    ]
    assert [item.ticker for item in select_earnings_candidates(candidates)] == [
        "HIGH",
        "PASS",
    ]


def _news(news_id: str, title: str, score: float) -> SimpleNamespace:
    return SimpleNamespace(
        news_id=news_id,
        title=title,
        computed_score=score,
        event_date=date(2026, 8, 21),
    )


def test_news_deduplicates_before_ranking() -> None:
    selected = select_top_news(
        [
            _news("one", "Federal Reserve changes guidance", 8.0),
            _news("duplicate", "Federal Reserve: changes guidance!", 9.0),
            _news("two", "Semiconductor demand accelerates", 7.0),
            _news("three", "Oil supply expectations tighten", 6.0),
        ]
    )
    assert [item.news_id for item in selected] == ["duplicate", "two", "three"]


def test_news_fails_closed_instead_of_inventing_filler() -> None:
    with pytest.raises(ReportValidationError):
        select_top_news([_news("one", "Only one valid event", 8.0)])


@pytest.mark.parametrize("value", [-0.1, 10.1])
def test_score_components_are_bounded(value: float) -> None:
    with pytest.raises(ValidationError):
        NewsAttentionComponents(
            market_size_impact=value,
            earnings_impact=5.0,
            macro_importance=5.0,
            sector_influence=5.0,
            short_term_trading_relevance=5.0,
        )
