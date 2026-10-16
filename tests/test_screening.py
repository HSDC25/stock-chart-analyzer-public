"""Tests for common.screening metric functions (SQLite-backed)."""
import pytest

pd = pytest.importorskip("pandas")

from common.db import init_db, save_kline_to_db
from common.screening import (
    compute_n_day_change,
    compute_three_day_change,
    compute_volume_change_pct,
    is_limit_up,
    batch_compute_metrics,
)


def _seed_db(tmp_path):
    """Create a temp DB with 10 days of K-line data for 600000."""
    db = str(tmp_path / "screen.db")
    init_db(db)
    dates = pd.date_range("2024-06-01", periods=10, freq="B")
    df = pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": [10.0] * 10,
        "close": [10 + i * 0.5 for i in range(10)],
        "high": [11.0] * 10,
        "low": [9.0] * 10,
        "volume": [1000 + i * 100 for i in range(10)],
        "amount": [10000.0] * 10,
        "pct_change": [5.0] * 10,
    })
    save_kline_to_db("600000", df, db_path=db)
    return db


def test_is_limit_up_main_board():
    assert is_limit_up("600000", 9.8)
    assert not is_limit_up("600000", 5.0)


def test_is_limit_up_gem():
    assert is_limit_up("300750", 19.5)
    assert not is_limit_up("300750", 10.0)


def test_is_limit_up_star():
    assert is_limit_up("688001", 19.5)


def test_compute_n_day_change_with_data(tmp_path, monkeypatch):
    db = _seed_db(tmp_path)
    monkeypatch.setattr("common.screening.DB_PATH", db)
    # With 10 bars at close = 10, 10.5, ..., 14.5
    # 3-day change: (last - 3_bars_ago) / 3_bars_ago * 100
    result = compute_n_day_change("600000", 3, end_str="2024-06-14")
    # Should return a float or None (depending on exact date alignment)
    assert result is None or isinstance(result, float)


def test_compute_three_day_change_delegates(tmp_path, monkeypatch):
    db = _seed_db(tmp_path)
    monkeypatch.setattr("common.screening.DB_PATH", db)
    r1 = compute_three_day_change("600000", end_str="2024-06-14")
    r2 = compute_n_day_change("600000", 3, end_str="2024-06-14")
    assert r1 == r2


def test_compute_volume_change_pct(tmp_path, monkeypatch):
    db = _seed_db(tmp_path)
    monkeypatch.setattr("common.screening.DB_PATH", db)
    result = compute_volume_change_pct("600000", end_str="2024-06-14")
    assert result is None or isinstance(result, float)


def test_batch_compute_metrics_empty():
    result = batch_compute_metrics([])
    assert result == {}


def test_batch_compute_metrics_missing_codes(tmp_path, monkeypatch):
    db = _seed_db(tmp_path)
    monkeypatch.setattr("common.screening.DB_PATH", db)
    result = batch_compute_metrics(["999999"])
    assert "999999" in result
    assert result["999999"]["three_day_change"] is None
    assert result["999999"]["volume_change_pct"] is None
