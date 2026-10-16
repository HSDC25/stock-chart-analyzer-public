#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云端服务器数据采集脚本（部署在 Linux 云服务器，由 cron 定时运行）。

流程：baostock 增量抓取 A 股日 K 线 + 腾讯行情 API 抓实时快照 →
写入 SQLite（WAL 模式）→ 清理旧数据并 VACUUM → 生成 fetch_state.json；
数据库经 nginx 提供下载，客户端（win11/launcher_webview_sync.py、
win7/launcher_win7_fast.py）只与本脚本产出的数据库/状态文件对接。
"""

import os
import sys
import time
import json
import gzip
import sqlite3
import logging
import random  # nosec B311 - 仅用于重试抖动，非安全场景
import warnings
import threading
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

warnings.filterwarnings('ignore')

# ---------------------------------------------------------------------------
# 配置外部化（安全修复：支持环境变量覆盖硬编码路径）
# ---------------------------------------------------------------------------
DB_DIR = os.environ.get('STOCKCHART_DB_DIR', '/var/www/stockdata')
DB_PATH = os.path.join(DB_DIR, 'stock_data.db')
LOG_PATH = os.path.join(DB_DIR, 'fetch.log')
STATE_FILE = os.path.join(DB_DIR, 'fetch_state.json')

TRADING_DAYS = int(os.environ.get('STOCKCHART_TRADING_DAYS', '30'))
CALENDAR_LOOKBACK = int(os.environ.get('STOCKCHART_CALENDAR_LOOKBACK', '90'))
MAX_WORKERS = int(os.environ.get('STOCKCHART_MAX_WORKERS', '5'))
RATE_LIMIT_SLEEP = float(os.environ.get('STOCKCHART_RATE_LIMIT_SLEEP', '0.05'))
BATCH_PAUSE_EVERY = int(os.environ.get('STOCKCHART_BATCH_PAUSE_EVERY', '500'))
BATCH_PAUSE_SEC = int(os.environ.get('STOCKCHART_BATCH_PAUSE_SEC', '2'))

# 日志文件位于 DB_DIR 下，必须先确保目录存在——否则首次部署时
# import 阶段 FileHandler 就会抛 FileNotFoundError，脚本根本起不来
os.makedirs(DB_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_PATH, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def init_db():
    """Create / open SQLite, ensure schema exists."""
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    # WAL 模式：写入不阻塞读，采集脚本落库时 nginx 侧的下载/校验不受影响
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS kline_data (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL, close REAL, high REAL, low REAL,
            volume REAL, amount REAL,
            amplitude REAL, pct_change REAL, change_val REAL, turnover REAL,
            PRIMARY KEY (code, date)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS stock_metadata (
            code TEXT PRIMARY KEY,
            name TEXT, price REAL, change_pct REAL,
            pe REAL, pb REAL, market_cap REAL, volume REAL,
            three_day_change REAL, volume_change_pct REAL,
            updated_at TEXT
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_kline_code ON kline_data(code)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_kline_date ON kline_data(date)')
    conn.commit()
    return conn


def get_existing_code_dates(conn):
    """Return dict: code -> set of dates already in DB."""
    cur = conn.execute('SELECT code, date FROM kline_data')
    result = {}
    for code, date in cur.fetchall():
        result.setdefault(code, set()).add(date)
    return result


def cleanup_old_data(conn, keep_days=60):
    """Delete K-line data older than keep_days calendar days."""
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime('%Y-%m-%d')
    before = conn.execute('SELECT COUNT(*) FROM kline_data').fetchone()[0]
    conn.execute('DELETE FROM kline_data WHERE date < ?', (cutoff,))
    conn.commit()
    after = conn.execute('SELECT COUNT(*) FROM kline_data').fetchone()[0]
    logger.info(f'Cleaned data older than {cutoff}: {before} -> {after} rows')


# ---------------------------------------------------------------------------
# Trading calendar (akshare)
# ---------------------------------------------------------------------------

def get_recent_trading_days(n=30):
    """Use akshare trade calendar to get last n trading days."""
    import akshare as ak

    today = datetime.now()
    trading_days = []

    try:
        logger.info('Fetching trading calendar via akshare...')
        df = ak.tool_trade_date_hist_sina()
        all_dates = sorted(df['trade_date'].astype(str).tolist())
        today_str = today.strftime('%Y-%m-%d')
        past_dates = [d for d in all_dates if d <= today_str]
        trading_days = past_dates[-n:]
        logger.info(f'Got {len(trading_days)} trading days from calendar')
    except Exception as e:  # nosec B110
        logger.warning(f'akshare calendar failed: {e}, falling back to weekday filter')

    if not trading_days:
        d = today
        while len(trading_days) < n:
            if d.weekday() < 5:
                trading_days.append(d.strftime('%Y-%m-%d'))
            d -= timedelta(days=1)
        trading_days.reverse()

    return trading_days


# ---------------------------------------------------------------------------
# Stock list (akshare)
# ---------------------------------------------------------------------------

def is_allowed_stock_code(code):
    """Keep: Shanghai main (6xx), Shenzhen main (0xx), ChiNext (3xx).
    Exclude: STAR (688), BSE (4/8/9), 388."""
    if not code:
        return False
    code = str(code)
    if code.startswith('688') or code.startswith('388'):
        return False
    return code.startswith(('6', '0', '3'))


def fetch_stock_list():
    """Fetch all A-share stock codes + names via akshare."""
    import akshare as ak

    logger.info('Fetching stock list via akshare stock_info_a_code_name...')
    df = ak.stock_info_a_code_name()

    # Filter
    df['code'] = df['code'].astype(str).str.zfill(6)
    df = df[df['code'].apply(is_allowed_stock_code)]

    # Save names to metadata table (basic info only, no real-time quotes)
    logger.info(f'Got {len(df)} A-share stocks')
    return df[['code', 'name']].copy()


def save_stock_names(conn, df_stocks):
    """Save stock code+name to metadata table (upsert)."""
    rows = []
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for _, row in df_stocks.iterrows():
        rows.append((row['code'], row['name'], now))
    conn.executemany('''
        INSERT INTO stock_metadata (code, name, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(code) DO UPDATE SET
            name = excluded.name,
            updated_at = excluded.updated_at
    ''', rows)
    conn.commit()
    logger.info(f'Saved names for {len(rows)} stocks')


# ---------------------------------------------------------------------------
# Real-time spot data (Tencent finance API)
# ---------------------------------------------------------------------------

def _build_tencent_secid(code):
    """Convert stock code to Tencent format: 600000 -> sh600000, 000001 -> sz000001."""
    if code.startswith(('6', '9')):
        return f'sh{code}'
    return f'sz{code}'


def _fetch_tencent_batch(secids):
    """Fetch one batch from Tencent quote API. Returns list of dicts."""
    url = f'https://qt.gtimg.cn/q={",".join(secids)}'
    for attempt in range(5):
        try:
            req = urllib.request.Request(url)
            req.add_header('User-Agent',
                           'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
            req.add_header('Referer', 'https://stockapp.finance.qq.com/')
            with urllib.request.urlopen(req, timeout=20) as resp:  # nosec B310 - 腾讯行情 API，URL 为硬编码
                raw = resp.read()
                if resp.headers.get('Content-Encoding') == 'gzip':
                    raw = gzip.decompress(raw)
                body = raw.decode('gbk', errors='ignore')
            break
        except Exception as e:  # nosec B110
            if attempt == 4:
                logger.warning(f'Tencent batch failed after 5 retries: {e}')
                return []
            time.sleep(min(2 ** attempt + random.uniform(0.3, 1.0), 15))  # nosec B311 - 重试抖动

    results = []
    for line in body.split(';'):
        line = line.strip()
        if '=' not in line or '"' not in line:
            continue
        data = line.split('"')[1] if '"' in line else ''
        fields = data.split('~')
        if len(fields) < 47:
            continue
        code = fields[2]
        price = fields[3]
        if not price or price == '-' or price == '':
            continue
        try:
            results.append({
                'code': code,
                'name': fields[1],
                'price': float(price),
                'change_pct': float(fields[32]) if fields[32] else 0.0,
                'pe': float(fields[39]) if fields[39] else 0.0,
                'pb': float(fields[46]) if fields[46] else 0.0,
                'market_cap': float(fields[44]) if fields[44] else 0.0,
                'volume': float(fields[6]) * 100 if fields[6] else 0.0,
            })
        except (ValueError, IndexError):
            continue
    return results


def fetch_realtime_spot(conn, codes):
    """Fetch real-time spot data (price/PE/PB/market_cap) via Tencent API and save to DB."""
    if not codes:
        return 0

    secids = [_build_tencent_secid(c) for c in codes]
    batch_size = 50
    total_batches = (len(secids) + batch_size - 1) // batch_size
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    written = 0

    logger.info(f'Fetching real-time spot data for {len(codes)} stocks via Tencent API...')

    for i in range(0, len(secids), batch_size):
        if i > 0:
            time.sleep(0.3)

        chunk = secids[i:i + batch_size]
        items = _fetch_tencent_batch(chunk)

        if items:
            rows = []
            for it in items:
                rows.append((
                    it['code'], it['name'], it['price'],
                    it['change_pct'], it['pe'], it['pb'],
                    it['market_cap'], it['volume'], now_str,
                ))
            conn.executemany('''
                INSERT OR REPLACE INTO stock_metadata
                (code, name, price, change_pct, pe, pb, market_cap, volume, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', rows)
            written += len(rows)

        batch_num = i // batch_size + 1
        if batch_num % 20 == 0 or batch_num == total_batches:
            logger.info(f'Spot data progress: {batch_num}/{total_batches} batches, '
                        f'{written} stocks written')

    conn.commit()
    logger.info(f'Real-time spot data complete: {written} stocks updated')
    return written


# ---------------------------------------------------------------------------
# K-line fetching (baostock)
# ---------------------------------------------------------------------------

# Thread-local baostock sessions
# baostock 连接非线程安全，用 thread-local 保存各线程独立会话
_thread_local = threading.local()
_lock = threading.Lock()
_stats = {'fetched': 0, 'skipped': 0, 'errors': 0, 'total_rows': 0}
_consecutive_failures = 0  # track consecutive errors for auto-reconnect
RECONNECT_EVERY = 300       # proactive re-login every N stocks
MAX_RETRIES = 2             # retry count per stock on failure


def _reconnect_bs():
    """Logout and re-login baostock to get a fresh connection."""
    import baostock as bs
    try:
        bs.logout()
    except Exception as e:  # nosec B110
        pass
    time.sleep(1)
    lg = bs.login()
    if lg.error_code == '0':
        _thread_local.bs_logged_in = True
        logger.info('baostock reconnected successfully')
        return True
    else:
        logger.error(f'baostock reconnect failed: {lg.error_msg}')
        _thread_local.bs_logged_in = False
        return False


def _get_bs_session():
    """Get or create a thread-local baostock session."""
    import baostock as bs
    if not hasattr(_thread_local, 'bs_logged_in') or not _thread_local.bs_logged_in:
        lg = bs.login()
        if lg.error_code == '0':
            _thread_local.bs_logged_in = True
        else:
            raise Exception(f'baostock login failed: {lg.error_msg}')
    return bs


def _do_query_one_stock(code, start_date, end_date):
    """Single attempt to query one stock. Returns (rows, ok). rows is list of tuples."""
    global _consecutive_failures
    bs = _get_bs_session()

    # Convert code to baostock format: 600000 -> sh.600000, 000001 -> sz.000001
    if code.startswith('6'):
        bs_code = f'sh.{code}'
    else:
        bs_code = f'sz.{code}'

    rs = bs.query_history_k_data_plus(
        bs_code,
        'date,open,high,low,close,volume,amount,turn,pctChg',
        start_date=start_date,
        end_date=end_date,
        frequency='d',
        adjustflag='2'  # forward-adjusted
    )

    if rs.error_code != '0':
        return [], False

    rows = []
    while rs.error_code == '0' and rs.next():
        r = rs.get_row_data()
        date_str = r[0]
        try:
            rows.append((
                code, date_str,
                float(r[1]) if r[1] else 0.0,   # open
                float(r[4]) if r[4] else 0.0,   # close
                float(r[2]) if r[2] else 0.0,   # high
                float(r[3]) if r[3] else 0.0,   # low
                float(r[5]) if r[5] else 0.0,   # volume
                float(r[6]) if r[6] else 0.0,   # amount
                0.0,                             # amplitude
                float(r[8]) if r[8] else 0.0,   # pct_change
                0.0,                             # change_val
                float(r[7]) if r[7] else 0.0,   # turnover
            ))
        except (ValueError, IndexError):
            continue

    return rows, True


def fetch_one_stock_baostock(code, start_date, end_date):
    """Fetch K-line for one stock via baostock with retry + reconnect."""
    global _consecutive_failures

    for attempt in range(MAX_RETRIES + 1):
        try:
            rows, ok = _do_query_one_stock(code, start_date, end_date)
            if ok:
                _consecutive_failures = 0
                return rows
            # baostock returned error code but didn't crash - likely a bad stock code
            # Don't retry, just return empty
            with _lock:
                _stats['errors'] += 1
            return []
        except Exception as e:  # nosec B110
            if attempt < MAX_RETRIES:
                logger.debug(f'{code} attempt {attempt+1} failed: {e}, reconnecting...')
                if _reconnect_bs():
                    time.sleep(0.5)
                    continue
            with _lock:
                _stats['errors'] += 1
            with _lock:
                _consecutive_failures += 1
            return []

    with _lock:
        _stats['errors'] += 1
    return []


def fetch_all_klines(codes, trading_days, conn):
    """Incrementally fetch K-line data for all codes using baostock."""
    start_date = trading_days[0]
    end_date = trading_days[-1]
    trading_days_set = set(trading_days)

    # Check what's already in DB
    existing = get_existing_code_dates(conn)

    # Determine which codes need fetching
    need_fetch = []
    for code in codes:
        ex_dates = existing.get(code, set())
        missing = trading_days_set - ex_dates
        if missing:
            need_fetch.append(code)

    total = len(need_fetch)
    logger.info(f'Incremental fetch: {total} stocks need update '
                f'({len(codes) - total} already up-to-date out of {len(codes)})')

    if total == 0:
        logger.info('All data is up-to-date, nothing to fetch')
        return

    _stats['fetched'] = 0
    _stats['skipped'] = len(codes) - total
    _stats['errors'] = 0
    _stats['total_rows'] = 0

    batch_all = []
    batch_lock = threading.Lock()

    def flush_batch():
        nonlocal batch_all
        with batch_lock:
            if not batch_all:
                return
            to_insert = batch_all
            batch_all = []
        try:
            conn.executemany('''
                INSERT OR REPLACE INTO kline_data
                (code, date, open, close, high, low, volume, amount,
                 amplitude, pct_change, change_val, turnover)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', to_insert)
            conn.commit()
            with _lock:
                _stats['total_rows'] += len(to_insert)
        except Exception as e:  # nosec B110
            logger.error(f'Batch insert error: {e}')

    processed = 0
    # baostock uses a single TCP connection; proactively reconnect to avoid Broken pipe
    for code in need_fetch:
        # Proactive reconnect every RECONNECT_EVERY stocks
        if processed > 0 and processed % RECONNECT_EVERY == 0:
            logger.info(f'Proactive reconnect after {processed} stocks')
            _reconnect_bs()

        # Auto-reconnect if too many consecutive failures
        if _consecutive_failures >= 5:
            logger.warning(f'{_consecutive_failures} consecutive failures, forcing reconnect')
            if _reconnect_bs():
                time.sleep(2)

        rows = fetch_one_stock_baostock(code, start_date, end_date)
        if rows:
            # Filter to only trading days we want
            rows = [r for r in rows if r[1] in trading_days_set]
            batch_all.extend(rows)

        processed += 1
        _stats['fetched'] += 1

        # Flush batch periodically
        if len(batch_all) >= 5000:
            flush_batch()

        # Progress logging
        # 批量暂停：给数据端降温，降低长时间高频请求被断连/限流的概率
        if processed % BATCH_PAUSE_EVERY == 0:
            flush_batch()
            logger.info(f'Progress: {processed}/{total} fetched, '
                        f'{_stats["errors"]} errors, {_stats["total_rows"]} rows total')
            time.sleep(BATCH_PAUSE_SEC)

        # 请求间限速：小幅 sleep 平滑请求速率，避免触发 baostock 风控
        time.sleep(RATE_LIMIT_SLEEP)

    # Final flush
    flush_batch()
    logger.info(f'Fetch complete: {_stats["fetched"]} fetched, '
                f'{_stats["skipped"]} skipped, {_stats["errors"]} errors, '
                f'{_stats["total_rows"]} rows inserted')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    logger.info('=' * 60)
    logger.info('Stock data fetch started')
    logger.info('=' * 60)

    # Random delay: 0-55 min within the 16:00-17:00 window
    # 随机打散启动时刻，避免定时任务总在同一秒集中访问数据源
    delay_min = random.randint(0, 55)  # nosec B311 - 随机延迟
    logger.info(f'Random delay: {delay_min} minutes before starting')
    time.sleep(delay_min * 60)

    conn = init_db()

    try:
        # 1. Get trading calendar
        trading_days = get_recent_trading_days(TRADING_DAYS)
        if not trading_days:
            logger.error('No trading days found!')
            return
        logger.info(f'Trading days: {trading_days[0]} to {trading_days[-1]} '
                    f'({len(trading_days)} days)')

        # 2. Fetch stock list
        try:
            df_stocks = fetch_stock_list()
            save_stock_names(conn, df_stocks)
            codes = df_stocks['code'].tolist()
        except Exception as e:  # nosec B110
            logger.error(f'Failed to fetch stock list: {e}')
            cur = conn.execute('SELECT DISTINCT code FROM kline_data')
            codes = [row[0] for row in cur.fetchall()]
            if not codes:
                logger.error('No stocks in DB and cannot fetch list, aborting')
                return
            logger.info(f'Using {len(codes)} codes from existing DB')

        # 3. Login baostock (main thread)
        import baostock as bs
        lg = bs.login()
        if lg.error_code != '0':
            logger.error(f'baostock login failed: {lg.error_msg}')
            return
        logger.info('baostock logged in')
        _thread_local.bs_logged_in = True

        # 4. Incremental K-line fetch (single-threaded for baostock stability)
        fetch_all_klines(codes, trading_days, conn)

        # 5. Logout baostock
        bs.logout()
        _thread_local.bs_logged_in = False

        # 6. Fetch real-time spot data (price/PE/PB/market_cap) via Tencent API
        spot_count = fetch_realtime_spot(conn, codes)
        _stats['spot_updated'] = spot_count

        # 7. Cleanup old data
        cleanup_old_data(conn, keep_days=60)

        # 8. VACUUM
        try:
            conn.execute('VACUUM')
        except Exception as e:  # nosec B110
            logger.warning(f'VACUUM failed: {e}')

        # 9. Log DB size
        conn.close()
        if os.path.exists(DB_PATH):
            size_mb = os.path.getsize(DB_PATH) / 1024 / 1024
            logger.info(f'Database size: {size_mb:.1f} MB')

        # 10. Save state
        state = {
            'last_run': datetime.now().isoformat(),
            'trading_days': trading_days,
            'stock_count': len(codes),
            'stats': _stats,
        }
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2)

        logger.info('Fetch completed successfully')

    except Exception as e:  # nosec B110
        logger.error(f'Fatal error in main: {e}', exc_info=True)
        try:
            import baostock as bs
            bs.logout()
        except Exception as e:  # nosec B110
            pass
        conn.close()
        sys.exit(1)


if __name__ == '__main__':
    main()
