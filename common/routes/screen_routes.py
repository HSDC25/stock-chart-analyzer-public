"""Screening API routes Blueprint.

Extracted from win11/app.py (screen_* endpoints).
Business logic is kept inline for now; will be refactored in Phase 3.

Compatible with Python 3.8+.

简介：选股策略蓝图（screen_bp）——首板涨停、涨停放量、放量、连阳、
涨停回落、大涨天数、涨停天数排行等策略筛选，全部直接查本地 SQLite
（kline_data + stock_metadata）。涨幅阈值按板块区分：主板 9.8%、
创业板/科创板 19.5%。股票池为沪深主板 + 创业板（不含科创板/北交所）；
ST/*ST、退市整理期（简称含“退”）与最近才上市（含无涨跌幅期）的次新股统一排除。
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing

from flask import Blueprint, jsonify, request

from common.screening import exclude_risk_sql, is_risk_name, limit_up_sql_case

logger = logging.getLogger("stockchart")

screen_bp = Blueprint('screen', __name__)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _window_start(cur, days):
    """倒数第 *days* 个交易日的日期，用作窗口查询的下界。

    库只增不减（每个交易日约 +4600 行），不加下界的 ROW_NUMBER 会随历史变长
    退化成全表扫描（126 万行实测 1.12s，加下界 0.61s）。历史不足 *days* 天时
    退回最早一天，等价于不设下界。

    调用方会传 3 倍窗口：K 线按"每股最近 N 根"数，如果贴着 N 个交易日切，
    停牌几天或日期不齐的股票就会少算；留 3 倍余量既保住原有语义，
    又能砍掉大部分历史行。
    """
    row = cur.execute(
        'SELECT date FROM kline_data GROUP BY date ORDER BY date DESC LIMIT 1 OFFSET ?',
        (max(int(days), 1) - 1,)).fetchone()
    if row and row[0]:
        return row[0]
    row = cur.execute('SELECT MIN(date) FROM kline_data').fetchone()
    return row[0] if row and row[0] else ''


def _latest_date(cur):
    row = cur.execute('SELECT MAX(date) FROM kline_data').fetchone()
    return row[0] if row and row[0] else None


def _meta_map(cur, codes):
    """一次 IN 查询取回全部元数据（逐股点查在 100 条时虽只要 2ms，但没必要）。"""
    if not codes:
        return {}
    qmarks = ','.join('?' * len(codes))
    rows = cur.execute(
        'SELECT code, name, price, change_pct, pe, pb, market_cap, volume '
        'FROM stock_metadata WHERE code IN (%s)' % qmarks, codes)
    return {r[0]: r for r in rows}


def _attach_meta(hit, meta_row):
    """把 stock_metadata 的一行铺到结果 dict 上，数值统一两位小数。"""
    hit['name'] = meta_row[1] or ''
    for key, value in zip(['price', 'change_pct', 'pe', 'pb', 'market_cap', 'volume'],
                          meta_row[2:]):
        hit[key] = round(float(value), 2) if value is not None else None
    return hit


# 新股上市前 5 个交易日（北交所为首日）不设涨跌幅，涨幅阈值会把它们误判成涨停。
# 首根 K 线晚于"倒数第 25 个交易日"的股票视为次新股，短线筛选一律排除。
MIN_HISTORY_BARS = 25


def _available_date_count(cur):
    row = cur.execute('SELECT COUNT(DISTINCT date) FROM kline_data').fetchone()
    return int(row[0] or 0)


def _fresh_listing_clause(cur, col='code'):
    """返回排除"最近才上市"股票的 SQL 片段。

    判定口径：本地库已有 >= MIN_HISTORY_BARS 个交易日时，首根 K 线晚于
    "倒数第 25 个交易日" 的股票视为次新股/长期停牌复牌。不用"根数不足"，
    避免把数据来源暂时缺历史的股票一并误杀。库刚装好时不做此限制，
    否则全市场都只有几根 K 线、会被清空。
    """
    if _available_date_count(cur) < MIN_HISTORY_BARS:
        return ''
    cutoff = _window_start(cur, MIN_HISTORY_BARS)
    if not cutoff or len(cutoff) != 10:
        return ''
    cutoff = cutoff.replace("'", "''")
    return (" AND {c} NOT IN (SELECT code FROM kline_data GROUP BY code "
            "HAVING MIN(date) > '{d}')").format(c=col, d=cutoff)


def _fresh_listing_codes(cur):
    """Python 路径用的同一份次新股集合（不满足条件时返回空集合）。"""
    if _available_date_count(cur) < MIN_HISTORY_BARS:
        return set()
    cutoff = _window_start(cur, MIN_HISTORY_BARS)
    if not cutoff:
        return set()
    rows = cur.execute(
        'SELECT code FROM kline_data GROUP BY code HAVING MIN(date) > ?',
        (cutoff,)).fetchall()
    return {r[0] for r in rows}


def _int_arg(name, default, lo=None, hi=None):
    """取整数查询参数：坏值退回默认值，再夹到 [lo, hi]。

    不抛 ValueError：`?limit=abc` 或 `?limit=999999999` 应该退化成一次正常查询，
    而不是 500 或把几十万行全捞出来。上限取值只为挡住失控请求，正常前端用不到。
    """
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        value = int(default)
    if lo is not None:
        value = max(value, lo)
    if hi is not None:
        value = min(value, hi)
    return value


def _float_arg(name, default, lo=None, hi=None):
    """取浮点查询参数，语义同 ``_int_arg``。"""
    try:
        value = float(request.args.get(name, default))
    except (TypeError, ValueError):
        value = float(default)
    if lo is not None:
        value = max(value, lo)
    if hi is not None:
        value = min(value, hi)
    return value


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@screen_bp.route('/api/screen/first_limit_up')
def screen_first_limit_up():
    """First limit-up of the day (limit-up today, no limit-up in prior 15 trading days)."""
    try:
        from common.config import DB_PATH

        limit = _int_arg('limit', 100, 1, 1000)
        with closing(sqlite3.connect(DB_PATH, timeout=30.0)) as conn:
            cur = conn.cursor()

            cur.execute('SELECT MAX(date) FROM kline_data')
            latest_date = cur.fetchone()[0]
            if not latest_date:
                return jsonify({'success': False, 'error': 'No data'})

            # 首板要求"此前 15 个交易日无涨停"。用交易日下界，不用 '-14 days'
            # 的自然日窗口（自然日 14 天通常只有 9~10 个交易日）。
            window_floor = _window_start(cur, 15)
            fresh_clause = _fresh_listing_clause(cur, 'm.code')
            cur.execute('''
                WITH recent_limit_up AS (
                    -- 最近 15 个交易日内出现过涨停的股票集合：
                    -- "首板"要求此前两周没有涨停，故用 NOT IN 排除老涨停股
                    SELECT DISTINCT code FROM kline_data
                    WHERE date >= ?
                      AND date < ?
                      AND pct_change >= ''' + limit_up_sql_case('code') + '''
                )
                SELECT m.code, m.name, m.price, m.change_pct, m.pe, m.pb,
                       m.market_cap, m.volume
                FROM stock_metadata m
                WHERE m.change_pct >= ''' + limit_up_sql_case('m.code') + '''
                  AND m.price > 0
                  ''' + exclude_risk_sql('m.name') + '''
                  ''' + fresh_clause + '''
                  AND m.code NOT IN (SELECT code FROM recent_limit_up)
                ORDER BY m.change_pct DESC
                LIMIT ?
            ''', (window_floor, latest_date, limit))
            rows = cur.fetchall()

        columns = ['code', 'name', 'price', 'change_pct', 'pe', 'pb',
                    'market_cap', 'volume']
        results = []
        for row in rows:
            r = dict(zip(columns, row))
            for col in ['price', 'change_pct', 'pe', 'pb', 'market_cap', 'volume']:
                v = r.get(col)
                if v is not None:
                    r[col] = round(float(v), 2)
            results.append(r)

        return jsonify({
            'success': True, 'data': results,
            'total': len(results), 'type': 'first_limit_up',
        })
    except Exception as e:
        logger.exception("Failed to screen first limit-up stocks")
        return jsonify({'success': False, 'error': '服务器内部错误，请稍后重试'}), 500


@screen_bp.route('/api/screen/limit_up_volume')
def screen_limit_up_volume():
    """First limit-up of the day + turnover volume surge >= 2.5x previous day."""
    try:
        from common.config import DB_PATH

        limit = _int_arg('limit', 50, 1, 1000)
        with closing(sqlite3.connect(DB_PATH, timeout=30.0)) as conn:
            cur = conn.cursor()

            cur.execute('SELECT MAX(date) FROM kline_data')
            latest_date = cur.fetchone()[0]
            if not latest_date:
                return jsonify({'success': False, 'error': 'No data'})

            cur.execute('SELECT MAX(date) FROM kline_data WHERE date < ?', (latest_date,))
            prev_date = cur.fetchone()[0]
            if not prev_date:
                return jsonify({'success': False, 'error': 'No previous date'})

            window_floor = _window_start(cur, 15)
            fresh_clause = _fresh_listing_clause(cur, 'm.code')
            cur.execute('''
                WITH recent_limit_up AS (
                    SELECT DISTINCT code FROM kline_data
                    WHERE date >= ?
                      AND date < ?
                      AND pct_change >= ''' + limit_up_sql_case('code') + '''
                )
                SELECT m.code, m.name, m.price, m.change_pct, m.pe, m.pb,
                       m.market_cap, m.volume,
                       ROUND(today.turnover / yesterday.turnover, 2) as turnover_ratio
                FROM stock_metadata m
                JOIN kline_data today ON today.code = m.code AND today.date = ?
                JOIN kline_data yesterday ON yesterday.code = m.code AND yesterday.date = ?
                WHERE m.change_pct >= ''' + limit_up_sql_case('m.code') + '''
                  AND m.price > 0
                  ''' + exclude_risk_sql('m.name') + '''
                  ''' + fresh_clause + '''
                  AND yesterday.turnover > 0
                  AND today.turnover / yesterday.turnover >= 2.5
                  AND m.code NOT IN (SELECT code FROM recent_limit_up)
                ORDER BY turnover_ratio DESC
                LIMIT ?
            ''', (window_floor, latest_date, latest_date, prev_date, limit))
            rows = cur.fetchall()

        columns = ['code', 'name', 'price', 'change_pct', 'pe', 'pb',
                    'market_cap', 'volume', 'turnover_ratio']
        results = []
        for row in rows:
            r = dict(zip(columns, row))
            for col in ['price', 'change_pct', 'pe', 'pb', 'market_cap', 'volume']:
                v = r.get(col)
                if v is not None:
                    r[col] = round(float(v), 2)
            tr = r.get('turnover_ratio')
            if tr is not None:
                r['turnover_ratio'] = round(float(tr), 2)
            results.append(r)

        return jsonify({
            'success': True, 'data': results,
            'total': len(results), 'type': 'limit_up_volume',
        })
    except Exception as e:
        logger.exception("Failed to screen limit-up volume stocks")
        return jsonify({'success': False, 'error': '服务器内部错误，请稍后重试'}), 500


@screen_bp.route('/api/screen/volume_surge')
def screen_volume_surge():
    """Turnover volume surge >= 3.0x vs previous day (no limit-up requirement)."""
    try:
        from common.config import DB_PATH

        limit = _int_arg('limit', 50, 1, 1000)
        with closing(sqlite3.connect(DB_PATH, timeout=30.0)) as conn:
            cur = conn.cursor()

            cur.execute('SELECT MAX(date) FROM kline_data')
            latest_date = cur.fetchone()[0]
            if not latest_date:
                return jsonify({'success': False, 'error': 'No data'})

            cur.execute('SELECT MAX(date) FROM kline_data WHERE date < ?', (latest_date,))
            prev_date = cur.fetchone()[0]
            if not prev_date:
                return jsonify({'success': False, 'error': 'No previous date'})

            fresh_clause = _fresh_listing_clause(cur, 'm.code')
            cur.execute('''
                SELECT m.code, m.name, m.price, m.change_pct, m.pe, m.pb,
                       m.market_cap, m.volume,
                       ROUND(today.turnover / yesterday.turnover, 2) as turnover_ratio,
                       ROUND(today.turnover, 2) as today_turnover,
                       ROUND(yesterday.turnover, 2) as yesterday_turnover
                FROM stock_metadata m
                JOIN kline_data today ON today.code = m.code AND today.date = ?
                JOIN kline_data yesterday ON yesterday.code = m.code AND yesterday.date = ?
                WHERE m.price > 0
                  ''' + exclude_risk_sql('m.name') + '''
                  ''' + fresh_clause + '''
                  AND yesterday.turnover > 0
                  AND today.turnover / yesterday.turnover >= 3.0
                ORDER BY turnover_ratio DESC
                LIMIT ?
            ''', (latest_date, prev_date, limit))
            rows = cur.fetchall()

        columns = ['code', 'name', 'price', 'change_pct', 'pe', 'pb',
                    'market_cap', 'volume', 'turnover_ratio',
                    'today_turnover', 'yesterday_turnover']
        results = []
        for row in rows:
            r = dict(zip(columns, row))
            for col in ['price', 'change_pct', 'pe', 'pb', 'market_cap', 'volume']:
                v = r.get(col)
                if v is not None:
                    r[col] = round(float(v), 2)
            for col in ['turnover_ratio', 'today_turnover', 'yesterday_turnover']:
                v = r.get(col)
                if v is not None:
                    r[col] = round(float(v), 2)
            results.append(r)

        return jsonify({
            'success': True, 'data': results,
            'total': len(results), 'type': 'volume_surge',
        })
    except Exception as e:
        logger.exception("Failed to screen volume surge stocks")
        return jsonify({'success': False, 'error': '服务器内部错误，请稍后重试'}), 500


@screen_bp.route('/api/screen/consecutive_up')
def screen_consecutive_up():
    """5-7 consecutive up-days with cumulative gain <= 20%."""
    try:
        from common.config import DB_PATH

        limit = _int_arg('limit', 100, 1, 1000)
        with closing(sqlite3.connect(DB_PATH, timeout=30.0)) as conn:
            cur = conn.cursor()

            cur.execute('SELECT DISTINCT date FROM kline_data ORDER BY date DESC LIMIT 7')
            dates = [r[0] for r in cur.fetchall()]
            if len(dates) < 5:
                return jsonify({'success': False, 'error': 'Not enough data'})

            d1, d2, d3, d4, d5 = dates[0], dates[1], dates[2], dates[3], dates[4]
            d6 = dates[5] if len(dates) > 5 else None
            d7 = dates[6] if len(dates) > 6 else None

            fresh_clause = _fresh_listing_clause(cur, 'm.code')
            query = '''
                -- 连阳筛选：按最近 7 个交易日日期对 kline_data 自连接 7 次（d1 最新），
                -- 逐日判断"收盘>开盘"得到 y1..y7，累计涨幅从最早一根阳线的开盘价算起；
                -- d6/d7 用 LEFT JOIN 允许不足 7 根阳线（5 连阳即入选）
                WITH base AS (
                    SELECT m.code, m.name, m.price, m.change_pct, m.pe, m.pb,
                           m.market_cap, m.volume,
                           (d1_data.close > d1_data.open) AS y1,
                           (d2_data.close > d2_data.open) AS y2,
                           (d3_data.close > d3_data.open) AS y3,
                           (d4_data.close > d4_data.open) AS y4,
                           (d5_data.close > d5_data.open) AS y5,
                           (d6_data.open IS NOT NULL AND d6_data.close > d6_data.open) AS y6,
                           (d7_data.open IS NOT NULL AND d7_data.close > d7_data.open) AS y7,
                           ROUND((d1_data.close - CASE
                                     WHEN d7_data.open IS NOT NULL
                                          AND d7_data.close > d7_data.open
                                          THEN d7_data.open
                                     WHEN d6_data.open IS NOT NULL
                                          AND d6_data.close > d6_data.open
                                          THEN d6_data.open
                                     ELSE d5_data.open END)
                               / CASE
                                     WHEN d7_data.open IS NOT NULL
                                          AND d7_data.close > d7_data.open
                                          THEN d7_data.open
                                     WHEN d6_data.open IS NOT NULL
                                          AND d6_data.close > d6_data.open
                                          THEN d6_data.open
                                     ELSE d5_data.open END * 100, 2) AS total_gain,
                           CASE
                               WHEN d7_data.open IS NOT NULL
                                    AND d7_data.close > d7_data.open
                                    AND d6_data.open IS NOT NULL
                                    AND d6_data.close > d6_data.open
                                    AND d5_data.close > d5_data.open
                                    AND d4_data.close > d4_data.open
                                    AND d3_data.close > d3_data.open
                                    AND d2_data.close > d2_data.open
                                    AND d1_data.close > d1_data.open THEN 7
                               WHEN d6_data.open IS NOT NULL
                                    AND d6_data.close > d6_data.open
                                    AND d5_data.close > d5_data.open
                                    AND d4_data.close > d4_data.open
                                    AND d3_data.close > d3_data.open
                                    AND d2_data.close > d2_data.open
                                    AND d1_data.close > d1_data.open THEN 6
                               ELSE 5
                           END AS consecutive_days
                    FROM stock_metadata m
                    JOIN kline_data d1_data ON d1_data.code = m.code AND d1_data.date = ?
                    JOIN kline_data d2_data ON d2_data.code = m.code AND d2_data.date = ?
                    JOIN kline_data d3_data ON d3_data.code = m.code AND d3_data.date = ?
                    JOIN kline_data d4_data ON d4_data.code = m.code AND d4_data.date = ?
                    JOIN kline_data d5_data ON d5_data.code = m.code AND d5_data.date = ?
                    LEFT JOIN kline_data d6_data ON d6_data.code = m.code AND d6_data.date = ?
                    LEFT JOIN kline_data d7_data ON d7_data.code = m.code AND d7_data.date = ?
                    WHERE m.price > 0
                      ''' + exclude_risk_sql('m.name') + '''
                      ''' + fresh_clause + '''
                )
                SELECT code, name, price, change_pct, pe, pb, market_cap, volume,
                       total_gain, consecutive_days
                FROM base
                WHERE y1 = 1 AND y2 = 1 AND y3 = 1 AND y4 = 1 AND y5 = 1
                  AND total_gain > 0 AND total_gain <= 20
                ORDER BY total_gain DESC
                LIMIT ?
            '''
            params = [d1, d2, d3, d4, d5, d6, d7, limit]
            cur.execute(query, params)
            rows = cur.fetchall()

        columns = ['code', 'name', 'price', 'change_pct', 'pe', 'pb',
                    'market_cap', 'volume', 'total_gain', 'days']
        results = []
        for row in rows:
            r = dict(zip(columns, row))
            for col in ['price', 'change_pct', 'pe', 'pb', 'market_cap', 'volume']:
                v = r.get(col)
                if v is not None:
                    r[col] = round(float(v), 2)
            tg = r.get('total_gain')
            if tg is not None:
                r['total_gain'] = round(float(tg), 2)
            results.append(r)

        return jsonify({
            'success': True, 'data': results,
            'total': len(results), 'type': 'consecutive_up',
        })
    except Exception as e:
        logger.exception("Failed to screen consecutive-up stocks")
        return jsonify({'success': False, 'error': '服务器内部错误，请稍后重试'}), 500


@screen_bp.route('/api/screen/limit_up_pullback')
def screen_limit_up_pullback():
    """Limit-up followed by 2-4 day pullback of 6-7%."""
    try:
        from common.config import DB_PATH
        from common.screening import is_limit_up

        limit = _int_arg('limit', 50, 1, 1000)
        pullback_min = _float_arg('pullback_min', 6.0, 0.0, 100.0)
        pullback_max = _float_arg('pullback_max', 7.0, 0.0, 100.0)

        with closing(sqlite3.connect(DB_PATH, timeout=30.0)) as conn:
            cur = conn.cursor()

            cur.execute('SELECT MAX(date) FROM kline_data')
            latest_date = cur.fetchone()[0]
            if not latest_date:
                return jsonify({'success': False, 'error': 'No data'})

            fresh_codes = _fresh_listing_codes(cur)

            def _match(code, rows):
                """对单只股票最近 6 根 K 线应用涨停回落逻辑，命中返回结果 dict。"""
                if len(rows) < 3:
                    return None

                limit_up_idx = None
                for i, row in enumerate(rows):
                    pct_chg = row[5]
                    if pct_chg is not None and is_limit_up(code, pct_chg):
                        limit_up_idx = i
                        break

                if limit_up_idx is None:
                    return None

                limit_up_close = rows[limit_up_idx][2]
                limit_up_date = rows[limit_up_idx][0]
                days_after = limit_up_idx

                if days_after < 2 or days_after > 4:
                    return None

                min_price = limit_up_close
                pullback_days = 0

                for i in range(limit_up_idx):
                    row = rows[i]
                    low_p = row[4]
                    if low_p is not None and low_p < min_price:
                        min_price = low_p
                        pullback_days = i + 1

                if limit_up_close <= 0:
                    return None

                pullback_pct = (limit_up_close - min_price) / limit_up_close * 100

                if pullback_min <= pullback_pct <= pullback_max:
                    return {
                        'code': code,
                        'pullback_pct': round(pullback_pct, 2),
                        'pullback_days': pullback_days,
                        'limit_up_price': round(limit_up_close, 2),
                        'current_price': round(rows[0][2], 2) if rows[0][2] else None,
                        'limit_up_date': limit_up_date,
                        'days_after_limit_up': days_after,
                        'latest_pct': round(rows[0][5], 2) if rows[0][5] else None,
                    }
                return None

            results = []
            # 批量扫描：只取"全局第 6 新交易日"之后的行（每股最多 6 根），
            # 一次索引范围扫描取代旧的"全市场代码 + 每股一条点查"循环。
            # 附带语义改进：长期停牌的股票（最近 K 线早于 cutoff）自然被排除。
            # ROW_NUMBER/子查询不可用（SQLite 过旧）时回退到逐股循环。
            try:
                cutoff_row = cur.execute('''
                    SELECT DISTINCT date FROM kline_data ORDER BY date DESC LIMIT 1 OFFSET 5
                ''').fetchone()
                grouped = {}
                if cutoff_row and cutoff_row[0]:
                    cur.execute('''
                        SELECT code, date, open, close, high, low, pct_change
                        FROM kline_data
                        WHERE date >= ?
                        ORDER BY code, date DESC
                    ''', (cutoff_row[0],))
                    for row in cur.fetchall():
                        grouped.setdefault(row[0], []).append(row[1:])

                for code, rows in grouped.items():
                    if code in fresh_codes:
                        continue
                    try:
                        hit = _match(code, rows)
                        if hit:
                            results.append(hit)
                    except Exception:
                        continue
            except sqlite3.OperationalError:
                grouped = None

            if not grouped:
                # 回退：逐股扫描（每股一条 SQL）
                cur.execute('SELECT DISTINCT code FROM kline_data')
                all_codes = [row[0] for row in cur.fetchall()]

                for code in all_codes:
                    if code in fresh_codes:
                        continue
                    try:
                        cur.execute('''
                            SELECT date, open, close, high, low, pct_change
                            FROM kline_data
                            WHERE code = ?
                            ORDER BY date DESC
                            LIMIT 6
                        ''', (code,))
                        rows = cur.fetchall()
                        hit = _match(code, rows)
                        if hit:
                            results.append(hit)
                    except Exception:
                        continue


        # Enrich with stock metadata
        with closing(sqlite3.connect(DB_PATH, timeout=30.0)) as conn:
            cur = conn.cursor()

            meta = _meta_map(cur, [r['code'] for r in results])
            final_results = []
            for r in results:
                row = meta.get(r['code'])
                if row and is_risk_name(row[1]):
                    continue
                if row:
                    _attach_meta(r, row)
                else:
                    r['name'] = ''
                final_results.append(r)


        final_results.sort(key=lambda x: x['pullback_pct'], reverse=True)

        return jsonify({
            'success': True,
            'data': final_results[:limit],
            'total': len(final_results),
            'type': 'limit_up_pullback',
            'params': {'pullback_min': pullback_min, 'pullback_max': pullback_max},
        })
    except Exception as e:
        logger.exception("Failed to screen limit-up pullback stocks")
        return jsonify({'success': False, 'error': '服务器内部错误，请稍后重试'}), 500


@screen_bp.route('/api/screen/big_up_days')
def screen_big_up_days():
    """Count of days with pct_change >= threshold in the last N trading days."""
    try:
        from common.config import DB_PATH

        limit = _int_arg('limit', 50, 1, 1000)
        days = _int_arg('days', 30, 1, 250)
        threshold = _float_arg('threshold', 3.5, -100.0, 100.0)
        min_count = _int_arg('min_count', 1, 0, 10000)
        max_count = _int_arg('max_count', days, 0, 10000)
        # 价格带过滤：剔除高价股（>40 元）与低价股（<6 元）
        min_price = _float_arg('min_price', 6.0, 0.0, 1e6)
        max_price = _float_arg('max_price', 40.0, 0.0, 1e6)
        # 区间涨幅上限：最近 N 个交易日区间涨幅超过该值的股票排除（防高位股）
        max_range_gain = _float_arg('max_range_gain', 50.0, 0.0, 1e6)
        # 价值收紧条件（默认关闭，保持旧行为兼容）：
        #   min_limit_up: 区间内至少出现几次涨停（按板块 9.8%/19.5% 判定）
        #   max_drop_pct: 最近 10 个交易日单日最大跌幅不得深于该值（负数，如 -3 表示无深跌）
        #   exclude_st:   排除 ST/*ST 股
        min_limit_up = _int_arg('min_limit_up', 0, 0, 10000)
        max_drop_pct = (None if request.args.get('max_drop_pct') is None
                        else _float_arg('max_drop_pct', -3.0, -100.0, 100.0))
        exclude_st = request.args.get('exclude_st', '1') in ('1', 'true', 'yes')

        with closing(sqlite3.connect(DB_PATH, timeout=30.0)) as conn:
            cur = conn.cursor()

            min_bars = max(10, days // 5)

            # 单条窗口函数 SQL 取代旧的"全市场代码列表 + 每股一条点查"循环
            # （约 5000 次点查 → 一次索引扫描，秒级 → 百毫秒级）。
            # ROW_NUMBER 需要 SQLite >= 3.25；过旧时回退到逐股循环。
            _drop_cond = ''
            if max_drop_pct is not None:
                _drop_cond = 'AND min_pct_10 >= ?'
            _st_cond = "AND name NOT LIKE '%ST%'" if exclude_st else ''
            _delist_cond = "AND name NOT LIKE '%退%'"
            _fresh_clause = _fresh_listing_clause(cur, 'code')
            _window_sql = '''
                WITH meta AS (
                    SELECT code FROM stock_metadata
                    WHERE price IS NOT NULL AND price >= ? AND price <= ?
                      ''' + _st_cond + '''
                      ''' + _delist_cond + '''
                      ''' + _fresh_clause + '''
                ),
                ranked AS (
                    SELECT code, pct_change, close,
                           ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn
                    FROM kline_data
                    WHERE pct_change IS NOT NULL
                      AND date >= ?
                      AND code IN (SELECT code FROM meta)
                ),
                agg AS (
                    SELECT code,
                           SUM(CASE WHEN pct_change >= ? THEN 1 ELSE 0 END) AS cnt,
                           SUM(CASE WHEN pct_change >= ''' + limit_up_sql_case('code') + '''
                                    THEN 1 ELSE 0 END) AS lu_cnt,
                           MIN(CASE WHEN rn <= 10 THEN pct_change END) AS min_pct_10,
                           COUNT(*) AS total_days,
                           MAX(CASE WHEN rn = 1 THEN close END) AS latest_close,
                           MAX(CASE WHEN rn = ? THEN close END) AS base_close
                    FROM ranked
                    WHERE rn <= ?
                    GROUP BY code
                )
                SELECT code, cnt, total_days, lu_cnt
                FROM agg
                WHERE total_days >= ? AND cnt >= ? AND cnt <= ?
                  AND lu_cnt >= ?
                  AND latest_close IS NOT NULL AND base_close IS NOT NULL
                  AND base_close > 0
                  AND (latest_close - base_close) / base_close * 100.0 <= ?
                  ''' + _drop_cond + '''
            '''
            results = []
            _window_floor = _window_start(cur, days * 3)
            _params = [min_price, max_price, _window_floor,
                       threshold, days, days,
                       min_bars, min_count, max_count,
                       min_limit_up, max_range_gain]
            if max_drop_pct is not None:
                _params.append(max_drop_pct)
            try:
                cur.execute(_window_sql, _params)
                for code, cnt, total_days, lu_cnt in cur.fetchall():
                    results.append({
                        'code': code,
                        'big_up_days': cnt,
                        'limit_up_days': lu_cnt,
                        'total_days': total_days,
                        'big_up_ratio': round(cnt / total_days * 100, 2) if total_days else 0,
                    })
            except sqlite3.OperationalError:
                # SQLite 过旧（无窗口函数）：回退到逐股循环
                cur.execute('''
                    SELECT code, COUNT(*) as cnt
                    FROM kline_data
                    WHERE pct_change IS NOT NULL
                    GROUP BY code
                    HAVING cnt >= ?
                ''', (min_bars,))
                all_codes = [row[0] for row in cur.fetchall()]
                # 价格带过滤（与窗口 SQL 路径保持一致）
                _meta_q = '''
                    SELECT code FROM stock_metadata
                    WHERE price IS NOT NULL AND price >= ? AND price <= ?
                ''' + _st_cond + _delist_cond + _fresh_clause
                cur.execute(_meta_q, (min_price, max_price))
                valid_codes = {row[0] for row in cur.fetchall()}

                from common.screening import is_limit_up

                for code in all_codes:
                    if code not in valid_codes:
                        continue
                    try:
                        cur.execute('''
                            SELECT pct_change, close
                            FROM kline_data
                            WHERE code = ? AND pct_change IS NOT NULL
                            ORDER BY date DESC
                            LIMIT ?
                        ''', (code, days))
                        rows = cur.fetchall()

                        if len(rows) < min_bars:
                            continue

                        # 区间涨幅过滤（与窗口 SQL 路径保持一致）：
                        # rows 按日期降序，rows[0] 为最新收盘、rows[-1] 为 N 日前收盘
                        latest_close = rows[0][1]
                        base_close = rows[-1][1]
                        if (latest_close is None or base_close is None
                                or base_close <= 0):
                            continue
                        range_gain = (latest_close - base_close) / base_close * 100.0
                        if range_gain > max_range_gain:
                            continue

                        count = sum(
                            1 for row in rows
                            if row[0] is not None and row[0] >= threshold
                        )
                        lu_count = sum(
                            1 for row in rows
                            if row[0] is not None and is_limit_up(code, row[0])
                        )
                        recent10 = [row[0] for row in rows[:10] if row[0] is not None]
                        min_pct_10 = min(recent10) if recent10 else None
                        if lu_count < min_limit_up:
                            continue
                        if max_drop_pct is not None and (min_pct_10 is None or min_pct_10 < max_drop_pct):
                            continue

                        if min_count <= count <= max_count:
                            results.append({
                                'code': code,
                                'big_up_days': count,
                                'limit_up_days': lu_count,
                                'total_days': len(rows),
                                'big_up_ratio': round(count / len(rows) * 100, 2) if rows else 0,
                            })
                    except Exception:
                        continue


        # Enrich with stock metadata
        with closing(sqlite3.connect(DB_PATH, timeout=30.0)) as conn:
            cur = conn.cursor()

            meta = _meta_map(cur, [r['code'] for r in results])
            final_results = []
            for r in results:
                row = meta.get(r['code'])
                if row:
                    _attach_meta(r, row)
                else:
                    r['name'] = ''
                final_results.append(r)


        final_results.sort(key=lambda x: x['big_up_days'], reverse=True)

        return jsonify({
            'success': True,
            'data': final_results[:limit],
            'total': len(final_results),
            'type': 'big_up_days',
            'params': {
                'days': days, 'threshold': threshold,
                'min_count': min_count, 'max_count': max_count,
                'min_price': min_price, 'max_price': max_price,
                'max_range_gain': max_range_gain,
                'min_limit_up': min_limit_up, 'max_drop_pct': max_drop_pct,
                'exclude_st': exclude_st,
            },
        })
    except Exception as e:
        logger.exception("Failed to screen big-up-days stocks")
        return jsonify({'success': False, 'error': '服务器内部错误，请稍后重试'}), 500


@screen_bp.route('/api/screen/limit_up_days')
def screen_limit_up_days():
    """Rank stocks by number of limit-up days in the last N trading days.

    Board rules: 主板 9.8%，创业板/科创板（3xx / 688 / 689 / 302）19.5%。
    ST 股（含 '*ST'）整体排除。
    """
    try:
        from common.config import DB_PATH

        limit = _int_arg('limit', 100, 1, 1000)
        days = _int_arg('days', 20, 1, 250)
        min_count = _int_arg('min_count', 1, 0, 10000)
        # 可选收紧条件：同时要求至少 N 天涨幅>=threshold 的大涨日
        min_big_up = _int_arg('min_big_up', 0, 0, 10000)
        big_threshold = _float_arg('big_threshold', 3.5, -100.0, 100.0)

        with closing(sqlite3.connect(DB_PATH, timeout=30.0)) as conn:
            cur = conn.cursor()

            min_bars = max(10, days // 5)
            risk_clause = exclude_risk_sql('name')
            fresh_clause = _fresh_listing_clause(cur, 'code')
            _sql = '''
                WITH ranked AS (
                    SELECT code, pct_change,
                           ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn
                    FROM kline_data
                    WHERE pct_change IS NOT NULL
                      AND date >= ?
                      AND code IN (
                          SELECT code FROM stock_metadata
                          WHERE 1=1 ''' + risk_clause + ''' ''' + fresh_clause + '''
                      )
                )
                SELECT code,
                       SUM(CASE WHEN pct_change >= ''' + limit_up_sql_case('code') + '''
                                THEN 1 ELSE 0 END) AS lu_days,
                       SUM(CASE WHEN pct_change >= ? THEN 1 ELSE 0 END) AS big_days,
                       COUNT(*) AS total_days
                FROM ranked
                WHERE rn <= ?
                GROUP BY code
                HAVING total_days >= ? AND lu_days >= ? AND big_days >= ?
                ORDER BY lu_days DESC, big_days DESC
                LIMIT ?
            '''
            cur.execute(_sql, (_window_start(cur, days * 3), big_threshold, days, min_bars,
                               min_count, min_big_up, limit))
            columns = ['code', 'limit_up_days', 'big_up_days', 'total_days']
            rows = cur.fetchall()

            results = [dict(zip(columns, row)) for row in rows]
            meta = _meta_map(cur, [r['code'] for r in results])
            final_results = []
            for r in results:
                row = meta.get(r['code'])
                if row:
                    _attach_meta(r, row)
                else:
                    r['name'] = ''
                final_results.append(r)


        return jsonify({
            'success': True,
            'data': final_results,
            'total': len(final_results),
            'type': 'limit_up_days',
            'params': {
                'days': days, 'min_count': min_count,
                'min_big_up': min_big_up, 'big_threshold': big_threshold,
            },
        })
    except Exception as e:
        logger.exception("Failed to screen limit-up-days stocks")
        return jsonify({'success': False, 'error': '服务器内部错误，请稍后重试'}), 500


@screen_bp.route('/api/screen/consecutive_limit_up')
def screen_consecutive_limit_up():
    """连板高度：截至最新交易日连续涨停 >= min_boards 的股票。

    与"涨停天数排行"的区别：那个数的是窗口内涨停的天数（可以不连续），
    这里数的是**当前还在延续**的连板高度，并按板块规则判定涨停。
    """
    try:
        from common.config import DB_PATH
        from common.screening import is_limit_up

        limit = _int_arg('limit', 100, 1, 1000)
        min_boards = _int_arg('min_boards', 2, 1, 60)

        with closing(sqlite3.connect(DB_PATH, timeout=30.0)) as conn:
            cur = conn.cursor()
            latest_date = _latest_date(cur)
            if not latest_date:
                return jsonify({'success': False, 'error': 'No data'})

            # 连板最多看 60 个交易日（再长的连板极罕见）；历史不足时自动退化
            cutoff = _window_start(cur, 60)
            fresh_codes = _fresh_listing_codes(cur)
            # 列序固定为 date, close, open, high, low, pct_change：
            # 价格的任何下标计算都必须用这里的顺序（close 是 [1]，不是 [2]）。
            rows = cur.execute('''
                SELECT code, date, close, open, high, low, pct_change
                FROM kline_data
                WHERE date >= ? AND pct_change IS NOT NULL
                ORDER BY code, date DESC
            ''', (cutoff,)).fetchall()

            grouped = {}
            for row in rows:
                grouped.setdefault(row[0], []).append(row[1:])

            hits = []
            for code, bars in grouped.items():
                if code in fresh_codes:
                    continue        # 次新股：上市前 5 日无涨跌幅，会被误判成涨停
                if not bars or bars[0][0] != latest_date:
                    continue        # 停牌或数据没更新到最新交易日，不算"当前连板"
                streak = 0
                one_word_boards = 0
                for bar in bars:
                    if is_limit_up(code, bar[5]):
                        streak += 1
                        # 一字板：开=收=高=低，开盘即封死，盘中买不进
                        if bar[1] == bar[2] == bar[3] == bar[4]:
                            one_word_boards += 1
                    else:
                        break
                if streak < min_boards:
                    continue
                base_close = bars[streak][1] if len(bars) > streak else None
                latest_close = bars[0][1]
                total_gain = None
                if base_close and base_close > 0 and latest_close is not None:
                    total_gain = round((latest_close - base_close) / base_close * 100, 2)
                is_one_word = bars[0][1] == bars[0][2] == bars[0][3] == bars[0][4]
                hits.append({
                    'code': code,
                    'boards': streak,
                    'first_limit_date': bars[streak - 1][0],
                    'latest_pct': round(bars[0][5], 2),
                    'total_gain': total_gain,
                    'is_one_word': is_one_word,
                    'one_word_boards': one_word_boards,
                })

            meta = _meta_map(cur, [h['code'] for h in hits])

        results = []
        for hit in hits:
            meta_row = meta.get(hit['code'])
            if not meta_row or is_risk_name(meta_row[1]) or not meta_row[2] or meta_row[2] <= 0:
                continue
            results.append(_attach_meta(hit, meta_row))
        results.sort(key=lambda r: (r['boards'], r.get('change_pct') or 0), reverse=True)

        return jsonify({
            'success': True, 'data': results[:limit], 'total': len(results),
            'type': 'consecutive_limit_up',
            'params': {'min_boards': min_boards},
        })
    except Exception as e:
        logger.exception("Failed to screen consecutive limit-up stocks")
        return jsonify({'success': False, 'error': '服务器内部错误，请稍后重试'}), 500


@screen_bp.route('/api/screen/shrink_pullback')
def screen_shrink_pullback():
    """涨停后的缩量回调：回撤 5~15%，且最新量能萎缩到前 5 日均量的 vol_ratio 倍以下。

    与"涨停回调6-7%"的区别：那个只看价格与天数、窗口短；这里给回撤区间放宽，
    并且要求量能同步萎缩——放量下跌不算洗盘。
    """
    try:
        from common.config import DB_PATH
        from common.screening import is_limit_up

        limit = _int_arg('limit', 100, 1, 1000)
        days = _int_arg('days', 15, 5, 250)
        pullback_min = _float_arg('pullback_min', 5.0, 0.0, 100.0)
        pullback_max = _float_arg('pullback_max', 15.0, 0.0, 100.0)
        vol_ratio = _float_arg('vol_ratio', 0.6, 0.0, 100.0)
        min_days_after = _int_arg('min_days_after', 2, 1, 30)

        with closing(sqlite3.connect(DB_PATH, timeout=30.0)) as conn:
            cur = conn.cursor()
            latest_date = _latest_date(cur)
            if not latest_date:
                return jsonify({'success': False, 'error': 'No data'})

            cutoff = _window_start(cur, days)
            fresh_codes = _fresh_listing_codes(cur)
            # 列序：date[0], close[1], low[2], volume[3], pct_change[4]
            rows = cur.execute('''
                SELECT code, date, close, low, volume, pct_change
                FROM kline_data
                WHERE date >= ? AND pct_change IS NOT NULL
                ORDER BY code, date DESC
            ''', (cutoff,)).fetchall()

            grouped = {}
            for row in rows:
                grouped.setdefault(row[0], []).append(row[1:])

            hits = []
            for code, bars in grouped.items():
                if code in fresh_codes:
                    continue        # 次新股：上市前 5 日无涨跌幅，会被误判成涨停
                if not bars or bars[0][0] != latest_date or len(bars) < min_days_after + 2:
                    continue
                limit_idx = None
                for i in range(min_days_after, len(bars)):
                    if is_limit_up(code, bars[i][4]):
                        limit_idx = i
                        break
                if limit_idx is None:
                    continue
                # 回撤基准是涨停日的收盘价；[2] 是最低价，会把回撤系统性算小
                limit_close = bars[limit_idx][1]
                if not limit_close or limit_close <= 0:
                    continue
                lows = [b[2] for b in bars[:limit_idx] if b[2] is not None]
                if not lows:
                    continue
                min_low = min(lows)
                pullback = (limit_close - min_low) / limit_close * 100
                if not (pullback_min <= pullback <= pullback_max):
                    continue
                # 停牌日 volume=0 会拉低均量、放大缩量比，均量只取正成交量
                prev_vols = [b[3] for b in bars[1:6] if b[3] is not None and b[3] > 0]
                latest_vol = bars[0][3]
                if not prev_vols or latest_vol is None or latest_vol <= 0:
                    continue
                avg_prev = sum(prev_vols) / len(prev_vols)
                if avg_prev <= 0:
                    continue
                shrink = latest_vol / avg_prev
                if shrink > vol_ratio:
                    continue
                hits.append({
                    'code': code,
                    'pullback_pct': round(pullback, 2),
                    'days_after_limit_up': limit_idx,
                    'vol_shrink_ratio': round(shrink, 2),
                    'limit_up_date': bars[limit_idx][0],
                    'limit_up_price': round(limit_close, 2),
                    'latest_pct': round(bars[0][4], 2),
                })

            meta = _meta_map(cur, [h['code'] for h in hits])

        results = []
        for hit in hits:
            meta_row = meta.get(hit['code'])
            if not meta_row or is_risk_name(meta_row[1]) or not meta_row[2] or meta_row[2] <= 0:
                continue
            results.append(_attach_meta(hit, meta_row))
        results.sort(key=lambda r: r['pullback_pct'], reverse=True)

        return jsonify({
            'success': True, 'data': results[:limit], 'total': len(results),
            'type': 'shrink_pullback',
            'params': {'days': days, 'pullback_min': pullback_min,
                       'pullback_max': pullback_max, 'vol_ratio': vol_ratio},
        })
    except Exception as e:
        logger.exception("Failed to screen shrink-pullback stocks")
        return jsonify({'success': False, 'error': '服务器内部错误，请稍后重试'}), 500


@screen_bp.route('/api/screen/new_high_breakout')
def screen_new_high_breakout():
    """N 日新高突破：最新收盘高于前 N-1 根 K 线的最高收盘，且放量、当日有涨幅。

    "突破"必须带量才有意义，所以默认要求量能 >= 前 5 日均量的 1.5 倍；
    停牌股/数据没更新到最新交易日的股票不参与。
    """
    try:
        from common.config import DB_PATH

        limit = _int_arg('limit', 100, 1, 1000)
        days = _int_arg('days', 20, 5, 250)
        vol_ratio = _float_arg('vol_ratio', 1.5, 0.0, 100.0)
        min_gain = _float_arg('min_gain', 1.0, -100.0, 100.0)
        # "N 日新高"必须真的有 N 根 K 线：不足 N 根的新股/停牌股不参与，
        # 否则 base_close 为空、区间涨幅也失去意义。
        min_bars = days

        with closing(sqlite3.connect(DB_PATH, timeout=30.0)) as conn:
            cur = conn.cursor()
            latest_date = _latest_date(cur)
            if not latest_date:
                return jsonify({'success': False, 'error': 'No data'})
            cutoff = _window_start(cur, days)
            fresh_clause = _fresh_listing_clause(cur, 'm.code')

            rows = cur.execute('''
                WITH win AS (
                    SELECT code, date, close, volume, pct_change,
                           ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn
                    FROM kline_data
                    WHERE date >= ? AND pct_change IS NOT NULL
                ),
                agg AS (
                    SELECT code,
                           MAX(CASE WHEN rn = 1 THEN close END) AS latest_close,
                           MAX(CASE WHEN rn = 1 THEN volume END) AS latest_volume,
                           MAX(CASE WHEN rn = 1 THEN pct_change END) AS latest_pct,
                           MAX(CASE WHEN rn = 1 THEN date END) AS latest_date,
                           MAX(CASE WHEN rn BETWEEN 2 AND ? THEN close END) AS prev_high,
                           AVG(CASE WHEN rn BETWEEN 2 AND 6 AND volume > 0
                                    THEN volume END) AS prev5_volume,
                           MAX(CASE WHEN rn = ? THEN close END) AS base_close,
                           COUNT(*) AS bars
                    FROM win
                    WHERE rn <= ?
                    GROUP BY code
                ),
                scored AS (
                    SELECT code, latest_close, latest_pct, latest_date, prev_high, base_close,
                           CASE WHEN prev5_volume > 0
                                THEN latest_volume / prev5_volume END AS vol_ratio
                    FROM agg
                    WHERE bars >= ?
                )
                SELECT m.code, m.name, m.price, m.change_pct, m.pe, m.pb, m.market_cap,
                       m.volume, s.latest_pct, s.vol_ratio,
                       ROUND((s.latest_close - s.base_close) / s.base_close * 100, 2) AS range_gain
                FROM scored s
                JOIN stock_metadata m ON m.code = s.code
                WHERE s.latest_date = ?
                  AND s.prev_high IS NOT NULL
                  AND s.latest_close > s.prev_high
                  AND s.base_close > 0
                  AND s.vol_ratio >= ?
                  AND s.latest_pct >= ?
                  AND m.price > 0
                  ''' + exclude_risk_sql('m.name') + '''
                  ''' + fresh_clause + '''
                ORDER BY s.vol_ratio DESC
                LIMIT ?
            ''', (cutoff, days, days, days, min_bars, latest_date,
                  vol_ratio, min_gain, limit)).fetchall()

        results = []
        for row in rows:
            results.append({
                'code': row[0], 'name': row[1] or '',
                'price': round(float(row[2]), 2) if row[2] is not None else None,
                'change_pct': round(float(row[3]), 2) if row[3] is not None else None,
                'pe': round(float(row[4]), 2) if row[4] is not None else None,
                'pb': round(float(row[5]), 2) if row[5] is not None else None,
                'market_cap': round(float(row[6]), 2) if row[6] is not None else None,
                'volume': round(float(row[7]), 2) if row[7] is not None else None,
                'latest_pct': round(float(row[8]), 2) if row[8] is not None else None,
                'vol_ratio': round(float(row[9]), 2) if row[9] is not None else None,
                'range_gain': round(float(row[10]), 2) if row[10] is not None else None,
            })

        return jsonify({
            'success': True, 'data': results, 'total': len(results),
            'type': 'new_high_breakout',
            'params': {'days': days, 'vol_ratio': vol_ratio, 'min_gain': min_gain},
        })
    except Exception as e:
        logger.exception("Failed to screen new-high breakout stocks")
        return jsonify({'success': False, 'error': '服务器内部错误，请稍后重试'}), 500

