"""Tests for common.db batch reader (N+1 fix verification)."""
import pytest

pd = pytest.importorskip("pandas")

from common.db import init_db, save_kline_to_db, read_last_bars_from_db, read_last_bars_batch


def _seed_multi(tmp_path):
    """Create a temp DB with K-line data for 3 stocks."""
    db = str(tmp_path / "batch.db")
    init_db(db)
    for code in ["600000", "000001", "300750"]:
        dates = pd.date_range("2024-06-01", periods=10, freq="B")
        base = {"600000": 10, "000001": 20, "300750": 30}[code]
        df = pd.DataFrame({
            "date": dates.strftime("%Y-%m-%d"),
            "open": [base] * 10,
            "close": [base + i * 0.1 for i in range(10)],
            "high": [base + 1] * 10,
            "low": [base - 1] * 10,
            "volume": [1000 + i * 50 for i in range(10)],
            "amount": [10000.0] * 10,
        })
        save_kline_to_db(code, df, db_path=db)
    return db


def test_read_last_bars_single(tmp_path):
    db = _seed_multi(tmp_path)
    bars = read_last_bars_from_db("600000", 3, "2024-06-14", db_path=db)
    assert bars is not None
    assert len(bars) == 3


def test_read_last_bars_missing_code(tmp_path):
    db = _seed_multi(tmp_path)
    bars = read_last_bars_from_db("999999", 3, "2024-06-14", db_path=db)
    assert bars is None


def test_read_last_bars_batch_all_codes(tmp_path):
    db = _seed_multi(tmp_path)
    codes = ["600000", "000001", "300750"]
    result = read_last_bars_batch(codes, 3, "2024-06-14", db_path=db)
    assert set(result.keys()) == set(codes)
    for code in codes:
        assert result[code] is not None
        assert len(result[code]) == 3


def test_read_last_bars_batch_partial(tmp_path):
    db = _seed_multi(tmp_path)
    codes = ["600000", "999999"]
    result = read_last_bars_batch(codes, 3, "2024-06-14", db_path=db)
    assert result["600000"] is not None
    assert result["999999"] is None


def test_read_last_bars_batch_empty():
    result = read_last_bars_batch([], 3, "2024-06-14")
    assert result == {}


def test_batch_matches_single(tmp_path):
    """Batch results must match individual reads for the same code/date."""
    db = _seed_multi(tmp_path)
    code = "600000"
    single = read_last_bars_from_db(code, 4, "2024-06-14", db_path=db)
    batch = read_last_bars_batch([code], 4, "2024-06-14", db_path=db)
    assert single == batch[code]
