"""Tests for the stock screening endpoints (Flask + temp SQLite).

构造一组合成 K 线，只验证判定逻辑本身：连板高度、缩量回调、N 日新高突破，
以及 689（科创板 CDR）涨停阈值与"窗口天数大于库内历史"时的回退行为。
"""
import sqlite3

import pytest

pd = pytest.importorskip("pandas")

from common.db import init_db, save_kline_to_db
from common.screening import is_limit_up

DATES = pd.bdate_range("2024-06-03", periods=20)


def _frame(pcts, volumes, closes=None, lows=None):
    """按每日涨跌幅反推 OHLC；close 可显式给出，low 默认取收盘价下方 1%。"""
    if closes is None:
        closes, price = [], 10.0
        for pct in pcts:
            price = round(price * (1 + pct / 100.0), 2)
            closes.append(price)
    return pd.DataFrame({
        "date": DATES[:len(closes)],
        "open": closes,
        "close": closes,
        "high": [c * 1.01 for c in closes],
        "low": lows if lows is not None else [c * 0.99 for c in closes],
        "volume": volumes,
        "amount": [10000.0] * len(closes),
        "pct_change": pcts,
        "turnover": [1.0] * len(closes),
    })


def _seed(db):
    # 4 连板（主板 10%）
    save_kline_to_db("600001", _frame([0] * 16 + [10] * 4, [100] * 20), db_path=db)
    # 2 连板
    save_kline_to_db("600002", _frame([0] * 18 + [10, 10], [100] * 20), db_path=db)
    # 涨停后缩量回调：第 15 根涨停，之后回撤约 8%，最新量能 0.3 倍
    pcts = [0] * 14 + [10, -5, -1, -1, -1, 0]
    closes = [10] * 14 + [11, 10.45, 10.35, 10.25, 10.15, 10.15]
    lows = [c * 0.99 for c in closes]
    lows[15:] = [10.12, 10.12, 10.12, 10.12, 10.12]      # 距涨停收盘约 8%
    save_kline_to_db("600003", _frame(pcts, [1000] * 19 + [300], closes, lows), db_path=db)
    # 20 日新高 + 放量 3 倍
    save_kline_to_db("600004", _frame([0] * 19 + [3], [100] * 19 + [300]), db_path=db)
    # 689 是 20% 档：12% 连涨不是涨停
    save_kline_to_db("689001", _frame([0] * 17 + [12, 12, 12], [100] * 20), db_path=db)
    # 数据停在上一交易日（停牌/未更新），不应算作"当前连板"
    save_kline_to_db("600005", _frame([0] * 17 + [10, 10], [100] * 19), db_path=db)
    # 首板：此前 19 天无涨停，仅最新一天涨停（主板 10%）
    save_kline_to_db("600006", _frame([0] * 19 + [10], [100] * 20), db_path=db)

    conn = sqlite3.connect(db)
    rows = [("600001", "连板四", 14.0, 10.0), ("600002", "连板二", 12.0, 10.0),
            ("600003", "缩量回调", 10.15, -1.0), ("600004", "新高突破", 10.3, 3.0),
            ("689001", "科创CDR", 14.0, 12.0), ("600005", "停牌股", 12.0, 10.0),
            ("600006", "今日首板", 11.0, 10.0)]
    conn.executemany(
        "INSERT INTO stock_metadata (code, name, price, change_pct) VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = str(tmp_path / "screen_routes.db")
    init_db(db)
    _seed(db)
    import common.config as config
    monkeypatch.setattr(config, "DB_PATH", db)

    from flask import Flask
    from common.routes.screen_routes import screen_bp
    app = Flask("screen-test")
    app.register_blueprint(screen_bp)
    return app.test_client()


def _codes(response):
    payload = response.get_json()
    assert payload["success"] is True
    return [row["code"] for row in payload["data"]]


def test_consecutive_limit_up_counts_current_streak(client):
    payload = client.get("/api/screen/consecutive_limit_up?min_boards=2").get_json()
    by_code = {row["code"]: row for row in payload["data"]}
    assert by_code["600001"]["boards"] == 4
    assert by_code["600002"]["boards"] == 2
    # 689 是 20% 档，12% 连涨不算涨停；停牌/未更新到最新交易日的也不算当前连板
    assert "689001" not in by_code
    assert "600005" not in by_code


def test_consecutive_limit_up_min_boards_filters(client):
    assert "600002" not in _codes(client.get("/api/screen/consecutive_limit_up?min_boards=3"))


def test_shrink_pullback_hits_limit_up_then_shrinking_drop(client):
    payload = client.get("/api/screen/shrink_pullback").get_json()
    codes = [row["code"] for row in payload["data"]]
    assert codes == ["600003"]
    row = payload["data"][0]
    assert 7.0 <= row["pullback_pct"] <= 9.0
    assert row["vol_shrink_ratio"] <= 0.6


def test_shrink_pullback_volume_gate_excludes_heavy_drop(client):
    # vol_ratio 压到 0.2，0.3 倍的缩量就不达标了
    codes = _codes(client.get("/api/screen/shrink_pullback?vol_ratio=0.2"))
    assert "600003" not in codes


def test_new_high_breakout_requires_volume(client):
    codes = _codes(client.get("/api/screen/new_high_breakout"))
    assert "600004" in codes
    assert "600001" not in codes       # 创了新高但量能只有 1 倍
    tight = _codes(client.get("/api/screen/new_high_breakout?vol_ratio=5"))
    assert "600004" not in tight


def test_window_boundary_with_exactly_days_of_history(client):
    # days=20 正好等于库内历史长度：下界取第 20 个交易日，结果不能为空
    for url in ("/api/screen/big_up_days?days=20&min_count=1",
                "/api/screen/limit_up_days?days=20&min_count=1"):
        payload = client.get(url).get_json()
        assert payload["success"] is True
        assert payload["data"], url


def test_window_start_falls_back_when_history_shorter_than_days(tmp_path):
    # OFFSET 取不到日期时（days > 历史长度）必须退回最早一天，绝不能变成 NULL 下界
    db = str(tmp_path / "short.db")
    init_db(db)
    save_kline_to_db("600001", _frame([0, 0, 0], [100, 100, 100]), db_path=db)
    from common.routes.screen_routes import _window_start
    conn = sqlite3.connect(db)
    try:
        assert _window_start(conn.cursor(), 20) == DATES[0].strftime("%Y-%m-%d")
    finally:
        conn.close()


def test_new_high_breakout_needs_full_window(client):
    # 只有 19 根 K 线（停牌股）不满足"20 日新高"的窗口要求
    codes = _codes(client.get("/api/screen/new_high_breakout?days=20"))
    assert "600005" not in codes


def test_is_limit_up_treats_689_as_star_board():
    assert is_limit_up("689001", 19.6)
    assert not is_limit_up("689001", 12.0)
    assert is_limit_up("688001", 19.6)
    assert is_limit_up("302001", 19.6)
    assert is_limit_up("600001", 9.8)


def test_limit_up_sql_case_matches_python_threshold():
    # SQL 阈值片段必须与 Python 逐股判定同源，尤其 689 不能漏（M1 回归）
    from common.screening import limit_up_sql_case, limit_up_threshold
    conn = sqlite3.connect(":memory:")
    try:
        expr = limit_up_sql_case("code")
        for code in ("600001", "000001", "300001", "302001", "688001", "689001"):
            got = conn.execute(
                "SELECT {} FROM (SELECT ? AS code)".format(expr), (code,)
            ).fetchone()[0]
            assert got == limit_up_threshold(code), code
    finally:
        conn.close()


def test_first_limit_up_uses_star_board_threshold_for_689(client):
    # 689001 当日涨 12%：20% 档不算涨停，不能进首板列表（旧 SQL 漏 689 会误纳）；
    # 600006 此前无涨停、今日主板 10% 涨停，应入选
    codes = _codes(client.get("/api/screen/first_limit_up"))
    assert "689001" not in codes
    assert "600006" in codes


@pytest.mark.parametrize("url", [
    "/api/screen/first_limit_up",
    "/api/screen/limit_up_volume",
    "/api/screen/volume_surge",
    "/api/screen/consecutive_up",
    "/api/screen/limit_up_pullback",
    "/api/screen/big_up_days",
    "/api/screen/limit_up_days",
    "/api/screen/consecutive_limit_up",
    "/api/screen/shrink_pullback",
    "/api/screen/new_high_breakout",
])
def test_all_screen_routes_respond_with_json(client, url):
    # 覆盖全部路由：连接生命周期改造（with closing）后每条路径都必须能正常收尾
    resp = client.get(url)
    assert resp.status_code == 200, url
    payload = resp.get_json()
    assert payload["success"] is True, url
    assert isinstance(payload["data"], list), url


def test_bad_numeric_params_fall_back_instead_of_500(client):
    # 坏参数不应把接口打成 500，也不该让 limit 失控把整库拉出来
    for url in ("/api/screen/first_limit_up?limit=abc",
                "/api/screen/big_up_days?days=abc&threshold=xyz",
                "/api/screen/consecutive_limit_up?min_boards=-5",
                "/api/screen/new_high_breakout?days=999999&vol_ratio=oops"):
        resp = client.get(url)
        assert resp.status_code == 200, url
        assert resp.get_json()["success"] is True, url


def test_limit_param_is_clamped(client):
    rows = client.get("/api/screen/first_limit_up?limit=999999999").get_json()["data"]
    assert len(rows) <= 1000
