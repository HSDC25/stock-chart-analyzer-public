"""baostock 兜底抓取回归测试（M4）。

_get_kline_baostock_standalone 之前写死 15 天窗口 + tail(5)，调用方传 days=30
也只能拿回 5 根；修复后窗口与截断都必须跟随 days（与 get_kline_baostock 一致）。
用假 baostock 注入，不触网。
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

pd = pytest.importorskip("pandas")

import common.fetcher as fetcher


class _FakeRS:
    def __init__(self, rows):
        self._rows = rows
        self._i = 0
        self.error_code = "0"
        self.error_msg = ""

    def next(self):
        return self._i < len(self._rows)

    def get_row_data(self):
        row = self._rows[self._i]
        self._i += 1
        return row


class _FakeBS:
    def __init__(self, rows):
        self.rows = rows
        self.query_kwargs = None

    def login(self):
        return SimpleNamespace(error_code="0", error_msg="")

    def logout(self):
        pass

    def query_history_k_data_plus(self, bs_code, fields, **kwargs):
        self.query_kwargs = kwargs
        return _FakeRS(self.rows)


def _rows(n):
    base = datetime.now() - timedelta(days=n)
    out = []
    for i in range(n):
        d = (base + timedelta(days=i)).strftime("%Y-%m-%d")
        out.append([d, "10", "11", "9", "10.5", "100", "1000", "1.0", "0.5"])
    return out


def test_standalone_baostock_honors_days(monkeypatch):
    fake = _FakeBS(_rows(40))
    monkeypatch.setattr(fetcher, "bs", fake)
    df = fetcher._get_kline_baostock_standalone("600000", days=30)
    assert len(df) == 30                       # 旧实现写死 tail(5) 会失败
    start = datetime.strptime(fake.query_kwargs["start_date"], "%Y-%m-%d")
    end = datetime.strptime(fake.query_kwargs["end_date"], "%Y-%m-%d")
    assert (end - start).days == 30 * 2 + 5    # 窗口跟随 days，而非写死 15


def test_standalone_baostock_small_days(monkeypatch):
    fake = _FakeBS(_rows(40))
    monkeypatch.setattr(fetcher, "bs", fake)
    df = fetcher._get_kline_baostock_standalone("600000", days=5)
    assert len(df) == 5


def test_standalone_baostock_empty_returns_empty_frame(monkeypatch):
    fake = _FakeBS([])
    monkeypatch.setattr(fetcher, "bs", fake)
    df = fetcher._get_kline_baostock_standalone("600000", days=10)
    assert df.empty