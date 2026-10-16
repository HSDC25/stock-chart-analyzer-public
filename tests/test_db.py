"""Tests for common.db K-line round-trip.

Requires pandas; skipped automatically when pandas is not installed.
"""
import pytest

pd = pytest.importorskip("pandas")

from common.db import init_db, save_kline_to_db, get_kline_from_db


def _make_df():
    # 注意 save_kline_to_db 将 df 的 'change' 列映射到表字段 change_val
    return pd.DataFrame(
        {
            "date": ["2024-01-01", "2024-01-02", "2024-01-03"],
            "open": [1.0, 2.0, 3.0],
            "close": [1.1, 2.1, 3.1],
            "high": [1.2, 2.2, 3.2],
            "low": [0.9, 1.9, 2.9],
            "volume": [10.0, 20.0, 30.0],
            "amount": [100.0, 200.0, 300.0],
            "amplitude": [0.1, 0.1, 0.1],
            "pct_change": [0.01, 0.01, 0.01],
            "change": [0.1, 0.1, 0.1],
            "turnover": [0.01, 0.01, 0.01],
        }
    )


def test_kline_roundtrip(tmp_path, monkeypatch):
    # 同步模式：get_kline_from_db 返回全部数据，不受"距今 days*2"日期过滤影响
    monkeypatch.setenv("STOCKCHART_SYNC_MODE", "1")
    db = str(tmp_path / "t.db")
    init_db(db)
    save_kline_to_db("600000", _make_df(), db_path=db)

    out = get_kline_from_db("600000", days=120, db_path=db)
    assert len(out) == 3
    assert list(out["code"]) == ["600000"] * 3
    assert "date" in out.columns


def test_kline_empty(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    out = get_kline_from_db("NONE", days=120, db_path=db)
    assert out.empty
