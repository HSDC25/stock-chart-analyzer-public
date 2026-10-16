"""Stock screening and metric computation functions.

Extracted from win11/app.py (lines ~2030-2320).  All functions are public
and importable as ``from common.screening import <name>``.

Compatible with Python 3.8+.

简介：选股计算层——按 FilterCriteria 过滤股票列表、基于 SQLite 计算各种
选股指标（N 日涨幅、量比、涨停回落、大涨天数等）。纯指标计算尽量走
批量 SQL（避免 N+1 查询），供 /api/stocks 等路由调用。
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, List, Optional

import pandas as pd

from common.config import DB_PATH, LIMIT_UP_GEM, LIMIT_UP_MAIN_BOARD
from common.db import read_last_bars_batch, read_last_bars_from_db
from common.models import FilterCriteria
from common.trading_calendar import trading_day_fetch_range

logger = logging.getLogger("stockchart")


# ---------------------------------------------------------------------------
# Core filter
# ---------------------------------------------------------------------------

def filter_stocks(df: pd.DataFrame, criteria: FilterCriteria) -> pd.DataFrame:
    """Filter a stock DataFrame according to *criteria*."""
    if criteria.codes:
        df = df[df['code'].isin(criteria.codes)].copy()
        return df

    if criteria.price_min is not None:
        df = df[df['price'] >= criteria.price_min]
    if criteria.price_max is not None:
        df = df[df['price'] <= criteria.price_max]

    if criteria.change_pct_min is not None:
        df = df[df['change_pct'] >= criteria.change_pct_min]
    if criteria.change_pct_max is not None:
        df = df[df['change_pct'] <= criteria.change_pct_max]

    if criteria.volume_min is not None:
        df = df[df['volume'] >= criteria.volume_min * 10000]

    if criteria.pe_min is not None:
        df = df[df['pe'] >= criteria.pe_min]
    if criteria.pe_max is not None:
        df = df[df['pe'] <= criteria.pe_max]

    if criteria.pb_min is not None:
        df = df[df['pb'] >= criteria.pb_min]
    if criteria.pb_max is not None:
        df = df[df['pb'] <= criteria.pb_max]

    if criteria.market_cap_min is not None:
        df = df[df['market_cap'] >= criteria.market_cap_min]
    if criteria.market_cap_max is not None:
        df = df[df['market_cap'] <= criteria.market_cap_max]

    # Sorting (only 'price' supported; default keeps original order)
    if criteria.sort_by == 'price' and criteria.sort_order in ('asc', 'desc'):
        df = df.sort_values(
            by='price',
            ascending=(criteria.sort_order == 'asc'),
            na_position='last',
        )

    return df.copy()


# ---------------------------------------------------------------------------
# Recent-bar fetcher (network-based, used by _fetch_recent_trading_bars)
# ---------------------------------------------------------------------------

def fetch_recent_trading_bars(code: str, n: int = 3, fetcher: Any = None) -> pd.DataFrame:
    """Fetch the latest *n* completed trading-day K-line bars via *fetcher*.

    During intraday hours today's incomplete bar is skipped.  Returns a
    DataFrame sorted by date ascending.
    """
    try:
        _start_str, end_str, keep = trading_day_fetch_range(n=n)
        if fetcher is None:
            return pd.DataFrame()
        df = fetcher.get_kline_data(code, days=60)  # extra buffer for holidays
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.copy()
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)
        # Exclude bars after end_str (intraday today belongs to "after")
        df = df[df['date'] <= pd.to_datetime(end_str)]
        return df.tail(keep).reset_index(drop=True)
    except Exception as e:
        logger.warning("[fetch_recent_trading_bars] %s: %s", code, e)
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Metric computations (fast SQLite readers)
# ---------------------------------------------------------------------------

def compute_n_day_change(code: str, n: int, end_str: str = None) -> Optional[float]:
    """Cumulative price change over the last *n* trading days (percent).

    Consistent with stock-chart software convention:
      n-day change = (latest_close - close_n_days_ago) / close_n_days_ago * 100.
    Requires *n + 1* bars (bar 0 is the baseline close).
    """
    try:
        if end_str is None:
            _, end_str, _ = trading_day_fetch_range(n=n)
        bars = read_last_bars_from_db(code, n + 1, end_str)
        if bars is None or len(bars) < 2:
            return None
        c0 = bars[0][0]
        cn = bars[-1][0]
        if c0 is None or cn is None or c0 == 0:
            return None
        return round((cn - c0) / c0 * 100, 2)
    except Exception:
        return None


def compute_three_day_change(code: str, end_str: str = None) -> Optional[float]:
    """Cumulative price change over the last 3 trading days (percent).

    Delegates to :func:`compute_n_day_change`.
    """
    return compute_n_day_change(code, 3, end_str)


def compute_volume_change_pct(code: str, end_str: str = None) -> Optional[float]:
    """Volume change rate between the last two trading days (percent)."""
    try:
        if end_str is None:
            _, end_str, _ = trading_day_fetch_range(n=2)
        bars = read_last_bars_from_db(code, 2, end_str)
        if bars is None or len(bars) < 2:
            return None
        v0 = bars[0][1]
        v1 = bars[1][1]
        if v0 is None or v1 is None or v0 == 0:
            return None
        # 取绝对值：筛选关心的是"量能放大/萎缩的幅度"，不区分方向
        return round(abs((v1 - v0) / v0 * 100), 2)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Limit-up helpers
# ---------------------------------------------------------------------------

def is_limit_up(code: str, pct_change: float) -> bool:
    """Return True if *pct_change* qualifies as limit-up for *code*'s board."""
    if code.startswith('3') or code.startswith('688'):
        return pct_change >= LIMIT_UP_GEM       # ChiNext / STAR: 20%
    return pct_change >= LIMIT_UP_MAIN_BOARD     # Main board: 10%


