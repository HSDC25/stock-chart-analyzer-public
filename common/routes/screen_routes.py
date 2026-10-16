"""Screening API routes Blueprint.

Extracted from win11/app.py (screen_* endpoints).
Business logic is kept inline for now; will be refactored in Phase 3.

Compatible with Python 3.8+.

简介：选股策略蓝图（screen_bp）——首板涨停、涨停放量、放量、连阳、
涨停回落、大涨天数、涨停天数排行等策略筛选，全部直接查本地 SQLite
（kline_data + stock_metadata）。涨幅阈值按板块区分：主板 9.8%、
创业板/科创板 19.5%。
"""
from __future__ import annotations

import logging
import sqlite3

from flask import Blueprint, jsonify, request

logger = logging.getLogger("stockchart")

screen_bp = Blueprint('screen', __name__)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@screen_bp.route('/api/screen/first_limit_up')
def screen_first_limit_up():
    """First limit-up of the day (limit-up today, no limit-up in prior 14 days)."""
    try:
        from common.config import DB_PATH

        limit = int(request.args.get('limit', 100))
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        cur = conn.cursor()

        cur.execute('SELECT MAX(date) FROM kline_data')
        latest_date = cur.fetchone()[0]
        if not latest_date:
            return jsonify({'success': False, 'error': 'No data'})

        cur.execute('''
            WITH recent_limit_up AS (
                -- 最近 14 个交易日内出现过涨停的股票集合：
                -- "首板"要求此前两周没有涨停，故用 NOT IN 排除老涨停股
                SELECT DISTINCT code FROM kline_data
                WHERE date >= date(?, '-14 days')
                  AND date < ?
                  AND pct_change >= CASE
                      WHEN code LIKE '3%' OR code LIKE '688%' THEN 19.5
                      ELSE 9.8
                  END
            )
            SELECT m.code, m.name, m.price, m.change_pct, m.pe, m.pb,
                   m.market_cap, m.volume
            FROM stock_metadata m
            WHERE m.change_pct >= CASE
                      WHEN m.code LIKE '3%' OR m.code LIKE '688%' THEN 19.5
                      ELSE 9.8
                  END
              AND m.price > 0
              AND m.name NOT LIKE 'ST%' AND m.name NOT LIKE '*ST%'
              AND m.name NOT LIKE 'SST%' AND m.name NOT LIKE 'S*ST%'
              AND m.code NOT IN (SELECT code FROM recent_limit_up)
            ORDER BY m.change_pct DESC
            LIMIT ?
        ''', (latest_date, latest_date, limit))
        rows = cur.fetchall()
        conn.close()

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

        limit = int(request.args.get('limit', 50))
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        cur = conn.cursor()

        cur.execute('SELECT MAX(date) FROM kline_data')
        latest_date = cur.fetchone()[0]
        if not latest_date:
            return jsonify({'success': False, 'error': 'No data'})

        cur.execute('SELECT MAX(date) FROM kline_data WHERE date < ?', (latest_date,))
        prev_date = cur.fetchone()[0]
        if not prev_date:
            return jsonify({'success': False, 'error': 'No previous date'})

        cur.execute('''
            WITH recent_limit_up AS (
                SELECT DISTINCT code FROM kline_data
                WHERE date >= date(?, '-14 days')
                  AND date < ?
                  AND pct_change >= CASE
                      WHEN code LIKE '3%' OR code LIKE '688%' THEN 19.5
                      ELSE 9.8
                  END
            )
            SELECT m.code, m.name, m.price, m.change_pct, m.pe, m.pb,
                   m.market_cap, m.volume,
                   ROUND(today.turnover / yesterday.turnover, 2) as turnover_ratio
            FROM stock_metadata m
            JOIN kline_data today ON today.code = m.code AND today.date = ?
            JOIN kline_data yesterday ON yesterday.code = m.code AND yesterday.date = ?
            WHERE m.change_pct >= CASE
                      WHEN m.code LIKE '3%' OR m.code LIKE '688%' THEN 19.5
                      ELSE 9.8
                  END
              AND m.price > 0
              AND m.name NOT LIKE 'ST%' AND m.name NOT LIKE '*ST%'
              AND m.name NOT LIKE 'SST%' AND m.name NOT LIKE 'S*ST%'
              AND yesterday.turnover > 0
              AND today.turnover / yesterday.turnover >= 2.5
              AND m.code NOT IN (SELECT code FROM recent_limit_up)
            ORDER BY turnover_ratio DESC
            LIMIT ?
        ''', (latest_date, latest_date, latest_date, prev_date, limit))
        rows = cur.fetchall()
        conn.close()

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

        limit = int(request.args.get('limit', 50))
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        cur = conn.cursor()

        cur.execute('SELECT MAX(date) FROM kline_data')
        latest_date = cur.fetchone()[0]
        if not latest_date:
            return jsonify({'success': False, 'error': 'No data'})

        cur.execute('SELECT MAX(date) FROM kline_data WHERE date < ?', (latest_date,))
        prev_date = cur.fetchone()[0]
        if not prev_date:
            return jsonify({'success': False, 'error': 'No previous date'})

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
              AND m.name NOT LIKE 'ST%' AND m.name NOT LIKE '*ST%'
              AND m.name NOT LIKE 'SST%' AND m.name NOT LIKE 'S*ST%'
              AND yesterday.turnover > 0
              AND today.turnover / yesterday.turnover >= 3.0
            ORDER BY turnover_ratio DESC
            LIMIT ?
        ''', (latest_date, prev_date, limit))
        rows = cur.fetchall()
        conn.close()

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

        limit = int(request.args.get('limit', 100))
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        cur = conn.cursor()

        cur.execute('SELECT DISTINCT date FROM kline_data ORDER BY date DESC LIMIT 7')
        dates = [r[0] for r in cur.fetchall()]
        if len(dates) < 5:
            return jsonify({'success': False, 'error': 'Not enough data'})

        d1, d2, d3, d4, d5 = dates[0], dates[1], dates[2], dates[3], dates[4]
        d6 = dates[5] if len(dates) > 5 else None
        d7 = dates[6] if len(dates) > 6 else None

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
                  AND m.name NOT LIKE 'ST%' AND m.name NOT LIKE '*ST%'
                  AND m.name NOT LIKE 'SST%' AND m.name NOT LIKE 'S*ST%'
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
        conn.close()

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
        return jsonify({'success': False, 'error': '服务器内部错误: %s' % str(e)}), 500


@screen_bp.route('/api/screen/limit_up_pullback')
def screen_limit_up_pullback():
    """Limit-up followed by 2-4 day pullback of 6-7%."""
    try:
        from common.config import DB_PATH
        from common.screening import is_limit_up

        limit = int(request.args.get('limit', 50))
        pullback_min = float(request.args.get('pullback_min', 6.0))
        pullback_max = float(request.args.get('pullback_max', 7.0))

        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        cur = conn.cursor()

        cur.execute('SELECT MAX(date) FROM kline_data')
        latest_date = cur.fetchone()[0]
        if not latest_date:
            return jsonify({'success': False, 'error': 'No data'})

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

        conn.close()

        # Enrich with stock metadata
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        cur = conn.cursor()

        final_results = []
        for r in results:
            cur.execute('''
                SELECT name, price, change_pct, pe, pb, market_cap, volume
                FROM stock_metadata WHERE code = ?
            ''', (r['code'],))
            meta = cur.fetchone()
            if meta:
                r['name'] = meta[0] or ''
                r['price'] = round(float(meta[1]), 2) if meta[1] else None
                r['change_pct'] = round(float(meta[2]), 2) if meta[2] else None
                r['pe'] = round(float(meta[3]), 2) if meta[3] else None
                r['pb'] = round(float(meta[4]), 2) if meta[4] else None
                r['market_cap'] = round(float(meta[5]), 2) if meta[5] else None
                r['volume'] = round(float(meta[6]), 2) if meta[6] else None
            else:
                r['name'] = ''
            final_results.append(r)

        conn.close()

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
        return jsonify({'success': False, 'error': '服务器内部错误: %s' % str(e)}), 500


@screen_bp.route('/api/screen/big_up_days')
def screen_big_up_days():
    """Count of days with pct_change >= threshold in the last N trading days."""
    try:
        from common.config import DB_PATH

        limit = int(request.args.get('limit', 50))
        days = int(request.args.get('days', 30))
        threshold = float(request.args.get('threshold', 3.5))
        min_count = int(request.args.get('min_count', 1))
        max_count = int(request.args.get('max_count', days))
        # 价格带过滤：剔除高价股（>40 元）与低价股（<6 元）
        min_price = float(request.args.get('min_price', 6.0))
        max_price = float(request.args.get('max_price', 40.0))
        # 区间涨幅上限：最近 N 个交易日区间涨幅超过该值的股票排除（防高位股）
        max_range_gain = float(request.args.get('max_range_gain', 50.0))
        # 价值收紧条件（默认关闭，保持旧行为兼容）：
        #   min_limit_up: 区间内至少出现几次涨停（按板块 9.8%/19.5% 判定）
        #   max_drop_pct: 最近 10 个交易日单日最大跌幅不得深于该值（负数，如 -3 表示无深跌）
        #   exclude_st:   排除 ST/*ST 股
        min_limit_up = int(request.args.get('min_limit_up', 0))
        max_drop_pct = request.args.get('max_drop_pct')
        max_drop_pct = float(max_drop_pct) if max_drop_pct is not None else None
        exclude_st = request.args.get('exclude_st', '0') in ('1', 'true', 'yes')

        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        cur = conn.cursor()

        min_bars = max(10, days // 5)

        # 单条窗口函数 SQL 取代旧的"全市场代码列表 + 每股一条点查"循环
        # （约 5000 次点查 → 一次索引扫描，秒级 → 百毫秒级）。
        # ROW_NUMBER 需要 SQLite >= 3.25；过旧时回退到逐股循环。
        _drop_cond = ''
        if max_drop_pct is not None:
            _drop_cond = 'AND min_pct_10 >= ?'
        _st_cond = "AND name NOT LIKE '%ST%'" if exclude_st else ''
        _window_sql = '''
            WITH meta AS (
                SELECT code FROM stock_metadata
                WHERE price IS NOT NULL AND price >= ? AND price <= ?
                  ''' + _st_cond + '''
            ),
            ranked AS (
                SELECT code, pct_change, close,
                       ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn
                FROM kline_data
                WHERE pct_change IS NOT NULL
                  AND code IN (SELECT code FROM meta)
            ),
            agg AS (
                SELECT code,
                       SUM(CASE WHEN pct_change >= ? THEN 1 ELSE 0 END) AS cnt,
                       SUM(CASE WHEN (code LIKE '3%' OR code LIKE '688%' OR code LIKE '689%' OR code LIKE '302%')
                                     AND pct_change >= 19.5
                                THEN 1
                                WHEN code NOT LIKE '3%' AND code NOT LIKE '688%'
                                     AND code NOT LIKE '689%' AND code NOT LIKE '302%'
                                     AND pct_change >= 9.8
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
        _params = [min_price, max_price,
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
            ''' + _st_cond
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

        conn.close()

        # Enrich with stock metadata
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        cur = conn.cursor()

        final_results = []
        for r in results:
            cur.execute('''
                SELECT name, price, change_pct, pe, pb, market_cap, volume
                FROM stock_metadata WHERE code = ?
            ''', (r['code'],))
            meta = cur.fetchone()
            if meta:
                r['name'] = meta[0] or ''
                r['price'] = round(float(meta[1]), 2) if meta[1] else None
                r['change_pct'] = round(float(meta[2]), 2) if meta[2] else None
                r['pe'] = round(float(meta[3]), 2) if meta[3] else None
                r['pb'] = round(float(meta[4]), 2) if meta[4] else None
                r['market_cap'] = round(float(meta[5]), 2) if meta[5] else None
                r['volume'] = round(float(meta[6]), 2) if meta[6] else None
            else:
                r['name'] = ''
            final_results.append(r)

        conn.close()

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
        return jsonify({'success': False, 'error': '服务器内部错误: %s' % str(e)}), 500


@screen_bp.route('/api/screen/limit_up_days')
def screen_limit_up_days():
    """Rank stocks by number of limit-up days in the last N trading days.

    Board rules: 主板 9.8%，创业板/科创板（3xx / 688 / 689 / 302）19.5%。
    ST 股（含 '*ST'）整体排除。
    """
    try:
        from common.config import DB_PATH

        limit = int(request.args.get('limit', 100))
        days = int(request.args.get('days', 20))
        min_count = int(request.args.get('min_count', 1))
        # 可选收紧条件：同时要求至少 N 天涨幅>=threshold 的大涨日
        min_big_up = int(request.args.get('min_big_up', 0))
        big_threshold = float(request.args.get('big_threshold', 3.5))

        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        cur = conn.cursor()

        min_bars = max(10, days // 5)
        _sql = '''
            WITH ranked AS (
                SELECT code, pct_change,
                       ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn
                FROM kline_data
                WHERE pct_change IS NOT NULL
                  AND code IN (
                      SELECT code FROM stock_metadata
                      WHERE name NOT LIKE '%ST%'
                  )
            )
            SELECT code,
                   SUM(CASE WHEN (code LIKE '3%' OR code LIKE '688%' OR code LIKE '689%' OR code LIKE '302%')
                                 AND pct_change >= 19.5
                            THEN 1
                            WHEN code NOT LIKE '3%' AND code NOT LIKE '688%'
                                 AND code NOT LIKE '689%' AND code NOT LIKE '302%'
                                 AND pct_change >= 9.8
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
        cur.execute(_sql, (big_threshold, days, min_bars,
                           min_count, min_big_up, limit))
        columns = ['code', 'limit_up_days', 'big_up_days', 'total_days']
        rows = cur.fetchall()

        final_results = []
        for row in rows:
            r = dict(zip(columns, row))
            cur.execute('''
                SELECT name, price, change_pct, pe, pb, market_cap, volume
                FROM stock_metadata WHERE code = ?
            ''', (r['code'],))
            meta = cur.fetchone()
            if meta:
                r['name'] = meta[0] or ''
                for k, v in zip(['price', 'change_pct', 'pe', 'pb',
                                 'market_cap', 'volume'], meta[1:]):
                    r[k] = round(float(v), 2) if v is not None else None
            else:
                r['name'] = ''
            final_results.append(r)

        conn.close()

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
        return jsonify({'success': False, 'error': '服务器内部错误: %s' % str(e)}), 500
