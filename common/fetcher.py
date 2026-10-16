"""
Stock data fetching infrastructure.

Provides multi-source K-line data retrieval with keep-alive HTTP connections,
provider health monitoring (circuit breaker), anti-blocking utilities, and the
central ``StockDataFetcher`` class.

Extracted from win11/app.py into a reusable, Python 3.8 compatible module.

简介：行情数据抓取层——多数据源（腾讯/新浪/baostock）轮询 + 故障转移
获取 K 线，批量抓实时行情入库，供 preload 与各 API 路由使用。
关键设计：每个数据源独立令牌桶限速 + 熔断器（连续失败进入冷却），
HTTP 连接按线程复用（keep-alive），所有结果带 TTL 进程内缓存。
"""
from __future__ import annotations

import gzip
import json
import logging
import random  # nosec B311 - anti-blocking UA rotation & retry jitter only
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

# Third-party
import requests


class _LazyBaostock:
    """baostock 延迟加载代理：仅在实际走 baostock 回退源时才真正 import。

    baostock 导入约需 0.5~1s 且只有回退路径用到；首次属性访问时导入真实
    模块并替换模块级 ``bs`` 名字，之后所有调用零开销。
    """

    def __getattr__(self, name):
        import baostock
        globals()['bs'] = baostock
        return getattr(baostock, name)


bs = _LazyBaostock()

# Internal
from common.cache import ThreadSafeCache, ThreadSafeDictCache
from common.config import CACHE_TTL_SECONDS, DB_PATH
from common.db import (
    get_kline_from_db as _get_kline_from_db,
    is_data_fresh as _is_data_fresh,
    load_stock_metadata as _load_stock_metadata,
    save_kline_to_db as _save_kline_to_db,
)
from common.models import is_allowed_stock_code, is_stock_st
from common.security import (
    ProviderHealth,
    RateLimiter,
    global_block_detect,
    global_block_success,
    global_block_wait,
)

logger = logging.getLogger("stockchart")


# ============================================================================
# Rate limiters (per-provider Token Bucket instances)
# ============================================================================

sina_limiter = RateLimiter(rate=5, burst=8)
tencent_limiter = RateLimiter(rate=12, burst=16)


# ============================================================================
# Thread-local connection pool & provider state globals
# ============================================================================

#: Thread-local storage for per-thread ``requests.Session`` keep-alive pools.
kline_conn_local = threading.local()

#: Lock protecting ``kline_provider_stats``.
kline_provider_stats_lock = threading.Lock()
#: Per-provider success/failure counters (populated at runtime).
kline_provider_stats = {}  # type: Dict[str, dict]

#: Ordered list of providers for round-robin selection.
kline_provider_order = ["tencent", "sina"]  # Tencent is currently more stable
#: Current index into ``kline_provider_order``.
kline_provider_index = 0
#: Lock protecting ``kline_provider_index``.
kline_provider_index_lock = threading.Lock()


# ============================================================================
# ProviderHealth instance — circuit breaker
# ============================================================================

kline_provider_health = ProviderHealth(fail_threshold=2, cooldown=300)


# ============================================================================
# Anti-blocking: User-Agent pool & random browser headers
# ============================================================================

UA_POOL = [
    # Local real UA (highest weight)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0",
    # Edge neighbour versions (reduce fingerprint uniqueness)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0",
    # Chrome (same kernel version range)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    # Firefox
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:149.0) Gecko/20100101 Firefox/149.0",
    # Chrome (macOS)
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    # Safari (macOS)
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.5 Safari/605.1.15",
]


def random_browser_headers(referer: str = None) -> dict:
    """Generate randomised browser-like request headers.

    随机化 UA/头信息以降低被行情站点反爬封禁的概率；
    Accept 与 Accept-Language 按 UA 对应的浏览器特征保持一致。
    """
    ua = random.choice(UA_POOL)  # nosec B311
    is_firefox = "Firefox/" in ua
    is_safari = "Safari/" in ua and "Chrome/" not in ua

    if is_firefox:
        accept = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        lang = "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7"
    elif is_safari:
        accept = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        lang = "zh-CN,zh-Hans;q=0.9,en;q=0.8"
    else:
        accept = "application/json, text/plain, */*"
        lang = "zh-CN,zh;q=0.9,en;q=0.8"

    h = {
        "User-Agent": ua,
        "Accept": accept,
        "Accept-Language": lang,
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    }
    if referer:
        h["Referer"] = referer
    return h


