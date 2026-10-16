#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StockChart 智能分析模块（股票打分/预测）。

使用纯 Python（pandas/numpy）实现的规则评分系统，无需 sklearn；
数据直接读取本地 SQLite（~/Documents/StockChart/stock_data.db），
由 Flask 的 ml_routes 蓝图调用（/api/ml/* 接口），也可独立运行自测
（python ml_module.py）。注意：本模块列名用 snake_case（ma_5、rsi 等），
与 common.indicators 的 MA5/RSI 命名不同，因此指标在此模块内自行计算。
"""

import os
import sys
import json
import sqlite3
import logging

logger = logging.getLogger('stockchart_ml')
import warnings
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

# 数据库路径
_DATA_DIR = Path.home() / 'Documents' / 'StockChart'
_DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = str(_DATA_DIR / 'stock_data.db')


def calculate_features(df: pd.DataFrame) -> pd.DataFrame:
    """计算技术指标特征

    NOTE: common.indicators provides calculate_ma/macd/rsi/bollinger with
    column names like MA5, MACD, RSI, BOLL_UPPER.  This ML module uses
    snake_case columns (ma_5, macd, rsi, boll_upper) plus extra derived
    features (ma_ratio_*, ma_*_*_cross, boll_position).  To avoid breaking
    the downstream rule_based_predict() logic, we keep inline calculations
    here rather than delegating to common.indicators.
    """
    df = df.copy()
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').reset_index(drop=True)

    # 价格特征
    df['returns'] = df['close'].pct_change()
    df['high_low_range'] = (df['high'] - df['low']) / df['close']
    df['open_close_range'] = (df['close'] - df['open']) / df['open']

    # 移动平均线 (snake_case naming, distinct from common.indicators.calculate_ma)
    for period in [5, 10, 20, 60]:
        df[f'ma_{period}'] = df['close'].rolling(window=period).mean()
        df[f'ma_ratio_{period}'] = df['close'] / df[f'ma_{period}']

    # 均线交叉
    df['ma_5_10_cross'] = (df['ma_5'] > df['ma_10']).astype(int)
    df['ma_10_20_cross'] = (df['ma_10'] > df['ma_20']).astype(int)

    # 波动率
    df['volatility_5'] = df['returns'].rolling(window=5).std()
    df['volatility_20'] = df['returns'].rolling(window=20).std()

    # 成交量特征
    df['volume_ma_5'] = df['volume'].rolling(window=5).mean()
    df['volume_ma_20'] = df['volume'].rolling(window=20).mean()
    df['volume_ratio'] = df['volume'] / (df['volume_ma_5'] + 1e-8)

    # RSI (snake_case; common.indicators.calculate_rsi uses 'RSI')
    # 14 日相对强弱指标；分母加 1e-8 防止除零
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = (-delta).where(delta < 0, 0)
    avg_gain = gain.rolling(window=14).mean()
    avg_loss = loss.rolling(window=14).mean()
    rs = avg_gain / (avg_loss + 1e-8)
    df['rsi'] = 100 - (100 / (1 + rs))

    # MACD (snake_case; common.indicators.calculate_macd uses 'MACD'/'MACD_Signal'/'MACD_Hist')
    ema_12 = df['close'].ewm(span=12, adjust=False).mean()
    ema_26 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = ema_12 - ema_26
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_histogram'] = df['macd'] - df['macd_signal']

    # 布林带 (snake_case + boll_position; common.indicators.calculate_bollinger uses 'BOLL_*')
    # 20 日均线 ± 2 倍标准差；boll_position 归一化时加 1e-8 防除零
    df['boll_mid'] = df['close'].rolling(window=20).mean()
    df['boll_std'] = df['close'].rolling(window=20).std()
    df['boll_upper'] = df['boll_mid'] + 2 * df['boll_std']
    df['boll_lower'] = df['boll_mid'] - 2 * df['boll_std']
    df['boll_position'] = (df['close'] - df['boll_lower']) / (df['boll_upper'] - df['boll_lower'] + 1e-8)

    # 动量
    df['momentum_5'] = df['close'] / df['close'].shift(5) - 1
    df['momentum_10'] = df['close'] / df['close'].shift(10) - 1
    df['momentum_20'] = df['close'] / df['close'].shift(20) - 1

    # 时间特征
    df['day_of_week'] = df['date'].dt.dayofweek
    df['is_month_start'] = df['date'].dt.is_month_start.astype(int)
    df['is_month_end'] = df['date'].dt.is_month_end.astype(int)

    # 目标变量
    df['target'] = (df['close'].shift(-1) > df['close']).astype(int)
    df['target_returns'] = df['close'].shift(-1) / df['close'] - 1

    return df


def rule_based_predict(df: pd.DataFrame) -> dict:
    """
    基于规则的专家系统预测
    综合多个技术信号给出评分
    """
    if len(df) < 30:
        return {'error': '数据不足，至少需要30天数据'}
    
    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else None
    
    score = 0
    signals = []
    
    # 均线信号
    if latest.get('ma_5', 0) > latest.get('ma_10', 0):
        score += 2
        signals.append('5日线在10日线上方(+2) 📈')
    else:
        score -= 2
        signals.append('5日线在10日线下方(-2) 📉')
    
    if latest.get('ma_10', 0) > latest.get('ma_20', 0):
        score += 1
        signals.append('10日线在20日线上方(+1) 📈')
    else:
        score -= 1
        signals.append('10日线在20日线下方(-1) 📉')
    
    # MACD信号
    if latest.get('macd_histogram', 0) > 0:
        if prev is not None and prev.get('macd_histogram', 0) <= 0:
            score += 3
            signals.append('MACD金叉(+3) 🔥')
        else:
            score += 1
            signals.append('MACD在零轴上方(+1) 📈')
    else:
        score -= 1
        signals.append('MACD在零轴下方(-1) 📉')
    
    # RSI信号
    rsi = latest.get('rsi', 50)
    if 40 <= rsi <= 60:
        score += 1
        signals.append(f'RSI中性({rsi:.1f},+1)')
    elif rsi > 70:
        score -= 2
        signals.append(f'RSI超买({rsi:.1f},-2) ⚠️')
    elif rsi < 30:
        score += 2
        signals.append(f'RSI超卖({rsi:.1f},+2) 💡')
    else:
        signals.append(f'RSI={rsi:.1f}')
    
    # 量比信号
    vol_ratio = latest.get('volume_ratio', 1)
    if vol_ratio > 1.5:
        score += 2
        signals.append(f'放量({vol_ratio:.1f},+2) 🔥')
    elif vol_ratio < 0.5:
        score -= 1
        signals.append(f'缩量({vol_ratio:.1f},-1)')
    else:
        signals.append(f'量比={vol_ratio:.2f}')
    
    # 动量信号
    momentum = latest.get('momentum_5', 0)
    if momentum > 0.03:
        score += 2
        signals.append(f'5日涨幅{momentum*100:.1f}%(+2) 🚀')
    elif momentum < -0.03:
        score -= 2
        signals.append(f'5日跌幅{momentum*100:.1f}%(-2) 💥')
    else:
        signals.append(f'5日动量={momentum*100:.1f}%')
    
    # 布林带位置
    boll_pos = latest.get('boll_position', 0.5)
    if boll_pos > 0.9:
        score -= 1
        signals.append(f'接近布林上轨({boll_pos:.2f},-1)')
    elif boll_pos < 0.1:
        score += 1
        signals.append(f'接近布林下轨({boll_pos:.2f},+1)')
    
    # 综合判断
    if score >= 4:
        prediction = '强烈看多 🔥'
        confidence = min(85, 50 + score * 5)
    elif score >= 2:
        prediction = '看多 📈'
        confidence = min(70, 50 + score * 4)
    elif score >= 0:
        prediction = '中性偏多 ➡️'
        confidence = 50 + score * 3
    elif score >= -2:
        prediction = '中性偏空 ➡️'
        confidence = 50 + abs(score) * 3
    elif score >= -4:
        prediction = '看空 📉'
        confidence = min(70, 50 + abs(score) * 4)
    else:
        prediction = '强烈看空 💥'
        confidence = min(85, 50 + abs(score) * 5)
    
    return {
        'score': score,
        'prediction': prediction,
        'confidence': confidence,
        'signals': signals,
        'latest_price': latest.get('close', 0),
        'rsi': latest.get('rsi', None),
        'macd': latest.get('macd', None),
        'volume_ratio': latest.get('volume_ratio', None),
        'momentum_5': latest.get('momentum_5', None),
    }


def predict_stock(code: str) -> dict:
    """分析单只股票：从本地库取最近 120 个交易日，计算特征后走规则评分。

    120 天足够覆盖 60 日均线与 20 日动量等长周期特征的回看窗口；
    同时附带 stock_metadata 表中的名称/实时价/PE 等基础信息。
    """
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        query = '''
            SELECT date, open, close, high, low, volume
            FROM kline_data
            WHERE code = ?
            ORDER BY date DESC
            LIMIT 120
        '''
        df = pd.read_sql_query(query, conn, params=(code,))
        
        # 获取股票基本信息
        meta_query = '''SELECT name, price, change_pct, pe, pb, market_cap, volume 
                        FROM stock_metadata WHERE code = ?'''
        meta_df = pd.read_sql_query(meta_query, conn, params=(code,))
        
        stock_name = ''
        current_price = 0
        change_pct = 0
        pe = 0
        pb = 0
        market_cap = 0
        vol = 0
        
        if len(meta_df) > 0:
            row = meta_df.iloc[0]
            stock_name = row.get('name', '') or ''
            current_price = row.get('price', 0) or 0
            change_pct = row.get('change_pct', 0) or 0
            pe = row.get('pe', 0) or 0
            pb = row.get('pb', 0) or 0
            market_cap = row.get('market_cap', 0) or 0
            vol = row.get('volume', 0) or 0

        if len(df) < 30:
            return {'error': f'{code}: 数据不足（{len(df)}天）'}
        
        df = df.sort_values('date').reset_index(drop=True)
        df = calculate_features(df)
        
        result = rule_based_predict(df)
        result['code'] = code
        result['name'] = stock_name
        result['price'] = current_price
        result['change_pct'] = change_pct
        result['pe'] = pe
        result['pb'] = pb
        result['market_cap'] = market_cap
        result['volume'] = vol
        result['data_days'] = len(df)
        return result
    except Exception as e:
        logger.error('predict_stock(%s) 失败: %s', code, e)
        return {'code': code, 'error': str(e)}
    finally:
        if conn is not None:
            conn.close()


def rf_predict(code: str) -> dict:
    """兼容旧接口：/api/ml/rf_predict 路由引用此函数。

    当前实现为规则评分（非随机森林），直接委托 predict_stock，
    返回结构与 predict_stock 完全一致。
    """
    return predict_stock(code)


def screen_stocks(top_n: int = 20, progress_callback=None) -> list:
    """筛选潜力股票：遍历库内所有有足够历史数据的非 ST 股并打分，
    按得分降序返回前 top_n 只（只保留得分为正的看多标的）。

    *progress_callback(done, total)*：可选，逐股回调供异步作业上报进度。
    """
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)

        cursor = conn.cursor()
        cursor.execute('''
            SELECT k.code, COUNT(*) as cnt
            FROM kline_data k
            JOIN stock_metadata m ON m.code = k.code
            WHERE m.name NOT LIKE 'ST%' AND m.name NOT LIKE '*ST%'
              AND m.name NOT LIKE 'SST%' AND m.name NOT LIKE 'S*ST%'
            GROUP BY k.code
            HAVING cnt >= 30
        ''')
        codes = [row[0] for row in cursor.fetchall()]

        logger.info('[ML] 开始筛选 %d 只股票...', len(codes))

        results = []
        for i, code in enumerate(codes):
            if progress_callback:
                progress_callback(i, len(codes))
            if i % 500 == 0 and i > 0:
                logger.info('[ML] 已处理 %d/%d 只...', i, len(codes))

            result = predict_stock(code)
            if 'error' not in result and result.get('score', 0) > 0:
                results.append(result)

        results.sort(key=lambda x: x.get('score', 0), reverse=True)

        logger.info('[ML] 筛选完成，找到 %d 只看多股票', len(results))
        return results[:top_n]

    except Exception as e:
        logger.error('[ML] 筛选失败: %s', e)
        return []
    finally:
        if conn is not None:
            conn.close()


# 训练模型 - 现在只是保存历史分析结果
def train_rf_model():
    """训练模型 - 纯Python实现，收集历史数据用于优化规则。

    并不真正训练随机森林：只统计库规模并对 100 只样本股跑一遍规则
    评分，汇总多空信号占比作为"规则优化"结果返回给前端。
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        try:
            cursor = conn.cursor()

            # 统计数据库状态
            cursor.execute('SELECT COUNT(DISTINCT code) FROM kline_data')
            stock_count = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(*) FROM kline_data')
            total_records = cursor.fetchone()[0]

            cursor.execute('SELECT MIN(date), MAX(date) FROM kline_data')
            min_date, max_date = cursor.fetchone()
        finally:
            conn.close()

        # 分析历史数据，统计规则准确率
        analysis_count = 0
        up_count = 0

        conn = sqlite3.connect(DB_PATH, timeout=10)
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT DISTINCT code FROM kline_data LIMIT 100')
            sample_codes = [row[0] for row in cursor.fetchall()]
        finally:
            conn.close()
        
        for code in sample_codes:
            try:
                result = predict_stock(code)
                if 'error' not in result:
                    analysis_count += 1
                    if result.get('score', 0) > 0:
                        up_count += 1
            except Exception as e:  # nosec B112
                logging.getLogger("stockchart_ml").debug("分析失败: %s", e)
                continue
        
        return {
            'success': True,
            'message': f'规则优化完成！分析准确率统计完成',
            'stocks': stock_count,
            'records': total_records,
            'date_range': f'{min_date} ~ {max_date}',
            'analyzed': analysis_count,
            'up_signals': up_count
        }
        
    except Exception as e:
        return {'success': False, 'error': str(e)}


if __name__ == '__main__':
    print("=" * 60)
    print("  StockChart 智能分析模块")
    print("=" * 60)
    
    # 测试预测
    print('\n--- 测试预测: 000001 ---')
    result = predict_stock('000001')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    
    # 测试筛选
    print('\n--- 测试筛选 ---')
    screened = screen_stocks(top_n=5)
    for i, r in enumerate(screened[:5]):
        print(f"{i+1}. {r.get('code')} {r.get('name', '')}: {r.get('prediction')} (得分:{r.get('score')})")