def compute_limit_up_pullback(code: str, conn=None, db_path: str = None) -> Optional[dict]:
    """Detect a limit-up day followed by a 2-4 day pullback of 6-7%.

    Logic:
    - Find the most recent limit-up day in the last 6 K-line bars.
    - Require 2-4 trading days after the limit-up day.
    - Measure max pullback from the limit-up close to subsequent lows.
    - Return details dict when pullback is within 6-7%, else ``None``.
    """
    path = db_path or DB_PATH
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(path, timeout=30.0)
        close_conn = True

    try:
        cur = conn.cursor()

        # Latest 6 bars (limit-up day + up to 4 following days + 1 buffer)
        cur.execute('''
            SELECT date, open, close, high, low, pct_change
            FROM kline_data
            WHERE code = ?
            ORDER BY date DESC
            LIMIT 6
        ''', (code,))
        rows = cur.fetchall()

        if len(rows) < 3:  # need at least limit-up day + 2 following days
            return None

        # rows[0] is the newest trading day; scan backwards for limit-up
        limit_up_idx = None
        for i, row in enumerate(rows):
            _date, _open_p, _close_p, _high_p, _low_p, pct_chg = row
            if pct_chg is not None and is_limit_up(code, pct_chg):
                limit_up_idx = i
                break

        if limit_up_idx is None:
            return None

        limit_up_row = rows[limit_up_idx]
        limit_up_close = limit_up_row[2]   # closing price on limit-up day
        limit_up_date = limit_up_row[0]

        # Number of newer trading days after the limit-up day
        days_after = limit_up_idx

        if days_after < 2 or days_after > 4:
            return None  # require 2-4 post-limit-up trading days

        # Maximum pullback among the days after limit-up
        min_price_after = limit_up_close
        pullback_days = 0

        for i in range(limit_up_idx):
            row = rows[i]
            low_p = row[4]
            if low_p is not None and low_p < min_price_after:
                min_price_after = low_p
                pullback_days = i + 1

        if limit_up_close <= 0:
            return None

        max_pullback_pct = (limit_up_close - min_price_after) / limit_up_close * 100

        if 6.0 <= max_pullback_pct <= 7.0:
            return {
                'pullback_pct': round(max_pullback_pct, 2),
                'pullback_days': pullback_days,
                'limit_up_price': round(limit_up_close, 2),
                'current_price': round(rows[0][2], 2) if rows[0][2] else None,
                'limit_up_date': limit_up_date,
                'days_after_limit_up': days_after,
            }

        return None
    except Exception as e:
        logger.warning("[compute_limit_up_pullback] %s: %s", code, e)
        return None
    finally:
        if close_conn:
            conn.close()


def compute_big_up_days_count(
    code: str,
    days: int = 30,
    threshold: float = 3.5,
    conn=None,
    db_path: str = None,
) -> Optional[int]:
    """Count trading days with pct_change >= *threshold* in the last *days* days."""
    path = db_path or DB_PATH
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(path, timeout=30.0)
        close_conn = True

    try:
        cur = conn.cursor()
        cur.execute('''
            SELECT pct_change
            FROM kline_data
            WHERE code = ? AND pct_change IS NOT NULL
            ORDER BY date DESC
            LIMIT ?
        ''', (code, days))
        rows = cur.fetchall()

        if not rows:
            return None

        count = sum(1 for row in rows if row[0] is not None and row[0] >= threshold)
        return count
    except Exception as e:
        logger.warning("[compute_big_up_days_count] %s: %s", code, e)
        return None
    finally:
        if close_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Batch computation
# ---------------------------------------------------------------------------

def batch_compute_metrics(codes: list, worker_count: int = 20) -> dict:
    """Compute three_day_change and volume_change_pct using batch queries.

    Returns ``{code: {'three_day_change': float|None, 'volume_change_pct': float|None}}``.

    Uses ``read_last_bars_batch`` to fetch all required bars in two SQL queries
    (one for 3-day, one for 2-day) instead of 2*N individual queries.
    """
    if not codes:
        return {}

    # Pre-compute shared end_str values (same trading-day window for all stocks)
    _, end_str_3, _ = trading_day_fetch_range(n=3)
    _, end_str_2, _ = trading_day_fetch_range(n=2)

    # Batch fetch: 4 bars per code for 3-day change (need n+1 = 4 bars)
    bars_3 = read_last_bars_batch(codes, 4, end_str_3)
    # Batch fetch: 2 bars per code for volume change
    bars_2 = read_last_bars_batch(codes, 2, end_str_2)

    results = {}  # type: Dict[str, Dict[str, Optional[float]]]
    for code in codes:
        # 3-day change calculation
        t = None
        rows_3 = bars_3.get(code)
        if rows_3 and len(rows_3) >= 2:
            c0 = rows_3[0][0]
            cn = rows_3[-1][0]
            if c0 is not None and cn is not None and c0 != 0:
                t = round((cn - c0) / c0 * 100, 2)

        # Volume change calculation
        v = None
        rows_2 = bars_2.get(code)
        if rows_2 and len(rows_2) >= 2:
            v0 = rows_2[0][1]
            v1 = rows_2[1][1]
            if v0 is not None and v1 is not None and v0 != 0:
                v = round(abs((v1 - v0) / v0 * 100), 2)

        results[code] = {'three_day_change': t, 'volume_change_pct': v}

    return results
