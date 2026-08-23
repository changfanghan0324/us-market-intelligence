"""Fail-closed NYSE calendar context and timezone-aware schedule gating."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import exchange_calendars
import pandas as pd

from market_intelligence.domain.models import MarketContext
from market_intelligence.errors import CalendarCoverageError, ConfigurationError

NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class SessionInfo:
    session_date: date
    opens_at: datetime
    closes_at: datetime
    is_early_close: bool


class NYSECalendar:
    """Small stable surface over the pinned `exchange_calendars` package."""

    def __init__(self, calendar: Any | None = None) -> None:
        self._calendar = calendar or exchange_calendars.get_calendar("XNYS")
        self.coverage_start = self._calendar.first_session.date()
        self.coverage_end = self._calendar.last_session.date()

    def _ensure_coverage(self, target: date) -> None:
        if not isinstance(target, date) or isinstance(target, datetime):
            raise TypeError("calendar target must be a date")
        if not self.coverage_start <= target <= self.coverage_end:
            raise CalendarCoverageError(
                "Pinned XNYS calendar coverage does not include the requested date. "
                "Upgrade and verify exchange-calendars before publishing."
            )

    @staticmethod
    def _label(target: date) -> pd.Timestamp:
        return pd.Timestamp(target)

    def is_session(self, target: date) -> bool:
        self._ensure_coverage(target)
        return bool(self._calendar.is_session(self._label(target)))

    def previous_session(self, target: date) -> date:
        """Return the most recent session strictly before `target`."""

        self._ensure_coverage(target)
        label = self._label(target)
        try:
            if self._calendar.is_session(label):
                result = self._calendar.previous_session(label)
            else:
                result = self._calendar.date_to_session(label, direction="previous")
        except (ValueError, IndexError) as error:
            raise CalendarCoverageError(
                "Pinned XNYS calendar cannot resolve the previous session."
            ) from error
        resolved = result.date()
        self._ensure_coverage(resolved)
        return resolved

    def session_on_or_after(self, target: date) -> date:
        """Return `target` when open, otherwise the next NYSE session."""

        self._ensure_coverage(target)
        try:
            result = self._calendar.date_to_session(
                self._label(target),
                direction="next",
            )
        except (ValueError, IndexError) as error:
            raise CalendarCoverageError(
                "Pinned XNYS calendar cannot resolve the next open session."
            ) from error
        resolved = result.date()
        self._ensure_coverage(resolved)
        return resolved

    def session_info(self, target: date) -> SessionInfo:
        self._ensure_coverage(target)
        if not self.is_session(target):
            raise ValueError("requested date is not an XNYS session")
        label = self._label(target)
        opens_at = self._calendar.session_open(label).to_pydatetime().astimezone(UTC)
        closes_at = self._calendar.session_close(label).to_pydatetime().astimezone(UTC)
        local_close = closes_at.astimezone(NEW_YORK)
        return SessionInfo(
            session_date=target,
            opens_at=opens_at,
            closes_at=closes_at,
            is_early_close=local_close.time() < time(16, 0),
        )

    def market_context(self, report_date: date) -> MarketContext:
        """Resolve prior session and next-calendar-day earnings context."""

        self._ensure_coverage(report_date)
        tomorrow = report_date + timedelta(days=1)
        self._ensure_coverage(tomorrow)
        tomorrow_is_session = self.is_session(tomorrow)
        next_open = self.session_on_or_after(tomorrow)
        tomorrow_info = self.session_info(tomorrow) if tomorrow_is_session else None
        return MarketContext(
            report_date=report_date,
            report_date_is_session=self.is_session(report_date),
            previous_session=self.previous_session(report_date),
            tomorrow_date=tomorrow,
            tomorrow_is_session=tomorrow_is_session,
            next_open_session=next_open,
            tomorrow_opens_at=tomorrow_info.opens_at if tomorrow_info else None,
            tomorrow_closes_at=tomorrow_info.closes_at if tomorrow_info else None,
        )


def is_eligible_scheduled_slot(
    scheduled_at: datetime,
    *,
    timezone_name: str = "America/New_York",
    hour: int = 8,
    minute: int = 7,
) -> bool:
    """Evaluate the primary scheduled timestamp, never a delayed start time."""

    if scheduled_at.tzinfo is None or scheduled_at.utcoffset() is None:
        raise ValueError("scheduled_at must be timezone-aware")
    if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
        raise ValueError("hour must be an integer from 0 through 23")
    if isinstance(minute, bool) or not isinstance(minute, int) or not 0 <= minute <= 59:
        raise ValueError("minute must be an integer from 0 through 59")
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ConfigurationError("Configured schedule timezone is unavailable.") from error
    local_slot = scheduled_at.astimezone(zone)
    return (
        local_slot.hour == hour
        and local_slot.minute == minute
        and local_slot.second == 0
        and local_slot.microsecond == 0
    )


def should_run_for_event(
    event_name: str,
    *,
    scheduled_at: datetime | None = None,
    force: bool = False,
) -> bool:
    """Manual dispatch bypasses the slot gate; schedule requires its slot time."""

    if event_name == "workflow_dispatch":
        return True
    if event_name != "schedule":
        return False
    if force:
        # `force` controls date-keyed idempotency, not schedule eligibility.
        pass
    if scheduled_at is None:
        raise ValueError("scheduled_at is required for a schedule event")
    return is_eligible_scheduled_slot(scheduled_at)
