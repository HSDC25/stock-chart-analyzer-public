"""
Technical indicator calculations for stock chart analysis.

Pure functions that take a pandas DataFrame and return a modified DataFrame
(or dict for prepare_chart_data). Extracted from win11/app.py for reuse
across all platform frontends.

Python 3.8 compatible.
"""

import math
from typing import Dict, List, Optional

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Indicator calculations
# ---------------------------------------------------------------------------

def calculate_ma(df: pd.DataFrame, periods: List[int] = None) -> pd.DataFrame:
    """计算移动平均线"""
    if periods is None:
        periods = [5, 10, 20, 60]
    for p in periods:
        df[f'MA{p}'] = df['close'].rolling(window=p).mean().round(2)
    return df


def calculate_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """计算 MACD 指标"""
    ema_fast = df['close'].ewm(span=fast, adjust=False).mean()
    ema_slow = df['close'].ewm(span=slow, adjust=False).mean()
    df['MACD'] = (ema_fast - ema_slow).round(4)
    df['MACD_Signal'] = df['MACD'].ewm(span=signal, adjust=False).mean().round(4)
    df['MACD_Hist'] = (df['MACD'] - df['MACD_Signal']).round(4)
    return df


def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """计算 RSI 指标"""
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = (-delta).where(delta < 0, 0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    df['RSI'] = (100 - (100 / (1 + rs))).round(2)
    return df


def calculate_bollinger(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> pd.DataFrame:
    """计算布林带"""
    df['BOLL_MID'] = df['close'].rolling(window=period).mean().round(2)
    df['BOLL_STD'] = df['close'].rolling(window=period).std().round(2)
    df['BOLL_UPPER'] = (df['BOLL_MID'] + std_dev * df['BOLL_STD']).round(2)
    df['BOLL_LOWER'] = (df['BOLL_MID'] - std_dev * df['BOLL_STD']).round(2)
    return df


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clean_nan_values(obj):
    """递归清理数据中的 NaN 值。

    NaN/Inf 无法被 JSON 序列化（json.dumps 会报错或产生非法 JSON），
    统一转成 None，前端对 None 的位置显示为断点。
    """
    if isinstance(obj, dict):
        return {k: clean_nan_values(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [clean_nan_values(item) for item in obj]
    elif isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    return obj


# ---------------------------------------------------------------------------
# Chart data preparation
# ---------------------------------------------------------------------------

def prepare_chart_data(df: pd.DataFrame, display_bars: Optional[int] = None) -> Dict:
    """
    准备图表所需数据。

    先在完整历史窗口上计算技术指标（MA/MACD/RSI/BOLL 需要足够的历史才有值），
    再截取最后 display_bars 根用于展示。这样即使只展示"近 3 个交易日"，
    指标也能基于完整历史算出真实值，而不会因为窗口太短被整体清空导致 K 线画不出来。
    """
    df = df.copy()
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').reset_index(drop=True)

    # 计算技术指标（基于完整历史）
    df = calculate_ma(df)
    df = calculate_macd(df)
    df = calculate_rsi(df)
    df = calculate_bollinger(df)

    # 截取展示窗口（放在算完指标之后，保证尾部指标是真实值）
    if display_bars is not None and display_bars > 0 and len(df) > display_bars:
        df = df.tail(display_bars).reset_index(drop=True)

    # 注意：这里不再 dropna 整体清空——K 线（OHLC）永远有值，
    # 指标缺失的位置会被 clean_nan_values 转成 None，前端自然显示为断点。

    # 根据数据粒度决定日期格式（日 K vs 分钟线）
    sample = df['date'].iloc[0]
    if sample.hour != 0 or sample.minute != 0:
        date_fmt = '%m-%d %H:%M'
    else:
        date_fmt = '%Y-%m-%d'
    dates = df['date'].dt.strftime(date_fmt).tolist()

    data = {
        'dates': dates,
        'kline': df[['open', 'close', 'low', 'high']].values.tolist(),
        'volume': df['volume'].astype(int).tolist(),
        'ma5': df['MA5'].tolist(),
        'ma10': df['MA10'].tolist(),
        'ma20': df['MA20'].tolist(),
        'ma60': df['MA60'].tolist(),
        'macd': df['MACD'].tolist(),
        'macd_signal': df['MACD_Signal'].tolist(),
        'macd_hist': df['MACD_Hist'].tolist(),
        'rsi': df['RSI'].tolist(),
        'boll_upper': df['BOLL_UPPER'].tolist(),
        'boll_mid': df['BOLL_MID'].tolist(),
        'boll_lower': df['BOLL_LOWER'].tolist(),
    }

    return clean_nan_values(data)
