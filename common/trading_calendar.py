"""A-share trading calendar utilities (2026 holidays).

简介：A 股交易日/交易时段判断与"最近 N 个交易日"区间计算，
被数据新鲜度检查、选股指标计算等使用。关键设计：法定节假日表
A_SHARE_HOLIDAYS_2026 为手工维护的硬编码集合，**每年需人工更新**。
"""

from datetime import datetime, timedelta
from typing import Optional, Tuple


# =============================================================================
# A-share holiday calendar (2026 statutory holidays, actual market closure days)
# =============================================================================
A_SHARE_HOLIDAYS_2026 = {
    # New Year's Day
    '2026-01-01', '2026-01-02',
    # Spring Festival (Jan 26 - Feb 1, including adjusted workdays)
    '2026-01-26', '2026-01-27', '2026-01-28', '2026-01-29', '2026-01-30',
    '2026-01-31', '2026-02-01',
    # Qingming Festival
    '2026-04-04', '2026-04-05', '2026-04-06',
    # Labour Day
    '2026-05-01', '2026-05-02', '2026-05-03', '2026-05-04', '2026-05-05',
    # Dragon Boat Festival
    '2026-05-31', '2026-06-01', '2026-06-02',
    # Mid-Autumn Festival
    '2026-09-25', '2026-09-26', '2026-09-27',
    # National Day
    '2026-10-01', '2026-10-02', '2026-10-03', '2026-10-04', '2026-10-05',
    '2026-10-06', '2026-10-07', '2026-10-08',
}


def is_trading_day(d):
    # type: (datetime) -> bool
    """Determine whether a given day is an A-share trading day.

    Simple heuristic: not a weekend and not a statutory holiday.
    """
    if d.weekday() >= 5:
        return False
    if d.strftime('%Y-%m-%d') in A_SHARE_HOLIDAYS_2026:
        return False
    return True


def is_market_open_now(now=None):
    # type: (Optional[datetime]) -> bool
    """Check whether the A-share market is currently in session.

    Trading sessions: 09:30-11:30 and 13:00-15:00 on trading days.
    """
    now = now or datetime.now()
    if not is_trading_day(now):
        return False
    t = now.strftime('%H:%M')
    return ('09:30' <= t <= '11:30') or ('13:00' <= t <= '15:00')


def trading_day_fetch_range(n=3, now=None):
    # type: (int, Optional[datetime]) -> Tuple[str, str, int]
    """Return (start_date, end_date, bars_to_keep) for fetching the latest *n* trading days.

    Rules:
      - During trading hours: today's bar is incomplete, so we start from the
        previous trading day and look back *n* days.
      - After close / weekends / holidays: today (or the last trading day) counts
        towards *n*.

    ``bars_to_keep`` is the number of rows to retain from the tail of the
    resulting DataFrame (sorted by date ascending).
    """
    now = now or datetime.now()
    open_now = is_market_open_now(now)

    # Determine the most recent completed trading day
    cursor = now  # type: datetime
    if open_now:
        # Intraday: today's bar hasn't closed yet, step back one day
        cursor = now - timedelta(days=1)
    # Keep stepping back until we land on a trading day
    while not is_trading_day(cursor):
        cursor -= timedelta(days=1)
    end_date = cursor

    # Walk back n-1 more trading days to find start_date
    steps = n - 1
    walk = end_date
    while steps > 0:
        walk -= timedelta(days=1)
        if is_trading_day(walk):
            steps -= 1
    start_date = walk
    # Add 7 extra calendar days as buffer for holidays / suspensions
    fetch_start = start_date - timedelta(days=7)
    return fetch_start.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d'), n
