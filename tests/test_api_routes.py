"""Flask API integration tests using test_client + temp SQLite.

Tests route registration, request validation, and error handling.
External APIs (baostock, Sina, Tencent) are NOT called.
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

pd = pytest.importorskip("pandas")


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """Create a Flask test client with a temp database."""
    db_dir = tmp_path_factory.mktemp("api_test")
    db_path = str(db_dir / "test.db")

    import common.config as cfg
    original_db = cfg.DB_PATH
    cfg.DB_PATH = db_path

    from common.db import init_db
    init_db(db_path)

    # Seed metadata
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO stock_metadata"
        " (code, name, price, change_pct, pe, pb, market_cap, volume, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("600000", "浦发银行", 10.5, 1.2, 5.0, 0.8, 3e10, 1e6, "2024-06-14 15:00:00"),
    )
    conn.commit()
    conn.close()

    sys.path.insert(0, os.path.join(ROOT, "win11"))
    for mod in list(sys.modules):
        if mod == "app" or (mod.startswith("common") and "routes" in mod):
            del sys.modules[mod]

    import app as app_module

    # Point template/static to win11 theme dirs so templates resolve
    theme_dir = os.path.join(ROOT, "win11", "themes", "dark")
    if os.path.isdir(theme_dir):
        app_module.app.template_folder = os.path.join(theme_dir, "templates")
        app_module.app.static_folder = os.path.join(theme_dir, "static")

    app_module.app.config["TESTING"] = True
    app_module.app.config["WTF_CSRF_ENABLED"] = False

    with app_module.app.test_client() as c:
        yield c

    cfg.DB_PATH = original_db
    sys.path.remove(os.path.join(ROOT, "win11"))


class TestHealthAndIndex:
    def test_index_returns_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_data_status(self, client):
        resp = client.get("/api/data-status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)

    def test_market_status(self, client):
        resp = client.get("/api/market_status")
        assert resp.status_code == 200

    def test_csrf_token(self, client):
        resp = client.get("/api/csrf-token")
        assert resp.status_code == 200


class TestStockEndpoints:
    def test_stocks_post(self, client):
        csrf_resp = client.get("/api/csrf-token")
        token = csrf_resp.get_json()["csrf_token"]
        resp = client.post(
            "/api/stocks",
            data="{}",
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 200

    def test_kline_invalid_code_rejected(self, client):
        resp = client.get("/api/stock/INVALID/kline")
        assert resp.status_code in (400, 403, 404, 500)

    def test_kline_valid_code(self, client):
        resp = client.get("/api/stock/600000/kline?days=5")
        assert resp.status_code in (200, 404)

    def test_top_change(self, client):
        resp = client.get("/api/top-change")
        assert resp.status_code == 200


class TestScreeningEndpoints:
    """All screening routes are GET and should return 200 even with empty data."""

    @pytest.mark.parametrize("path", [
        "/api/screen/big_up_days",
        "/api/screen/consecutive_up",
        "/api/screen/first_limit_up",
        "/api/screen/limit_up_days",
        "/api/screen/limit_up_pullback",
        "/api/screen/limit_up_volume",
        "/api/screen/volume_surge",
    ])
    def test_screen_endpoint(self, client, path):
        resp = client.get(path)
        assert resp.status_code == 200


class TestRankEndpoints:
    def test_rank_top3day(self, client):
        resp = client.get("/api/rank/top3day")
        assert resp.status_code == 200

    def test_rank_top5day(self, client):
        resp = client.get("/api/rank/top5day")
        assert resp.status_code == 200


class TestMLEndpoints:
    def test_ml_predict_get(self, client):
        resp = client.get("/api/ml/predict/600000")
        # May fail without real K-line data but should not crash
        assert resp.status_code in (200, 400, 500)

    def test_ml_screen(self, client):
        resp = client.get("/api/ml/screen")
        assert resp.status_code == 200

    def test_ml_rf_predict(self, client):
        resp = client.get("/api/ml/rf_predict/600000")
        assert resp.status_code in (200, 400, 500)

    def test_ml_screen_async_lifecycle(self, client, tmp_path, monkeypatch):
        """异步筛选作业：start → status 轮询至 done，应返回结果列表。

        ml_module 使用自己的 DB_PATH（默认指向用户真实数据库），这里
        monkeypatch 到内联微型库，保证测试快速且不依赖本机数据。
        """
        import sqlite3 as _sqlite3
        import time as _time

        import ml_module
        from common.db import init_db

        db_path = str(tmp_path / "ml_test.db")
        init_db(db_path)
        conn = _sqlite3.connect(db_path)
        for code in ("000001", "600000"):
            for i in range(35):
                conn.execute(
                    "INSERT OR REPLACE INTO kline_data VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (code, f"2024-01-{i + 1:02d}" if i < 30 else f"2024-02-{i - 29:02d}",
                     10.0, 10.0 + (i % 5), 11.0, 9.5, 1e6, 1e7, 2.0,
                     4.0 if i % 7 == 0 else 0.5, 0.1, 1.0),
                )
            conn.execute(
                "INSERT OR REPLACE INTO stock_metadata"
                " (code, name, price, change_pct, pe, pb, market_cap, volume)"
                " VALUES (?, ?, 10.0, 1.0, 5.0, 0.8, 3e9, 1e6)",
                (code, "测试" + code),
            )
        conn.commit()
        conn.close()
        monkeypatch.setattr(ml_module, "DB_PATH", db_path)

        csrf_resp = client.get("/api/csrf-token")
        token = csrf_resp.get_json()["csrf_token"]
        resp = client.post(
            "/api/ml/screen/start?top_n=5",
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 200
        job_id = resp.get_json()["job_id"]

        final = None
        for _ in range(60):
            status_resp = client.get(f"/api/ml/screen/status?job_id={job_id}")
            assert status_resp.status_code == 200
            body = status_resp.get_json()
            assert body["success"] is True
            if body["status"] in ("done", "error"):
                final = body
                break
            _time.sleep(0.5)
        assert final is not None, "ML screen job did not finish in 30s"
        assert final["status"] == "done"
        assert isinstance(final.get("data"), list)

    def test_ml_screen_start_conflict_without_token_rejected(self, client):
        """无 CSRF token 的 POST 应被拒绝（403）。"""
        resp = client.post("/api/ml/screen/start?top_n=5")
        assert resp.status_code == 403


class TestScreeningSQLRewrites:
    """P3 SQL 改写后的筛选路由：正常路径 + 窗口函数/回退逻辑可用。"""

    def test_big_up_days_shape(self, client):
        resp = client.get("/api/screen/big_up_days?days=30&threshold=3.5")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert isinstance(body["data"], list)
        for row in body["data"]:
            assert "big_up_days" in row and "total_days" in row

    def test_limit_up_pullback_shape(self, client):
        resp = client.get("/api/screen/limit_up_pullback")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert isinstance(body["data"], list)

    def test_big_up_days_with_data(self, client):
        """带种子数据的回归测试。

        曾因窗口 SQL 增加返回列而解包循环未同步（3 列 vs 5 列）导致
        线上 500——空库测试覆盖不到，必须有真实行数走通全链路。
        """
        import sqlite3

        import common.config as cfg

        conn = sqlite3.connect(cfg.DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO stock_metadata (code, name, price)"
            " VALUES ('000001', '平安银行', 10.0)"
        )
        for i in range(30):
            conn.execute(
                "INSERT OR REPLACE INTO kline_data VALUES"
                " (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("000001", f"2024-03-{i + 1:02d}", 10, 10, 10.5, 9.5,
                 1e6, 1e7, 1.0,
                 4.0 if i % 3 == 0 else 0.2,  # 10 天涨幅 ≥3.5%
                 0.1, 1.0),
            )
        conn.commit()
        conn.close()

        resp = client.get("/api/screen/big_up_days")
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        hit = [r for r in data if r["code"] == "000001"]
        assert hit, "000001 应命中（价格带内、区间涨幅 0%、10 天大涨）"
        assert hit[0]["big_up_days"] == 10
        assert hit[0]["total_days"] == 30

    def test_limit_up_days_board_rules(self, client):
        """涨停天数排行：主板 9.8% / 创业板 19.5% 板块规则 + 排序正确。"""
        import sqlite3

        import common.config as cfg

        conn = sqlite3.connect(cfg.DB_PATH)
        seeds = [
            # (code, name, 每日 pct_change 模式)
            ("600101", "主板多板", [10.0, 10.0, 10.0, 0.1]),   # 20日内 15 次涨停
            ("300101", "创业单板", [10.0, 10.0, 0.1, 0.1]),    # 10% < 19.5% → 0 次
            ("300102", "创业高板", [20.0, 0.1, 0.1, 0.1]),     # 20% ≥ 19.5% → 5 次
            ("600102", "ST排除", [10.0, 10.0, 0.1, 0.1]),      # ST 名整体排除
        ]
        for code, name, pattern in seeds:
            conn.execute(
                "INSERT OR REPLACE INTO stock_metadata (code, name, price)"
                " VALUES (?, ?, 10.0)", (code, name))
            for i in range(20):
                conn.execute(
                    "INSERT OR REPLACE INTO kline_data VALUES"
                    " (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (code, f"2024-03-{i + 1:02d}", 10, 10, 10.5, 9.5,
                     1e6, 1e7, 1.0, pattern[i % len(pattern)], 0.1, 1.0))
        conn.execute(
            "INSERT OR REPLACE INTO stock_metadata (code, name, price)"
            " VALUES ('600103', 'ST主板', 10.0)")
        for i in range(20):
            conn.execute(
                "INSERT OR REPLACE INTO kline_data VALUES"
                " (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("600103", f"2024-03-{i + 1:02d}", 10, 10, 10.5, 9.5,
                 1e6, 1e7, 1.0, 10.0 if i % 4 < 2 else 0.1, 0.1, 1.0))
        conn.commit()
        conn.close()

        resp = client.get("/api/screen/limit_up_days?days=20&min_count=1")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        data = body["data"]
        by_code = {r["code"]: r for r in data}

        assert "600103" not in by_code, "ST 股应被排除"
        assert by_code.get("600101", {}).get("limit_up_days") == 15
        assert "300101" not in by_code, "创业板 10% 不算涨停"
        assert by_code.get("300102", {}).get("limit_up_days") == 5
        # 降序：主板多板应排在创业高板之前
        ranks = [r["code"] for r in data]
        assert ranks.index("600101") < ranks.index("300102")

    def test_big_up_days_tightened_conditions(self, client):
        """大涨天数排行新增收紧条件：min_limit_up / max_drop_pct / exclude_st。"""
        resp = client.get("/api/screen/big_up_days?days=20&min_limit_up=2"
                          "&exclude_st=1&limit=200")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        for row in body["data"]:
            assert row["limit_up_days"] >= 2
            assert "ST" not in (row.get("name") or "").upper()
        # 命中集合应是宽松条件的子集
        loose = client.get("/api/screen/big_up_days?days=20").get_json()["data"]
        assert len(body["data"]) <= len(loose)

        resp = client.get("/api/screen/big_up_days?days=20&max_drop_pct=-9"
                          "&limit=200")
        body = resp.get_json()
        assert body["success"] is True
        # 000001 种子数据含 -0.1~4.0 的跌幅不深于 -9，仍应在结果中
        codes = {r["code"] for r in body["data"]}
        assert "000001" in codes or not body["data"]


class TestChartExport:
    def test_show_save_dialog_is_post_only(self, client):
        """有副作用的端点不许被 GET 触发：GET 不走 CSRF 校验，任意网页都能打过来。"""
        assert client.get("/api/show-save-dialog").status_code == 405

        token = client.get("/api/csrf-token").get_json()["csrf_token"]
        resp = client.post("/api/show-save-dialog", headers={"X-CSRF-Token": token})
        assert resp.status_code != 405


class TestPreloadEndpoints:
    def test_preload_status(self, client):
        resp = client.get("/api/preload/status")
        assert resp.status_code == 200
