"""Database operations for Stock Chart Analyzer.

Extracted from win11/app.py: SQLite schema initialisation, K-line CRUD,
metadata loading, freshness checks, and data-status inspection.

All public functions accept an optional ``db_path`` parameter that defaults
to ``None``, which falls back to ``common.config.DB_PATH``.  This makes every
function testable with in-memory or temporary databases.

Compatible with Python 3.8+.

简介：SQLite 数据层——建表、K 线写入/读取、股票元数据、数据新鲜度
检查等，被 fetcher/preload/各路由共用。关键设计：所有公开函数都接受
可选 db_path 以便测试；读连接走 WAL 模式的线程本地缓存（get_read_conn），
避免并发读写互相阻塞。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("stockchart")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_db_path(db_path=None):
    # type: (Optional[str]) -> str
    """Return *db_path* if given, otherwise import and return ``common.config.DB_PATH``."""
    if db_path is not None:
        return db_path
    from common.config import DB_PATH
    return DB_PATH


# Thread-local storage for read connections (WAL-mode safe concurrent reads).
_db_read_local = threading.local()


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

def init_db(db_path=None):
    # type: (Optional[str]) -> None
    """Create the SQLite schema (kline_data + stock_metadata tables).

    Safe to call multiple times -- uses ``CREATE TABLE IF NOT EXISTS``.
    """
    path = _resolve_db_path(db_path)
    conn = sqlite3.connect(path, timeout=30.0)
    cursor = conn.cursor()

    # WAL mode: concurrent read/write, writers don't block readers
    try:
        cursor.execute('PRAGMA journal_mode=WAL')
    except Exception as e:
        logger.warning("Failed to set WAL mode: %s", e)

    # K-line data table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS kline_data (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL,
            close REAL,
            high REAL,
            low REAL,
            volume REAL,
            amount REAL,
            amplitude REAL,
            pct_change REAL,
            change_val REAL,
            turnover REAL,
            PRIMARY KEY (code, date)
        )
    ''')

    # Indexes
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_code_date ON kline_data(code, date)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_date ON kline_data(date)'
    )

    # Stock metadata table (real-time quote cache)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS stock_metadata (
            code TEXT PRIMARY KEY,
            name TEXT,
            price REAL,
            change_pct REAL,
            pe REAL,
            pb REAL,
            market_cap REAL,
            volume REAL,
            three_day_change REAL,
            volume_change_pct REAL,
            updated_at TEXT
        )
    ''')

    # Upgrade existing tables with new columns (ignore "duplicate column" errors)
    for col_sql in [
        'ALTER TABLE stock_metadata ADD COLUMN three_day_change REAL',
        'ALTER TABLE stock_metadata ADD COLUMN volume_change_pct REAL',
    ]:
        try:
            cursor.execute(col_sql)
        except sqlite3.OperationalError as e:
            logger.debug("ALTER TABLE skipped (column may already exist): %s", e)

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# K-line write
# ---------------------------------------------------------------------------

def save_kline_to_db(code, df, db_path=None):
    # type: (str, Any, Optional[str]) -> None
    """Upsert K-line rows from a pandas DataFrame into ``kline_data``.

    Args:
        code: Stock ticker code.
        df: pandas DataFrame with columns ``date, open, close, high, low,
            volume, amount`` and optionally ``amplitude, pct_change, change,
            turnover``.
        db_path: Override database path.
    """
    if df.empty:
        return

    path = _resolve_db_path(db_path)
    conn = sqlite3.connect(path, timeout=30.0)
    cursor = conn.cursor()

    rows = []
    for _, row in df.iterrows():
        rows.append((
            code,
            row['date'].strftime('%Y-%m-%d') if hasattr(row['date'], 'strftime') else str(row['date'])[:10],
            row['open'], row['close'], row['high'], row['low'],
            row['volume'], row['amount'],
            row.get('amplitude', None), row.get('pct_change', None),
            row.get('change', None), row.get('turnover', None),
        ))

    if rows:
        cursor.executemany('''
            INSERT OR REPLACE INTO kline_data
            (code, date, open, close, high, low, volume, amount,
             amplitude, pct_change, change_val, turnover)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', rows)
        conn.commit()

    conn.close()


# ---------------------------------------------------------------------------
# Metadata read
# ---------------------------------------------------------------------------

def load_stock_metadata(conn=None, db_path=None):
    # type: (Any, Optional[str]) -> Any
    """Load all rows from ``stock_metadata`` into a pandas DataFrame.

    Returns an empty DataFrame when the table is empty or unreadable.
    """
    import pandas as pd

    close_conn = False
    if conn is None:
        path = _resolve_db_path(db_path)
        conn = sqlite3.connect(path, timeout=30.0)
        close_conn = True
    try:
        df = pd.read_sql_query(
            '''SELECT code, name, price, change_pct, pe, pb, market_cap, volume
               FROM stock_metadata''',
            conn,
        )
    except Exception:
        df = pd.DataFrame()
    if close_conn:
        conn.close()
    return df


# ---------------------------------------------------------------------------
# K-line read
# ---------------------------------------------------------------------------

def get_kline_from_db(code, days=120, db_path=None):
    # type: (str, int, Optional[str]) -> Any
    """Retrieve K-line data for *code* from SQLite.

    In sync mode (``STOCKCHART_SYNC_MODE=1``) all rows are returned;
    otherwise only rows within ``days * 2`` calendar days of today.
    The result is trimmed to the latest *days* rows.
    """
    import pandas as pd

    path = _resolve_db_path(db_path)
    conn = sqlite3.connect(path)
    sync_mode = os.environ.get("STOCKCHART_SYNC_MODE", "") == "1"
    if sync_mode:
        query = """
            SELECT code, date, open, close, high, low, volume, amount,
                   amplitude, pct_change, change_val, turnover
            FROM kline_data WHERE code = ? ORDER BY date
        """
        df = pd.read_sql_query(query, conn, params=(code,))
    else:
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (
            datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=days * 2)
        ).strftime("%Y-%m-%d")
        query = """
            SELECT code, date, open, close, high, low, volume, amount,
                   amplitude, pct_change, change_val, turnover
            FROM kline_data WHERE code = ? AND date >= ? AND date <= ?
            ORDER BY date
        """
        df = pd.read_sql_query(query, conn, params=(code, start_date, end_date))
    conn.close()
    if df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    if len(df) > days:
        df = df.tail(days).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Fast read-only connection (thread-local, WAL-safe)
# ---------------------------------------------------------------------------

def get_read_conn(db_path=None):
    # type: (Optional[str]) -> sqlite3.Connection
    """Return a SQLite read connection.

    * ``db_path`` is None → returns a cached thread-local connection to the
      default database (reused across calls on the same thread; do NOT close
      it, it belongs to this module).
    * ``db_path`` is given → always opens a FRESH connection to that file and
      the CALLER is responsible for closing it. It is intentionally not
      cached, because a thread-local cache cannot hold connections to
      different databases at the same time.
    """
    if db_path is not None:
        return sqlite3.connect(db_path, check_same_thread=False)

    conn = getattr(_db_read_local, 'conn', None)
    if conn is None:
        conn = sqlite3.connect(_resolve_db_path(None), check_same_thread=False)
        _db_read_local.conn = conn
    return conn


# ---------------------------------------------------------------------------
# Fast last-N-bars reader (single code)
# ---------------------------------------------------------------------------

def read_last_bars_from_db(code, n, end_date_str, db_path=None):
    # type: (str, int, str, Optional[str]) -> Optional[Tuple]
    """Read the latest *n* ``(close, volume)`` bars directly from SQLite.

    Returns a tuple of rows sorted by date ascending, or ``None`` when fewer
    than 2 rows are available.
    """
    try:
        if db_path is not None:
            conn = sqlite3.connect(db_path, check_same_thread=False)
            close_after = True
        else:
            conn = get_read_conn(db_path)
            close_after = False

        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT close, volume FROM kline_data
                WHERE code = ? AND date <= ?
                ORDER BY date DESC LIMIT ?
            ''', (code, end_date_str, n))
            rows = cursor.fetchall()
        finally:
            if close_after:
                conn.close()

        if not rows or len(rows) < 2:
            return None
        return tuple(reversed(rows))
    except Exception as e:
        logger.debug('read_last_bars_from_db(%s) 失败: %s', code, e)
        return None


# ---------------------------------------------------------------------------
# Batch last-N-bars reader (multiple codes, single query)
# ---------------------------------------------------------------------------

def read_last_bars_batch(codes, n, end_date_str, db_path=None):
    # type: (List[str], int, str, Optional[str]) -> Dict[str, Optional[Tuple]]
    """Read the latest *n* ``(close, volume)`` bars for multiple codes in one query.

    Returns ``{code: tuple_of_rows_or_None}``.  Uses ``WHERE code IN (...)``
    to avoid the N+1 query pattern when many codes share the same end date.
    """
    result = {code: None for code in codes}
    if not codes:
        return result

    try:
        if db_path is not None:
            conn = sqlite3.connect(db_path, check_same_thread=False)
            close_after = True
        else:
            conn = get_read_conn(db_path)
            close_after = False

        try:
            placeholders = ','.join('?' for _ in codes)
            cursor = conn.cursor()
            cursor.execute(
                '''SELECT code, close, volume FROM kline_data
                   WHERE code IN ({}) AND date <= ?
                   ORDER BY code, date DESC'''.format(placeholders),
                list(codes) + [end_date_str],
            )
            # Group rows by code, keep only the first n per code
            grouped = {}  # type: Dict[str, List]
            for row_code, close_val, vol_val in cursor.fetchall():
                bucket = grouped.setdefault(row_code, [])
                if len(bucket) < n:
                    bucket.append((close_val, vol_val))
        finally:
            if close_after:
                conn.close()

        for code, rows in grouped.items():
            if len(rows) >= 2:
                result[code] = tuple(reversed(rows))
    except Exception as e:  # nosec B110 - 单个代码查询失败不应中断批量读取
        logger.warning('read_last_bars_batch 失败: %s', e)

    return result


# ---------------------------------------------------------------------------
# Per-stock freshness check
# ---------------------------------------------------------------------------

def is_data_fresh(code, db_path=None):
    # type: (str, Optional[str]) -> bool
    """Check whether the database contains data for *code* up to the latest
    completed trading day.
    """
    from common.trading_calendar import trading_day_fetch_range

    path = _resolve_db_path(db_path)
    conn = sqlite3.connect(path)
    cursor = conn.cursor()

    cursor.execute('SELECT MAX(date) FROM kline_data WHERE code = ?', (code,))
    result = cursor.fetchone()
    conn.close()

    if result is None or result[0] is None:
        return False

    latest_date = datetime.strptime(result[0], '%Y-%m-%d').date()
    _, end_str, _ = trading_day_fetch_range(n=1)
    latest_trading_day = datetime.strptime(end_str, '%Y-%m-%d').date()

    return latest_date >= latest_trading_day


# ---------------------------------------------------------------------------
# Local data status inspection
# ---------------------------------------------------------------------------

def check_local_data_status(db_path=None):
    # type: (Optional[str]) -> Dict[str, Any]
    """Inspect local data freshness using file mtime + content heuristics.

    Returns a dict with keys: ``fresh``, ``latest_date``, ``stock_count``,
    ``data_dir``, ``file_mtime``, ``file_size``, ``trading_days``,
    ``has_30_days``, ``data_update_time``.
    """
    from common.config import DATA_DIR
    from common.trading_calendar import is_trading_day

    path = _resolve_db_path(db_path)
    result = {
        'fresh': False,
        'latest_date': None,
        'stock_count': 0,
        'data_dir': str(DATA_DIR),
        'file_mtime': None,
        'file_size': 0,
        'trading_days': 0,
        'has_30_days': False,
        'data_update_time': None,
    }  # type: Dict[str, Any]

    try:
        db_file = Path(path)
        if not db_file.exists():
            return result

        mtime = db_file.stat().st_mtime
        mtime_dt = datetime.fromtimestamp(mtime)
        result['file_mtime'] = mtime_dt.strftime('%Y-%m-%d %H:%M:%S')
        result['file_size'] = db_file.stat().st_size

        now = datetime.now()
        hours_since_modified = (now - mtime_dt).total_seconds() / 3600

        conn = sqlite3.connect(path, timeout=10.0)
        cursor = conn.cursor()

        # Latest date in the DB
        cursor.execute('SELECT MAX(date) FROM kline_data')
        row = cursor.fetchone()
        if not row or not row[0]:
            conn.close()
            return result

        latest_date = row[0]
        result['latest_date'] = latest_date

        # Stock count on latest date
        cursor.execute(
            'SELECT COUNT(DISTINCT code) FROM kline_data WHERE date = ?',
            (latest_date,),
        )
        row = cursor.fetchone()
        result['stock_count'] = row[0] if row else 0

        # Total trading days
        cursor.execute('SELECT COUNT(DISTINCT date) FROM kline_data')
        result['trading_days'] = cursor.fetchone()[0]
        result['has_30_days'] = result['trading_days'] >= 30

        # Real-time metadata update timestamp
        try:
            cursor.execute('SELECT MAX(updated_at) FROM stock_metadata')
            up = cursor.fetchone()
            result['data_update_time'] = up[0] if up and up[0] else None
        except Exception as e:
            logger.debug("Failed to read data update time: %s", e)

        conn.close()

        # --- Freshness heuristics ---
        # 以下是一组宽松的"数据还算新鲜"启发式判断：本地库没有实时行情，
        # 只能靠最新数据日期 + 文件修改时间 + 是否节假日来近似判断，
        # 满足任一条件即认为无需重新预加载。
        today_str = now.strftime('%Y-%m-%d')
        latest_dt = datetime.strptime(latest_date, '%Y-%m-%d')

        # Data date is today and file was modified recently
        if latest_date == today_str and hours_since_modified < 24:
            result['fresh'] = True
            return result

        # Data date is yesterday
        yesterday = now - timedelta(days=1)
        if latest_date == yesterday.strftime('%Y-%m-%d'):
            if not is_trading_day(now):
                result['fresh'] = True
                return result
            if now.hour >= 15 and hours_since_modified < 24:
                result['fresh'] = True
                return result

        # Data date is last Friday and today is Monday
        if now.weekday() == 0:
            friday = now - timedelta(days=3)
            if latest_date == friday.strftime('%Y-%m-%d') and hours_since_modified < 72:
                result['fresh'] = True
                return result

        # Holiday check: no trading days between data date and today
        check_day = latest_dt + timedelta(days=1)
        has_trading_day_after = False
        while check_day.date() <= now.date():
            if is_trading_day(check_day):
                has_trading_day_after = True
                break
            check_day += timedelta(days=1)

        if not has_trading_day_after and result['stock_count'] >= 500:
            result['fresh'] = True
            return result

        # File modified today with sufficient stock count
        if hours_since_modified < 24 and result['stock_count'] >= 500:
            result['fresh'] = True
            return result

    except Exception as e:
        logger.warning("[check_local_data_status] %s", e)

    return result
