"""Tests for common.trading_calendar (pure-Python, no external deps)."""
from datetime import datetime

from common.trading_calendar import is_trading_day, trading_day_fetch_range


def test_weekend_not_trading():
    # 2026-01-03 is Saturday, 2026-01-04 is Sunday
    sat = datetime(2026, 1, 3)
    sun = datetime(2026, 1, 4)
    assert not is_trading_day(sat)
    assert not is_trading_day(sun)


def test_weekday_is_trading():
    # 2026-01-05 is Monday (not a holiday)
    mon = datetime(2026, 1, 5)
    assert is_trading_day(mon)


def test_holiday_not_trading():
    # Spring Festival 2026: Jan 26 - Feb 1 (approximate, check actual list)
    from common.trading_calendar import A_SHARE_HOLIDAYS_2026
    if A_SHARE_HOLIDAYS_2026:
        first_holiday = list(A_SHARE_HOLIDAYS_2026)[0]
        parts = first_holiday.split("-")
        dt = datetime(int(parts[0]), int(parts[1]), int(parts[2]))
        assert not is_trading_day(dt)


def test_trading_day_fetch_range_returns_strings():
    start, end, keep = trading_day_fetch_range(n=3)
    assert isinstance(start, str)
    assert isinstance(end, str)
    assert isinstance(keep, int)
    assert keep >= 1
    # start <= end
    assert start <= end
