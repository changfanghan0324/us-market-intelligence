from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from market_intelligence.providers.knowledge_catalog import (
    KNOWLEDGE_REFRESH_CARD_COUNT,
    KNOWLEDGE_REFRESH_EPOCH,
    knowledge_refresh_for_date,
)
from market_intelligence.providers.openai_research import AIKnowledgeRefresh

_NARRATIVE_FIELDS = (
    "historical_background",
    "simple_explanation",
    "why_it_still_matters_today",
    "real_world_example",
)


def _sentence_count(value: str) -> int:
    return len(re.findall(r"[。！？]", value))


def test_catalog_has_at_least_24_distinct_professional_cards() -> None:
    cards = [
        knowledge_refresh_for_date(KNOWLEDGE_REFRESH_EPOCH + timedelta(days=offset))
        for offset in range(KNOWLEDGE_REFRESH_CARD_COUNT)
    ]

    assert KNOWLEDGE_REFRESH_CARD_COUNT >= 24
    assert all(isinstance(card, AIKnowledgeRefresh) for card in cards)
    assert len({card.concept for card in cards}) == KNOWLEDGE_REFRESH_CARD_COUNT
    assert (
        len({card.model_dump_json() for card in cards}) == KNOWLEDGE_REFRESH_CARD_COUNT
    )


def test_every_narrative_is_substantive_and_every_example_is_worked() -> None:
    for offset in range(KNOWLEDGE_REFRESH_CARD_COUNT):
        card = knowledge_refresh_for_date(
            KNOWLEDGE_REFRESH_EPOCH + timedelta(days=offset)
        )
        for field_name in _NARRATIVE_FIELDS:
            value = getattr(card, field_name)
            assert 2 <= _sentence_count(value) <= 4, (card.concept, field_name)
            assert len(value) >= 70, (card.concept, field_name)

        assert re.search(r"[零一二三四五六七八九十百\d]", card.real_world_example)
        assert any(
            marker in card.real_world_example for marker in ("假設", "若", "例如")
        )


def test_catalog_contains_no_live_claims_tickers_or_investment_instructions() -> None:
    forbidden = (
        "買進",
        "賣出",
        "加碼",
        "減碼",
        "目標價",
        "停損",
        "截至今日",
        "本日上漲",
        "本日下跌",
    )
    ticker_pattern = re.compile(r"(?:NYSE|NASDAQ|AMEX)\s*[:：]")

    for offset in range(KNOWLEDGE_REFRESH_CARD_COUNT):
        payload = knowledge_refresh_for_date(
            KNOWLEDGE_REFRESH_EPOCH + timedelta(days=offset)
        ).model_dump_json()
        assert not any(term in payload for term in forbidden)
        assert ticker_pattern.search(payload) is None


def test_epoch_selection_is_reproducible_and_wraps_exactly() -> None:
    first = knowledge_refresh_for_date(KNOWLEDGE_REFRESH_EPOCH)
    second = knowledge_refresh_for_date(KNOWLEDGE_REFRESH_EPOCH + timedelta(days=1))
    wrapped = knowledge_refresh_for_date(
        KNOWLEDGE_REFRESH_EPOCH + timedelta(days=KNOWLEDGE_REFRESH_CARD_COUNT)
    )

    assert first.concept == "基準率與貝氏更新"
    assert second != first
    assert wrapped == first
    assert knowledge_refresh_for_date(KNOWLEDGE_REFRESH_EPOCH) is first


def test_pre_epoch_dates_remain_deterministic() -> None:
    prior_date = KNOWLEDGE_REFRESH_EPOCH - timedelta(days=1)

    assert knowledge_refresh_for_date(prior_date) == knowledge_refresh_for_date(
        prior_date
    )
    assert knowledge_refresh_for_date(prior_date).concept == "反身性與回饋循環"


def test_discount_rate_example_uses_relative_percent_not_percentage_points() -> None:
    cards = [
        knowledge_refresh_for_date(KNOWLEDGE_REFRESH_EPOCH + timedelta(days=offset))
        for offset in range(KNOWLEDGE_REFRESH_CARD_COUNT)
    ]
    card = next(card for card in cards if card.concept == "折現率與資產存續期")

    assert "相對下降約百分之十三" in card.real_world_example
    assert "十三個百分點" not in card.real_world_example


@pytest.mark.parametrize(
    "invalid",
    ["2026-08-23", datetime(2026, 8, 23, 8, 0, tzinfo=UTC), None],
)
def test_selection_rejects_non_date_inputs(invalid: object) -> None:
    with pytest.raises(TypeError, match="report_date must be a date"):
        knowledge_refresh_for_date(invalid)  # type: ignore[arg-type]