# ============================================================================
# HTTP keep-alive helpers
# ============================================================================

def http_get_keepalive(url: str, headers: dict, provider: str, timeout: int = 15) -> bytes:
    """Fetch *url* using a thread-local ``requests.Session`` (keep-alive).

    On failure the session is torn down so the next call creates a fresh one.
    """
    if not hasattr(kline_conn_local, "sessions"):
        kline_conn_local.sessions = {}

    s = kline_conn_local.sessions.get(provider)
    if s is None:
        s = requests.Session()
        s.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
        })
        try:
            import certifi
            s.verify = certifi.where()
        except ImportError:
            s.verify = True
        kline_conn_local.sessions[provider] = s

    try:
        resp = s.get(url, headers=headers, timeout=(5, timeout), allow_redirects=True)
        resp.raise_for_status()
        return resp.content
    except Exception:
        try:
            s.close()
        except Exception as e:
            logger.debug("Failed to close session: %s", e)
        kline_conn_local.sessions.pop(provider, None)
        raise


# ============================================================================
# Keep-alive K-line fetchers (Sina / Tencent)
# ============================================================================

def fetch_kline_sina_keepalive(code: str, days: int = 10) -> pd.DataFrame:
    """Fetch daily K-line from Sina using keep-alive connection."""
    sina_limiter.wait()
    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    symbol = "{}{}".format(prefix, code)
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData?symbol={}&scale=240&ma=no&datalen={}".format(symbol, days)
    )
    headers = random_browser_headers(referer="https://finance.sina.com.cn/")
    body = http_get_keepalive(url, headers, "sina")
    # 新浪被反爬拦截时返回的是含"拒绝访问"的页面而非 JSON，需提前识别
    if "\u62d2\u7edd\u8bbf\u95ee".encode("utf-8") in body:
        raise Exception("sina blocked")
    klines = json.loads(body.decode("utf-8", errors="ignore"))
    if not klines:
        return pd.DataFrame()
    rows = []
    prev_close = None
    for k in klines:
        try:
            o = float(k["open"])
            c = float(k["close"])
            h = float(k["high"])
            lo = float(k["low"])
            vol = float(k["volume"])
        except (KeyError, ValueError):
            continue
        pct = round((c - prev_close) / prev_close * 100, 2) if prev_close and prev_close > 0 else None
        prev_close = c
        rows.append({
            "date": k["day"],
            "open": o, "close": c, "high": h, "low": lo,
            "volume": vol, "amount": 0.0, "pct_change": pct,
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def fetch_kline_tencent_keepalive(code: str, days: int = 10) -> pd.DataFrame:
    """Fetch daily K-line from Tencent using keep-alive connection."""
    tencent_limiter.wait()
    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           "?param=" + prefix + code + ",day,,," + str(days) + ",qfq")
    headers = random_browser_headers(referer="https://web.sqt.gtimg.cn/")
    body = http_get_keepalive(url, headers, "tencent")
    payload = json.loads(body.decode("utf-8", errors="ignore"))
    key = "{}{}".format(prefix, code)
    stock_data = payload.get("data", {}).get(key, {})
    klines = stock_data.get("qfqday") or stock_data.get("day") or []
    if not klines:
        return pd.DataFrame()
    rows = []
    prev_close = None
    for k in klines:
        if len(k) < 6:
            continue
        try:
            o, c, h, lo = float(k[1]), float(k[2]), float(k[3]), float(k[4])
            vol = float(k[5]) * 100  # 腾讯返回的单位是"手"，统一换算成"股"
        except (ValueError, IndexError):
            continue
        pct = round((c - prev_close) / prev_close * 100, 2) if prev_close and prev_close > 0 else None
        prev_close = c
        rows.append({
            "date": k[0],
            "open": o, "close": c, "high": h, "low": lo,
            "volume": vol, "amount": 0.0, "pct_change": pct,
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def fetch_kline(code: str, days: int = 10) -> pd.DataFrame:
    """Multi-source K-line fetch with round-robin and failover.

    Tries each healthy provider in rotation; falls back to baostock if all
    web sources fail.

    容错链路：先等全局冷却结束 → 从轮询起点开始依次尝试健康的 web 源
    （每源有独立限速与熔断）→ 全部失败再退回 baostock → 仍失败则触发
    全局封锁检测并抛错。
    """
    global kline_provider_index

    global_block_wait()

    # 轮询起点：每个请求都推进一次游标，把压力均匀分摊到各数据源
    with kline_provider_index_lock:
        start = kline_provider_index
        kline_provider_index = (start + 1) % len(kline_provider_order)

    providers = kline_provider_order[start:] + kline_provider_order[:start]
    last_err = None
    for provider in providers:
        if not kline_provider_health.ok(provider):
            continue
        try:
            if provider == "sina":
                df = fetch_kline_sina_keepalive(code, days)
            else:
                df = fetch_kline_tencent_keepalive(code, days)
            if df is not None and not df.empty:
                kline_provider_health.record_success(provider)
                global_block_success()
                return df
            kline_provider_health.record_fail(provider)
            last_err = Exception("{} returned empty".format(provider))
        except Exception as e:
            kline_provider_health.record_fail(provider)
            last_err = e

    # Web sources exhausted — fall back to baostock (slower but stable)
    try:
        df = _get_kline_baostock_standalone(code, days=days)
        if df is not None and not df.empty:
            global_block_success()
            return df
        last_err = Exception("baostock returned empty")
    except Exception as e:
        last_err = e

    global_block_detect()
    raise Exception("All K-line sources failed {}: {}".format(providers, last_err))


# ============================================================================
# Non-keepalive fetchers (urllib-based, with retries)
# ============================================================================

def fetch_kline_sina(code: str, days: int = 10) -> pd.DataFrame:
    """Fetch daily K-line from Sina Finance API (non-keepalive, with retries)."""
    import urllib.request

    sina_limiter.wait()
    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    symbol = "{}{}".format(prefix, code)
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData?symbol={}&scale=240&ma=no&datalen={}".format(symbol, days)
    )
    last_err = None
    for attempt in range(5):
        try:
            req = urllib.request.Request(
                url, headers=random_browser_headers(referer="https://finance.sina.com.cn/")
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                body = raw.decode("utf-8", errors="ignore")
            klines = json.loads(body)
            if not klines:
                return pd.DataFrame()
            break
        except Exception as e:
            last_err = e
            delay = min(2 ** attempt + random.uniform(0.5, 2.0), 30)  # nosec B311
            time.sleep(delay)
    else:
        raise Exception("Sina K-line failed: {}".format(last_err))

    rows = []
    prev_close = None
    for k in klines:
        try:
            o = float(k["open"])
            c = float(k["close"])
            h = float(k["high"])
            lo = float(k["low"])
            vol = float(k["volume"])
        except (KeyError, ValueError):
            continue
        pct = None
        if prev_close and prev_close > 0:
            pct = round((c - prev_close) / prev_close * 100, 2)
        prev_close = c
        rows.append({
            "date": k["day"],
            "open": o, "close": c, "high": h, "low": lo,
            "volume": vol, "amount": 0.0, "pct_change": pct,
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def fetch_kline_sina_minute(code: str, days: int = 1) -> pd.DataFrame:
    """Fetch minute-level K-line from Sina Finance API."""
    import urllib.request

    sina_limiter.wait()
    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    symbol = "{}{}".format(prefix, code)
    # 5 分钟线：一个交易日约 48 根，datalen 上限 200 由接口限制
    datalen = min(days * 48, 200)
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData?symbol={}&scale=5&ma=no&datalen={}".format(symbol, datalen)
    )
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url, headers=random_browser_headers(referer="https://finance.sina.com.cn/")
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                body = raw.decode("utf-8", errors="ignore")
            klines = json.loads(body)
            if not klines:
                return pd.DataFrame()
            break
        except Exception as e:
            last_err = e
            delay = min(2 ** attempt + random.uniform(0.5, 1.0), 10)  # nosec B311
            time.sleep(delay)
    else:
        raise Exception("Sina minute K-line failed: {}".format(last_err))

    rows = []
    prev_close = None
    for k in klines:
        try:
            o = float(k["open"])
            c = float(k["close"])
            h = float(k["high"])
            lo = float(k["low"])
            vol = float(k["volume"])
        except (KeyError, ValueError):
            continue
        pct = None
        if prev_close and prev_close > 0:
            pct = round((c - prev_close) / prev_close * 100, 2)
        prev_close = c
        rows.append({
            "date": k["day"],
            "open": o, "close": c, "high": h, "low": lo,
            "volume": vol, "amount": 0.0, "pct_change": pct,
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def fetch_kline_tencent(code: str, days: int = 10) -> pd.DataFrame:
    """Fetch daily K-line from Tencent Finance API (non-keepalive, with retries)."""
    import urllib.request

    tencent_limiter.wait()
    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           "?param=" + prefix + code + ",day,,," + str(days) + ",qfq")
    last_err = None
    for attempt in range(5):
        try:
            req = urllib.request.Request(
                url, headers=random_browser_headers(referer="https://web.sqt.gtimg.cn/")
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                body = raw.decode("utf-8", errors="ignore")
            payload = json.loads(body)
            key = "{}{}".format(prefix, code)
            stock_data = payload.get("data", {}).get(key, {})
            klines = stock_data.get("qfqday") or stock_data.get("day") or []
            if not klines:
                return pd.DataFrame()
            break
        except Exception as e:
            last_err = e
            delay = min(2 ** attempt + random.uniform(0.5, 2.0), 30)  # nosec B311
            time.sleep(delay)
    else:
        raise Exception("Tencent K-line failed: {}".format(last_err))

    rows = []
    prev_close = None
    for k in klines:
        if len(k) < 6:
            continue
        try:
            o, c, h, lo = float(k[1]), float(k[2]), float(k[3]), float(k[4])
            vol = float(k[5]) * 100
        except (ValueError, IndexError):
            continue
        pct = None
        if prev_close and prev_close > 0:
            pct = round((c - prev_close) / prev_close * 100, 2)
        prev_close = c
        rows.append({
            "date": k[0],
            "open": o, "close": c, "high": h, "low": lo,
            "volume": vol, "amount": 0.0, "pct_change": pct,
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


# ============================================================================
# Realtime batch fetchers (Tencent)
# ============================================================================

def build_tencent_secid(code: str) -> str:
    """Convert stock code to Tencent security-ID format (e.g. 600000 -> sh600000)."""
    if code.startswith(("6", "9")):
        return "sh{}".format(code)
    return "sz{}".format(code)


def fetch_realtime_tencent_batch(secids: list) -> list:
    """Batch-fetch realtime quotes from Tencent Finance API.

    Parameters
    ----------
    secids : list[str]
        Tencent-format security IDs, e.g. ``['sh600000', 'sz000001']``.

    Returns
    -------
    list[dict]
        Each dict contains: code, name, price, change_pct, pe, pb, market_cap, volume.
    """
    import urllib.request

    tencent_limiter.wait()
    # 腾讯批量行情接口：一次最多约 60 只，返回体是 GBK 编码的 "~" 分隔字段串
    url = "https://qt.gtimg.cn/q={}".format(",".join(secids))
    for attempt in range(5):
        try:
            req = urllib.request.Request(
                url, headers=random_browser_headers(referer="https://stockapp.finance.qq.com/")
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                body = raw.decode("gbk", errors="ignore")
            break
        except Exception as e:
            if attempt == 4:
                logger.warning("[tencent-realtime] batch quote failed (5 retries): %s", e)
                return []
            delay = min(2 ** attempt + random.uniform(0.5, 2.0), 30)  # nosec B311
            time.sleep(delay)

    results = []
    for line in body.split(";"):
        line = line.strip()
        if "=" not in line or '"' not in line:
            continue
        data = line.split('"')[1] if '"' in line else ""
        fields = data.split("~")
        if len(fields) < 47:
            continue
        code = fields[2]
        price = fields[3]
        if not price or price == "-" or price == "":
            continue
        try:
            price_f = float(price)
            change_pct = float(fields[32]) if fields[32] else 0.0
            pe = float(fields[39]) if fields[39] else 0.0
            pb = float(fields[46]) if fields[46] else 0.0
            mcap = float(fields[44]) if fields[44] else 0.0
            vol = float(fields[6]) * 100 if fields[6] else 0.0
        except (ValueError, IndexError):
            continue
        # 字段下标对应腾讯行情协议：3=现价, 32=涨跌幅, 39=PE, 44=总市值, 46=PB, 6=成交量(手)
        results.append({
            "code": code,
            "name": fields[1],
            "price": price_f,
            "change_pct": change_pct,
            "pe": pe,
            "pb": pb,
            "market_cap": mcap,
            "volume": vol,
        })
    return results


def fetch_realtime_tencent_to_db(codes: list, on_progress=None) -> int:
    """Batch-fetch Tencent realtime quotes and persist to ``stock_metadata`` table.

    Parameters
    ----------
    codes : list[str]
        Raw stock codes (e.g. ``['600000', '000001']``).
    on_progress : callable, optional
        Called as ``on_progress(done, total)`` after each batch.

    Returns
    -------
    int
        Number of rows written.
    """
    if not codes:
        return 0

    secids = [build_tencent_secid(c) for c in codes]
    batch_size = 50
    total = len(secids)
    written = 0
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    cursor = conn.cursor()

    for i in range(0, total, batch_size):
        if i > 0:
            time.sleep(0.5)
        chunk = secids[i:i + batch_size]
        items = fetch_realtime_tencent_batch(chunk)

        rows = []
        for it in items:
            rows.append((
                it["code"], it["name"], it["price"], it["change_pct"],
                it["pe"], it["pb"], it["market_cap"], it["volume"], now_str,
            ))

        if rows:
            cursor.executemany(
                "INSERT OR REPLACE INTO stock_metadata"
                " (code, name, price, change_pct, pe, pb, market_cap, volume, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            written += len(rows)

        if on_progress:
            on_progress(min(i + batch_size, total), total)

    conn.commit()
    conn.close()
    return written


# ============================================================================
# Baostock helpers
# ============================================================================

def _get_kline_baostock_standalone(code: str, days: int = 5) -> pd.DataFrame:
    """Standalone baostock K-line fetch (independent login per call).

    Used as the ultimate fallback inside ``fetch_kline()``.
    """
    try:
        lg = bs.login()
        if lg.error_code != "0":
            raise Exception("baostock login failed: {}".format(lg.error_msg))

        bs_code = "sh.{}".format(code) if code.startswith("6") else "sz.{}".format(code)
        end_date = datetime.now()
        start_date = end_date - timedelta(days=15)

        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,open,high,low,close,volume,amount,turn,pctChg",
            start_date=start_date.strftime("%Y-%m-%d"),
            end_date=end_date.strftime("%Y-%m-%d"),
            frequency="d",
            adjustflag="2",
        )

        if rs.error_code != "0":
            bs.logout()
            raise Exception("baostock query failed: {}".format(rs.error_msg))

        data_list = []
        while rs.error_code == "0" and rs.next():
            data_list.append(rs.get_row_data())
        bs.logout()

        if not data_list:
            return pd.DataFrame()

        df = pd.DataFrame(
            data_list,
            columns=["date", "open", "high", "low", "close", "volume", "amount", "turnover", "pct_change"],
        )
        for col in ["open", "high", "low", "close", "volume", "amount", "turnover", "pct_change"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        df["amplitude"] = None
        df["change"] = None
        df = df.sort_values("date").reset_index(drop=True)
        if len(df) > 5:
            df = df.tail(5).reset_index(drop=True)
        return df
    except Exception as e:
        logger.error("Standalone baostock fetch %s failed: %s", code, e)
        return pd.DataFrame()


def preload_with_new_bs_login(code: str) -> pd.DataFrame:
    """Per-thread baostock login + K-line fetch (used during preloading)."""
    return _get_kline_baostock_standalone(code, days=5)


# ============================================================================
# StockDataFetcher — central data access class
# ============================================================================

class StockDataFetcher:
    """Central stock data fetcher with caching and baostock integration.

    Cache fields use ``ThreadSafeCache`` / ``ThreadSafeDictCache`` from
    ``common.cache`` for thread-safe access across Flask request threads.
    """

    def __init__(self):
        # type: () -> None
        self._stock_list_cache = ThreadSafeCache(ttl_seconds=CACHE_TTL_SECONDS)
        self._kline_cache = ThreadSafeDictCache(ttl_seconds=CACHE_TTL_SECONDS)
        self._bs_logged_in = False
        self._bs_lock = threading.Lock()

    # -- public cache accessors for backward compat --------------------------

    @property
    def stock_list_cached_value(self):
        """Raw cached stock list DataFrame (or None)."""
        return self._stock_list_cache._data

    @stock_list_cached_value.setter
    def stock_list_cached_value(self, value):
        self._stock_list_cache._data = value
        self._stock_list_cache._timestamp = datetime.now() if value is not None else None

    def invalidate_stock_list_cache(self):
        # type: () -> None
        """Clear the stock list cache."""
        self._stock_list_cache.invalidate()

    # -- baostock lifecycle --------------------------------------------------

    def _bs_login(self):
        # type: () -> None
        """Login to baostock (idempotent)."""
        if not self._bs_logged_in:
            lg = bs.login()
            if lg.error_code == "0":
                self._bs_logged_in = True
            else:
                logger.warning("baostock login failed: %s", lg.error_msg)

    def _bs_health_check(self):
        # type: () -> None
        """Verify baostock connection is alive; re-login if needed."""
        if not self._bs_logged_in:
            self._bs_login()
            return
        try:
            rs = bs.query_history_k_data_plus(
                "sh.000001", "date",
                start_date="2020-01-02", end_date="2020-01-02",
                frequency="d",
            )
            if rs.error_code != "0":
                raise Exception(rs.error_msg)
        except Exception:
            logger.info("[baostock] connection lost, re-logging in...")
            try:
                bs.logout()
            except Exception as e:
                logger.debug("baostock logout failed: %s", e)
            self._bs_logged_in = False
            self._bs_login()

    def _bs_logout(self):
        # type: () -> None
        """Logout from baostock."""
        if self._bs_logged_in:
            bs.logout()
            self._bs_logged_in = False

    def get_kline_baostock(self, code: str, days: int = 5) -> pd.DataFrame:
        """Thread-safe baostock K-line fetch."""
        with self._bs_lock:
            self._bs_health_check()

            bs_code = "sh.{}".format(code) if code.startswith("6") else "sz.{}".format(code)
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days * 2 + 5)

            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,open,high,low,close,volume,amount,turn,pctChg",
                start_date=start_date.strftime("%Y-%m-%d"),
                end_date=end_date.strftime("%Y-%m-%d"),
                frequency="d",
                adjustflag="2",
            )

            if rs.error_code != "0":
                raise Exception("baostock query failed: {}".format(rs.error_msg))

            data_list = []
            while rs.error_code == "0" and rs.next():
                data_list.append(rs.get_row_data())

            if not data_list:
                return pd.DataFrame()

            df = pd.DataFrame(
                data_list,
                columns=["date", "open", "high", "low", "close", "volume", "amount", "turnover", "pct_change"],
            )
            for col in ["open", "high", "low", "close", "volume", "amount", "turnover", "pct_change"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df["date"] = pd.to_datetime(df["date"])
            df["amplitude"] = None
            df["change"] = None
            df = df.sort_values("date").reset_index(drop=True)
            if len(df) > days:
                df = df.tail(days).reset_index(drop=True)
            return df

    # -- high-level API ------------------------------------------------------

    def get_stock_list(self, refresh: bool = False) -> pd.DataFrame:
        """Get A-share stock list.

        Priority: SQLite ``stock_metadata`` table (instant) > baostock (slow).
        Results are cached for ``CACHE_TTL_SECONDS``.
        """
        if not refresh and self._stock_list_cache.is_valid:
            return self._stock_list_cache._data

        # --- Try SQLite metadata first ---
        try:
            meta = _load_stock_metadata()
            if not meta.empty:
                logger.info("[get_stock_list] SQLite metadata hit: %d stocks", len(meta))
                cols = ["code", "name", "price", "change_pct", "pe", "pb", "market_cap", "volume"]
                df = meta[cols].copy()
                df = df[df["code"].apply(is_allowed_stock_code)].reset_index(drop=True)
                df = df[~df["name"].apply(is_stock_st)].reset_index(drop=True)
                df["turnover"] = 0.0
                self._stock_list_cache._data = df
                self._stock_list_cache._timestamp = datetime.now()
                return df
        except Exception as e:
            logger.info("[get_stock_list] metadata read failed, falling back to baostock: %s", e)

        # --- Fallback: baostock ---
        last_err = None
        for _retry in range(3):
            try:
                try:
                    self._bs_logged_in = False
                    self._bs_login()
                except Exception as e:
                    logger.info("[get_stock_list] baostock login failed: %s", e)
                    last_err = e
                    time.sleep(1)
                    continue

                codes = []
                used_day = None
                for offset in range(6):
                    day = (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
                    try:
                        rs = bs.query_all_stock(day=day)
                    except Exception as e:
                        logger.info("[get_stock_list] query_all_stock(%s) error: %s", day, e)
                        last_err = e
                        break

                    data = []
                    while rs.error_code == "0" and rs.next():
                        row = rs.get_row_data()
                        data.append(row[0])
                    if data:
                        used_day = day
                        for c in data:
                            if c.startswith("sh.") or c.startswith("sz."):
                                codes.append(c[3:])
                        break

                if codes:
                    codes = [c for c in codes if is_allowed_stock_code(c)]
                    logger.info(
                        "[get_stock_list] using baostock list from %s, %d A-shares",
                        used_day, len(codes),
                    )
                    df = pd.DataFrame({"code": codes})
                    df["name"] = ""
                    df["price"] = 0.0
                    df["change_pct"] = 0.0
                    df["turnover"] = 0.0
                    df["pe"] = 0.0
                    df["pb"] = 0.0
                    df["market_cap"] = 0.0
                    df["volume"] = 0.0
                    self._stock_list_cache._data = df
                    self._stock_list_cache._timestamp = datetime.now()
                    return df

                last_err = Exception("baostock returned no stock list for last 5 trading days")
                time.sleep(1)
            except Exception as e:
                last_err = e
                logger.info("[get_stock_list] baostock attempt %d failed: %s", _retry + 1, e)
                time.sleep(1)

        if self._stock_list_cache._data is not None:
            logger.info(
                "[get_stock_list] baostock failed repeatedly, using stale cache (%d rows)",
                len(self._stock_list_cache._data),
            )
            return self._stock_list_cache._data
        raise Exception(
            "baostock failed after multiple attempts; check network (last error: {})".format(last_err)
        )

    def get_kline_data(self, code: str, days: int = 120, use_baostock: bool = True) -> pd.DataFrame:
        """Get K-line data for a stock.

        Checks in-memory cache first, then SQLite, then baostock.
        """
        import os

        cache_key = "{}_{}".format(code, days)

        # Check dict cache
        cached = self._kline_cache._data.get(cache_key)
        cached_ts = self._kline_cache._timestamps.get(cache_key)
        if cached is not None and cached_ts is not None:
            if (datetime.now() - cached_ts).total_seconds() < CACHE_TTL_SECONDS:
                return cached

        sync_mode = os.environ.get("STOCKCHART_SYNC_MODE", "") == "1"

        # Try SQLite（同步模式下数据由外部流程写入，本地读到即返回、不回源网络）
        try:
            df = _get_kline_from_db(code, days)
            if not df.empty:
                if sync_mode or _is_data_fresh(code):
                    with self._kline_cache._lock:
                        self._kline_cache._data[cache_key] = df
                        self._kline_cache._timestamps[cache_key] = datetime.now()
                    return df
        except Exception as e:
            logger.warning("SQLite read for %s K-line failed: %s", code, e)

        if sync_mode:
            return pd.DataFrame()

        # Fall back to baostock
        try:
            df = self.get_kline_baostock(code, days=days)
            if not df.empty:
                try:
                    _save_kline_to_db(code, df)
                except Exception as e:
                    logger.warning("Saving %s K-line to SQLite failed: %s", code, e)
                with self._kline_cache._lock:
                    self._kline_cache._data[cache_key] = df
                    self._kline_cache._timestamps[cache_key] = datetime.now()
                return df
        except Exception as e:
            logger.warning("baostock fetch for %s failed: %s", code, e)
            return pd.DataFrame()


# ============================================================================
# Module-level singleton
# ============================================================================

#: Global ``StockDataFetcher`` instance shared across the application.
fetcher = StockDataFetcher()
