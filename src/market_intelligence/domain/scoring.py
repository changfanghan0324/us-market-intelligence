"""Deterministic, model-independent report scoring and selection."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from decimal import Decimal
from typing import Final

from market_intelligence.errors import ReportValidationError

NEWS_COMPONENT_FIELDS: Final[tuple[str, ...]] = (
    "market_size_impact",
    "earnings_impact",
    "macro_importance",
    "sector_influence",
    "short_term_trading_relevance",
)
EARNINGS_WEIGHTS: Final[dict[str, Decimal]] = {
    "business_quality": Decimal("0.30"),
    "revenue_growth": Decimal("0.20"),
    "earnings_surprise_potential": Decimal("0.20"),
    "market_expectation_gap": Decimal("0.15"),
    "risk_reward_asymmetry": Decimal("0.15"),
}


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))


def news_attention_score(components: object) -> float:
    """Return the arithmetic mean of the five code-owned news components."""

    total = sum((_decimal(getattr(components, name)) for name in NEWS_COMPONENT_FIELDS), Decimal())
    return float(total / Decimal(len(NEWS_COMPONENT_FIELDS)))


def earnings_selection_score(components: object) -> float:
    """Return the exact 30/20/20/15/15 weighted earnings score."""

    total = sum(
        (
            _decimal(getattr(components, name)) * weight
            for name, weight in EARNINGS_WEIGHTS.items()
        ),
        Decimal(),
    )
    return float(total)


def _deduplication_key(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title).casefold()
    return re.sub(r"[^\w]+", "", normalized, flags=re.UNICODE)


def select_top_news(items: Iterable[object], *, count: int = 3) -> list[object]:
    """Deduplicate before ranking and fail closed if fewer than `count` remain."""

    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("count must be a positive integer")
    deduplicated: dict[str, object] = {}
    for item in items:
        key = _deduplication_key(item.title)
        current = deduplicated.get(key)
        if current is None or item.computed_score > current.computed_score:
            deduplicated[key] = item
    ranked = sorted(
        deduplicated.values(),
        key=lambda item: (-item.computed_score, -item.event_date.toordinal(), item.news_id),
    )
    if len(ranked) < count:
        raise ReportValidationError(
            f"market news requires {count} evidence-valid, deduplicated items"
        )
    return ranked[:count]


def select_earnings_candidates(
    candidates: Iterable[object],
    *,
    minimum_score: float = 7.0,
) -> list[object]:
    """Keep only candidates passing both deterministic score and attention gates."""

    if isinstance(minimum_score, bool) or not isinstance(minimum_score, (int, float)):
        raise TypeError("minimum_score must be numeric")
    if not 0.0 <= float(minimum_score) <= 10.0:
        raise ValueError("minimum_score must be between 0 and 10")
    return sorted(
        (
            candidate
            for candidate in candidates
            if candidate.market_attention and candidate.computed_score >= minimum_score
        ),
        key=lambda candidate: (-candidate.computed_score, candidate.ticker),
    )
