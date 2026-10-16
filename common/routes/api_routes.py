"""Core API routes Blueprint.

Extracted from win11/app.py.  Business logic is kept inline for now;
it will be refactored into service modules in Phase 3.

Compatible with Python 3.8+.

简介：核心 API 蓝图（api_bp）——首页、预加载启停/状态、本地数据状态、
K 线图表数据、市场开闭市、以及月度/3日/5日涨幅等榜单接口，前端页面
直接调用。业务逻辑暂内联在路由函数中，服务层拆分留给 Phase 3。
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd
from flask import Blueprint, jsonify, request, render_template, session, make_response

logger = logging.getLogger("stockchart")

api_bp = Blueprint('api', __name__)


# ---------------------------------------------------------------------------
# Helpers (will move to service layer in Phase 3)
# ---------------------------------------------------------------------------

def _get_fetcher():
    """Lazy-import the global StockDataFetcher instance."""
    from app import fetcher  # noqa: deferred to avoid circular imports at module level
    return fetcher


def _safe_float(val):
    """把任意值安全转成 float；None/非数值/NaN 一律返回 None（便于 JSON 输出）。"""
    if val is None:
        return None
    try:
        f = float(val)
        return f if f == f else None  # NaN check
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@api_bp.route('/')
def index():
    """Homepage."""
    response = make_response(render_template('index.html'))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@api_bp.route('/api/preload', methods=['POST'])
def api_preload():
    """Start background preload thread (low priority, non-blocking)."""
    from app import preload_state, background_preload  # noqa: deferred

    # start() 内部持锁完成"已运行检查 + 置位 running + 建线程"，
    # 修复原先"锁内检查、锁外起线程"的竞态；且必须把 state 传给
    # background_preload —— 直接裸起线程会因 state=None 立即报错返回。
    try:
        preload_state.start(lambda: background_preload(preload_state))
    except RuntimeError:
        return jsonify({'success': False, 'error': '预加载已经在运行中'}), 409

    return jsonify({'success': True, 'message': '后台预加载已启动'})


@api_bp.route('/api/preload/stop', methods=['POST'])
def api_preload_stop():
    """Stop background preload."""
    from app import preload_state  # noqa: deferred
    preload_state.stop()
    return jsonify({'success': True, 'message': '已发送停止信号'})


@api_bp.route('/api/preload/status')
def api_preload_status():
    """Get preload progress."""
    from app import preload_state  # noqa: deferred
    return jsonify({'success': True, **preload_state.get_status()})


@api_bp.route('/api/data-status')
def api_data_status():
    """Check local data freshness (used at startup to decide whether to preload)."""
    try:
        from common.db import check_local_data_status
        status = check_local_data_status()
        return jsonify({'success': True, **status})
    except Exception as e:
        logger.exception("Failed to check local data status")
        return jsonify({'success': False, 'error': '服务器内部错误，请稍后重试'}), 500


@api_bp.route('/api/rank/monthly_top', methods=['POST'])
def api_rank_monthly_top():
    """Top-50 stocks by monthly price change (computed from K-line data)."""
    try:
        from common.config import DB_PATH

        conn = sqlite3.connect(DB_PATH, timeout=30.0)

        # 月度涨幅 ≈ 最近 20 个交易日：OFFSET 19 取到最新日之前第 20 个交易日的
        # 收盘价作为基期，与最新收盘价相除得涨幅
        query = '''
            SELECT a.code, a.name, a.price, a.change_pct, a.pe, a.pb, a.market_cap,
                   ROUND((latest.close - ago.close) / ago.close * 100, 2) as monthly_change
            FROM stock_metadata a
            JOIN kline_data latest ON latest.code = a.code
              AND latest.date = (SELECT MAX(date) FROM kline_data)
            JOIN kline_data ago ON ago.code = a.code
              AND ago.date = (SELECT date FROM kline_data
                              WHERE date < (SELECT MAX(date) FROM kline_data)
                              ORDER BY date DESC LIMIT 1 OFFSET 19)
            WHERE a.price > 0 AND latest.close > 0 AND ago.close > 0
            AND a.name NOT LIKE 'ST%' AND a.name NOT LIKE '*ST%'
            AND a.name NOT LIKE 'SST%' AND a.name NOT LIKE 'S*ST%'
            ORDER BY monthly_change DESC
            LIMIT 50
        '''
        df = pd.read_sql_query(query, conn)
        conn.close()

        records = df.to_dict('records')
        for r in records:
            for col in ['price', 'change_pct', 'pe', 'pb', 'market_cap', 'monthly_change']:
                v = r.get(col)
                if pd.notna(v):
                    r[col] = round(float(v), 2)
                else:
                    r[col] = None

        return jsonify({'success': True, 'data': records, 'total': len(records)})
    except Exception as e:
        logger.exception("Failed to query monthly top stocks")
        return jsonify({'success': False, 'error': '服务器内部错误，请稍后重试'}), 500


@api_bp.route('/api/stocks', methods=['POST'])
def api_stocks():
    """Stock screening API (two-phase: basic filter then on-demand metric computation)."""
    try:
        from common.config import DB_PATH
        from common.models import FilterCriteria
        from common.screening import (
            filter_stocks, batch_compute_metrics, compute_big_up_days_count,
        )

        data = request.get_json() or {}
        criteria = FilterCriteria(
            price_min=data.get('price_min'),
            price_max=data.get('price_max'),
            change_pct_min=data.get('change_pct_min'),
            change_pct_max=data.get('change_pct_max'),
            volume_min=data.get('volume_min'),
            pe_min=data.get('pe_min'),
            pe_max=data.get('pe_max'),
            pb_min=data.get('pb_min'),
            pb_max=data.get('pb_max'),
            market_cap_min=data.get('market_cap_min'),
            market_cap_max=data.get('market_cap_max'),
            three_day_change_min=data.get('three_day_change_min'),
            volume_change_pct_min=data.get('volume_change_pct_min'),
            codes=data.get('codes'),
            sort_by=data.get('sort_by'),
            sort_order=data.get('sort_order'),
        )

        fetcher = _get_fetcher()

        # Phase A: basic filter (pure DataFrame ops, millisecond-level)
        stock_list = fetcher.get_stock_list()
        filtered = filter_stocks(stock_list, criteria)

        needs_3day = criteria.three_day_change_min is not None
        needs_volchg = criteria.volume_change_pct_min is not None

        # Phase B: on-demand metric computation + filtering
        if needs_3day or needs_volchg:
            candidate_codes = filtered['code'].tolist()
            metrics = batch_compute_metrics(candidate_codes)

            t3_list, vc_list = [], []
            for code in filtered['code']:
                m = metrics.get(code, {})
                t3_list.append(m.get('three_day_change'))
                vc_list.append(m.get('volume_change_pct'))
            filtered = filtered.copy()
            filtered['three_day_change'] = t3_list
            filtered['volume_change_pct'] = vc_list

            if needs_3day:
                # fillna(-1e18)：算不出指标的股票视为远小于阈值（即不满足条件），
                # 避免 NaN 参与比较被误保留
                filtered = filtered[
                    pd.to_numeric(filtered['three_day_change'], errors='coerce').fillna(-1e18)
                    >= criteria.three_day_change_min
                ]
            if needs_volchg:
                filtered = filtered[
                    pd.to_numeric(filtered['volume_change_pct'], errors='coerce').fillna(-1e18)
                    >= criteria.volume_change_pct_min
                ]

        # Phase C: take top 100, add display metrics
        top100 = filtered.head(100).copy()
        if not needs_3day and not needs_volchg:
            display_codes = top100['code'].tolist()
            metrics = batch_compute_metrics(display_codes)
            t3_list, vc_list = [], []
            for code in top100['code']:
                m = metrics.get(code, {})
                t3_list.append(m.get('three_day_change'))
                vc_list.append(m.get('volume_change_pct'))
            top100['three_day_change'] = t3_list
            top100['volume_change_pct'] = vc_list

        # Add 30-day big-up-days metric (for display)
        if top100 is not None and len(top100) > 0:
            bu_list = []
            conn = sqlite3.connect(DB_PATH, timeout=30.0)
            for code in top100['code']:
                bu = compute_big_up_days_count(code, days=30, threshold=3.5, conn=conn)
                bu_list.append(bu)
            conn.close()
            top100['big_up_days_30'] = bu_list

        records = top100.to_dict('records')
        for r in records:
            for col in ['price', 'change_pct', 'pe', 'pb', 'market_cap',
                        'three_day_change', 'volume_change_pct', 'big_up_days_30']:
                v = r.get(col)
                if col == 'market_cap':
                    if pd.notna(v) and v > 1e6:
                        r[col] = round(v / 1e8, 2)
                    else:
                        r[col] = round(v, 2) if pd.notna(v) else None
                elif col in ('three_day_change', 'volume_change_pct', 'change_pct',
                             'pe', 'pb', 'price', 'big_up_days_30'):
                    r[col] = round(float(v), 2) if pd.notna(v) and v is not None else None

        return jsonify({'success': True, 'data': records, 'total': len(filtered)})
    except Exception as e:
        logger.exception("Failed to filter stocks")
        return jsonify({'success': False, 'error': '服务器内部错误，请稍后重试'}), 500


@api_bp.route('/api/top-change')
def api_top_change():
    """N-day cumulative change ranking API: compute market-wide N-day change, return top 50."""
    try:
        from common.models import is_allowed_stock_code
        from common.screening import compute_n_day_change
        from common.trading_calendar import trading_day_fetch_range

        n = request.args.get('days', 3, type=int)
        if n < 2 or n > 60:
            return jsonify({'success': False, 'error': 'days 参数需在 2~60 之间'}), 400

        _, end_str, _ = trading_day_fetch_range(n=n)

        fetcher = _get_fetcher()
        stock_list = fetcher.get_stock_list()
        all_codes = stock_list['code'].tolist()
        all_codes = [c for c in all_codes if is_allowed_stock_code(c)]

        results = {}

        def _compute_one(code):
            return (code, compute_n_day_change(code, n, end_str))

        with ThreadPoolExecutor(max_workers=20) as ex:
            futures = [ex.submit(_compute_one, c) for c in all_codes]
            for f in as_completed(futures):
                code, chg = f.result()
                if chg is not None:
                    results[code] = chg

        ranked = sorted(results.items(), key=lambda x: x[1], reverse=True)[:50]

        code_to_name = dict(zip(stock_list['code'], stock_list['name']))
        code_to_price = dict(zip(stock_list['code'], stock_list['price']))
        code_to_chg = dict(zip(stock_list['code'], stock_list.get('change_pct', [])))
        code_to_pe = dict(zip(stock_list['code'], stock_list.get('pe', [])))
        code_to_pb = dict(zip(stock_list['code'], stock_list.get('pb', [])))
        code_to_mcap = dict(zip(stock_list['code'], stock_list.get('market_cap', [])))

        records = []
        for rank, (code, chg) in enumerate(ranked, 1):
            records.append({
                'rank': rank,
                'code': code,
                'name': code_to_name.get(code, ''),
                'price': _safe_float(code_to_price.get(code)),
                'change_pct': _safe_float(code_to_chg.get(code)),
                'n_day_change': chg,
                'pe': _safe_float(code_to_pe.get(code)),
                'pb': _safe_float(code_to_pb.get(code)),
                'market_cap': _safe_float(code_to_mcap.get(code)),
                'days': n,
            })

        return jsonify({
            'success': True,
            'data': records,
            'days': n,
            'total': len(results),
        })
    except Exception as e:
        logger.exception("Failed to compute top change ranking")
        return jsonify({'success': False, 'error': '服务器内部错误，请稍后重试'}), 500


@api_bp.route('/api/stock/<code>/kline')
def api_kline(code):
    """Fetch daily K-line data for a single stock (last 30 trading days by default)."""
    # 严格校验 6 位代码格式，拒绝非法输入再进入后续查询
    if not re.match(r'^[036]\d{5}$', code):
        return jsonify({'error': 'Invalid stock code format'}), 400
    try:
        from app import prepare_chart_data  # noqa: deferred

        fetcher = _get_fetcher()
        days = request.args.get('days', 30, type=int)
        if days < 2 or days > 60:
            days = 30

        kline_df = fetcher.get_kline_data(code, days=days)
        if kline_df.empty:
            return jsonify({'success': False, 'error': '数据为空，请先预加载'}), 404

        chart_data = prepare_chart_data(kline_df, display_bars=days)

        stock_list = fetcher.get_stock_list()
        stock_name = stock_list[stock_list['code'] == code]['name'].values
        stock_name = stock_name[0] if len(stock_name) > 0 else code

        return jsonify({
            'success': True,
            'data': chart_data,
            'stock': {'code': code, 'name': stock_name},
            'freq': 'daily',
        })
    except Exception as e:
        logger.exception("Failed to fetch kline data for stock %s", code)
        return jsonify({'success': False, 'error': '服务器内部错误，请稍后重试'}), 500


@api_bp.route('/api/market_status')
def api_market_status():
    """A-share market open/closed status."""
    now = datetime.now()

    if now.weekday() >= 5:
        return jsonify({
            'success': True,
            'is_market_closed': True,
            'current_time': now.strftime('%Y-%m-%d %H:%M:%S'),
            'reason': '周末休市',
        })

    time_str = now.strftime('%H:%M')
    morning_start = '09:30'
    morning_end = '11:30'
    afternoon_start = '13:00'
    afternoon_end = '15:00'

    is_trading = (morning_start <= time_str <= morning_end) or \
                 (afternoon_start <= time_str <= afternoon_end)

    if is_trading:
        return jsonify({
            'success': True,
            'is_market_closed': False,
            'current_time': now.strftime('%Y-%m-%d %H:%M:%S'),
            'reason': '交易中',
        })
    else:
        return jsonify({
            'success': True,
            'is_market_closed': True,
            'current_time': now.strftime('%Y-%m-%d %H:%M:%S'),
            'reason': '非交易时段',
        })


@api_bp.route('/api/default-save-dir')
def api_default_save_dir():
    """Return the default export save directory path."""
    save_dir = str(Path.home() / 'Documents' / 'StockChart' / 'exports')
    Path(save_dir).mkdir(parents=True, exist_ok=True)
    return jsonify({'success': True, 'path': save_dir})


@api_bp.route('/api/rank/top3day')
def api_rank_top3day():
    """Top-50 stocks by 3-day cumulative change (from pre-computed field)."""
    try:
        from common.config import DB_PATH

        limit = int(request.args.get('limit', 50))

        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        query = '''
            SELECT code, name, price, change_pct,
                   COALESCE(three_day_change, 0) as three_day_change,
                   pe, pb, market_cap, volume
            FROM stock_metadata
            WHERE three_day_change IS NOT NULL AND price > 0
            AND name NOT LIKE 'ST%' AND name NOT LIKE '*ST%'
            AND name NOT LIKE 'SST%' AND name NOT LIKE 'S*ST%'
            ORDER BY three_day_change DESC
            LIMIT ?
        '''
        df = pd.read_sql_query(query, conn, params=(limit,))
        conn.close()

        records = df.to_dict('records')
        for r in records:
            for col in ['price', 'change_pct', 'pe', 'pb', 'market_cap', 'three_day_change']:
                v = r.get(col)
                if pd.notna(v):
                    r[col] = round(float(v), 2)
                else:
                    r[col] = None

        return jsonify({'success': True, 'data': records, 'total': len(records)})
    except Exception as e:
        logger.exception("Failed to query top 3-day change ranking")
        return jsonify({'success': False, 'error': '服务器内部错误，请稍后重试'}), 500


@api_bp.route('/api/rank/top5day')
def api_rank_top5day():
    """Top-50 stocks by 5-day cumulative change (computed from K-line data)."""
    try:
        from common.config import DB_PATH

        limit = int(request.args.get('limit', 50))

        conn = sqlite3.connect(DB_PATH, timeout=30.0)

        query = '''
            SELECT a.code, a.name, a.price, a.change_pct, a.pe, a.pb, a.market_cap,
                   ROUND((latest.close - ago.close) / ago.close * 100, 2) as five_day_change
            FROM stock_metadata a
            JOIN kline_data latest ON latest.code = a.code
              AND latest.date = (SELECT MAX(date) FROM kline_data)
            JOIN kline_data ago ON ago.code = a.code
              AND ago.date = (SELECT date FROM kline_data
                              WHERE date < (SELECT MAX(date) FROM kline_data)
                              ORDER BY date DESC LIMIT 1 OFFSET 4)
            WHERE a.price > 0 AND latest.close > 0 AND ago.close > 0
            AND a.name NOT LIKE 'ST%' AND a.name NOT LIKE '*ST%'
            AND a.name NOT LIKE 'SST%' AND a.name NOT LIKE 'S*ST%'
            ORDER BY five_day_change DESC
            LIMIT ?
        '''
        df = pd.read_sql_query(query, conn, params=(limit,))
        conn.close()

        records = df.to_dict('records')
        for r in records:
            for col in ['price', 'change_pct', 'pe', 'pb', 'market_cap', 'five_day_change']:
                v = r.get(col)
                if pd.notna(v):
                    r[col] = round(float(v), 2)
                else:
                    r[col] = None

        return jsonify({'success': True, 'data': records, 'total': len(records)})
    except Exception as e:
        logger.exception("Failed to query top 5-day change ranking")
        return jsonify({'success': False, 'error': '服务器内部错误，请稍后重试'}), 500
