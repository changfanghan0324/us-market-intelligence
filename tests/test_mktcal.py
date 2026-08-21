from __future__ import annotations

from datetime import UTC, date, datetime
from importlib import metadata
from zoneinfo import ZoneInfo

import pytest

from market_intelligence.errors import CalendarCoverageError
from market_intelligence.mktcal import (
    NYSECalendar,
    is_eligible_scheduled_slot,
    should_run_for_event,
)


@pytest.fixture(scope="module")
def calendar() -> NYSECalendar:
    return NYSECalendar()


def test_exchange_calendar_and_tzdata_are_pinned_to_verified_versions() -> None:
    assert metadata.version("exchange-calendars") == "4.13.2"
    assert metadata.version("tzdata") == "2026.3"


def test_2026_independence_day_observed_is_closed(calendar: NYSECalendar) -> None:
    assert not calendar.is_session(date(2026, 7, 3))
    assert calendar.session_on_or_after(date(2026, 7, 3)) == date(2026, 7, 6)


def test_2026_thanksgiving_is_closed_and_friday_is_early_close(
    calendar: NYSECalendar,
) -> None:
    assert not calendar.is_session(date(2026, 11, 26))
    session = calendar.session_info(date(2026, 11, 27))
    assert session.is_early_close
    local_close = session.closes_at.astimezone(ZoneInfo("America/New_York"))
    assert (local_close.hour, local_close.minute) == (13, 0)


def test_market_context_targets_next_calendar_day_and_next_open_session(
    calendar: NYSECalendar,
) -> None:
    context = calendar.market_context(date(2026, 7, 2))
    assert context.previous_session == date(2026, 7, 1)
    assert context.tomorrow_date == date(2026, 7, 3)
    assert not context.tomorrow_is_session
    assert context.next_open_session == date(2026, 7, 6)
    assert context.tomorrow_opens_at is None
    assert context.tomorrow_closes_at is None


def test_open_tomorrow_carries_sourced_calendar_times(calendar: NYSECalendar) -> None:
    context = calendar.market_context(date(2026, 8, 20))
    assert context.tomorrow_is_session
    assert context.next_open_session == date(2026, 8, 21)
    assert context.tomorrow_opens_at == datetime(2026, 8, 21, 13, 30, tzinfo=UTC)
    assert context.tomorrow_closes_at == datetime(2026, 8, 21, 20, 0, tzinfo=UTC)


def _eligible_utc_hours(day: date) -> list[int]:
    return [
        hour
        for hour in (12, 13)
        if is_eligible_scheduled_slot(
            datetime(day.year, day.month, day.day, hour, 0, tzinfo=UTC)
        )
    ]


@pytest.mark.parametrize(
    ("day", "expected_hour"),
    [
        (date(2026, 3, 7), 13),  # last day before spring transition
        (date(2026, 3, 8), 12),  # first day in daylight time
        (date(2026, 10, 31), 12),  # last day before fall transition
        (date(2026, 11, 1), 13),  # first day back in standard time
    ],
)
def test_2026_dst_boundaries_have_exactly_one_eligible_slot(
    day: date,
    expected_hour: int,
) -> None:
    assert _eligible_utc_hours(day) == [expected_hour]


def test_gate_uses_scheduled_slot_not_delayed_wall_clock_start() -> None:
    scheduled_slot = datetime(2026, 3, 8, 12, 0, tzinfo=UTC)
    delayed_start = datetime(2026, 3, 8, 12, 47, tzinfo=UTC)
    assert is_eligible_scheduled_slot(scheduled_slot)
    assert not is_eligible_scheduled_slot(delayed_start)


def test_timezone_aware_local_schedule_is_eligible_across_dst() -> None:
    new_york = ZoneInfo("America/New_York")
    assert is_eligible_scheduled_slot(datetime(2026, 1, 15, 8, 0, tzinfo=new_york))
    assert is_eligible_scheduled_slot(datetime(2026, 7, 15, 8, 0, tzinfo=new_york))


def test_schedule_requires_an_exact_slot_and_manual_dispatch_bypasses() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        is_eligible_scheduled_slot(
            datetime(2026, 8, 21, 8, 0, tzinfo=UTC).replace(tzinfo=None)
        )
    assert should_run_for_event("workflow_dispatch")
    assert not should_run_for_event("pull_request")
    with pytest.raises(ValueError, match="scheduled_at"):
        should_run_for_event("schedule")


def test_calendar_fails_closed_outside_dependency_coverage(calendar: NYSECalendar) -> None:
    with pytest.raises(CalendarCoverageError, match="coverage"):
        calendar.is_session(date(2100, 1, 4))
