"""Tests for common.indicators.

Requires pandas + numpy; skipped automatically when not installed.
"""
import pytest

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")

from common.indicators import (
    calculate_ma,
    calculate_macd,
    calculate_rsi,
    calculate_bollinger,
)


def _make_price_df(n=60):
    dates = pd.date_range("2024-01-01", periods=n, freq="D")
    # 简单锯齿波价格，便于指标计算
    close = np.concatenate([np.linspace(10, 20, n // 2), np.linspace(20, 10, n - n // 2)])
    return pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "close": close,
            "high": close + 1,
            "low": close - 1,
            "volume": np.ones(n) * 1000,
        }
    )


def test_calculate_ma_adds_columns():
    df = calculate_ma(_make_price_df(), periods=[5, 10])
    assert "MA5" in df.columns
    assert "MA10" in df.columns
    assert not df["MA5"].isna().all()


def test_calculate_macd():
    df = calculate_macd(_make_price_df())
    for col in ("MACD", "MACD_Signal", "MACD_Hist"):
        assert col in df.columns


def test_calculate_rsi_bounds():
    df = calculate_rsi(_make_price_df(), period=14)
    assert "RSI" in df.columns
    valid = df["RSI"].dropna()
    assert ((valid >= 0) & (valid <= 100)).all()


def test_calculate_bollinger():
    df = calculate_bollinger(_make_price_df(), period=20)
    for col in ("BOLL_MID", "BOLL_UPPER", "BOLL_LOWER"):
        assert col in df.columns
    # 上轨 >= 中轨 >= 下轨（在有效行）
    sub = df.dropna(subset=["BOLL_UPPER", "BOLL_MID", "BOLL_LOWER"])
    assert (sub["BOLL_UPPER"] >= sub["BOLL_MID"]).all()
    assert (sub["BOLL_MID"] >= sub["BOLL_LOWER"]).all()
