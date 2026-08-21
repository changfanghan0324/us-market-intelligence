"""Licensed market-data boundary.

OpenAI research output never contains quote, market-cap, EPS, or revenue
values.  The only way those values enter a canonical report is through this
protocol and the domain provenance validators.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable


class MarketMetricStatus(StrEnum):
    KNOWN = "known"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class MarketMetric:
    status: MarketMetricStatus
    value: Decimal | None
    unit: str | None
    provider: str | None
    source_evidence_id: str | None
    as_of: datetime | None
    unavailable_reason: str | None
    provenance: str

    @classmethod
    def known(
        cls,
        value: Decimal,
        *,
        unit: str,
        provider: str,
        source_evidence_id: str,
        as_of: datetime,
    ) -> MarketMetric:
        if not value.is_finite():
            raise ValueError("market metric must be finite")
        if not provider.strip() or not source_evidence_id.strip():
            raise ValueError("known market metric requires provider provenance")
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("market metric as_of must be timezone-aware")
        return cls(
            status=MarketMetricStatus.KNOWN,
            value=value,
            unit=unit,
            provider=provider.strip(),
            source_evidence_id=source_evidence_id.strip(),
            as_of=as_of,
            unavailable_reason=None,
            provenance="licensed_market_data_provider",
        )

    @classmethod
    def unavailable(cls, reason: str) -> MarketMetric:
        normalized = reason.strip()
        if not normalized:
            raise ValueError("unavailable market metric requires a reason")
        return cls(
            status=MarketMetricStatus.UNAVAILABLE,
            value=None,
            unit=None,
            provider=None,
            source_evidence_id=None,
            as_of=None,
            unavailable_reason=normalized,
            provenance="unavailable",
        )


@dataclass(frozen=True, slots=True)
class MarketDataEvidence:
    evidence_id: str
    title: str
    publisher: str
    tier: Literal[1, 2]
    url: str
    published_at: datetime | None
    accessed_at: datetime
    evidence_role: Literal["primary", "corroborating", "context"] = "primary"


@dataclass(frozen=True, slots=True)
class EarningsMarketData:
    ticker: str
    current_price: MarketMetric
    market_cap: MarketMetric
    expected_eps: MarketMetric
    expected_revenue: MarketMetric
    evidence: tuple[MarketDataEvidence, ...] = ()

    def __post_init__(self) -> None:
        evidence_ids = {item.evidence_id for item in self.evidence}
        for metric in (
            self.current_price,
            self.market_cap,
            self.expected_eps,
            self.expected_revenue,
        ):
            if (
                metric.status is MarketMetricStatus.KNOWN
                and metric.source_evidence_id not in evidence_ids
            ):
                raise ValueError("known market metric must attach its source evidence")


@runtime_checkable
class MarketDataProvider(Protocol):
    """Adapter for data whose public-display license has been reviewed."""

    def get_earnings_metrics(
        self,
        tickers: Sequence[str],
        *,
        target_date: date,
        as_of: datetime,
    ) -> Mapping[str, EarningsMarketData]: ...


class DisabledMarketDataProvider:
    """Safe default when no licensed quote/estimate provider is configured."""

    reason = "No licensed market-data provider configured."

    def get_earnings_metrics(
        self,
        tickers: Sequence[str],
        *,
        target_date: date,
        as_of: datetime,
    ) -> Mapping[str, EarningsMarketData]:
        del target_date, as_of
        return {
            ticker.upper(): EarningsMarketData(
                ticker=ticker.upper(),
                current_price=MarketMetric.unavailable(self.reason),
                market_cap=MarketMetric.unavailable(self.reason),
                expected_eps=MarketMetric.unavailable(self.reason),
                expected_revenue=MarketMetric.unavailable(self.reason),
            )
            for ticker in tickers
        }
