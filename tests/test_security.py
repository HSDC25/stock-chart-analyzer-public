"""Smoke tests for common.security pure helpers (no Flask needed)."""
import time

from common.security import RateLimiter, hmac_compare


def test_hmac_compare_equal():
    assert hmac_compare("abc", "abc") is True
    assert hmac_compare("", "") is True


def test_hmac_compare_diff():
    assert hmac_compare("abc", "abd") is False
    assert hmac_compare("abc", "") is False


def test_rate_limiter_burst():
    rl = RateLimiter(rate=10.0, burst=2)
    # 前两个令牌来自 burst，立即获得
    assert rl.acquire() == 0
    assert rl.acquire() == 0
    # 第三个需要等待（返回等待秒数 > 0）
    wait = rl.acquire()
    assert wait > 0


def test_rate_limiter_refill():
    rl = RateLimiter(rate=100.0, burst=1)
    assert rl.acquire() == 0          # 用掉唯一令牌
    assert rl.acquire() > 0           # 暂无令牌
    time.sleep(0.05)
    # rate=100/s => 0.05s 后应补充约 5 个令牌
    assert rl.acquire() == 0


def _app_with_security():
    from flask import Flask

    from common.security import register_security

    app = Flask(__name__)
    app.config["TESTING"] = True
    register_security(app)

    @app.route("/ping")
    def _ping():
        return "pong"

    return app


def test_csp_hardens_object_base_and_framing():
    """CSP 需关掉插件与 base-uri 劫持面，并锁死 frame-ancestors。"""
    headers = _app_with_security().test_client().get("/ping").headers
    csp = headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "frame-ancestors 'self'" in csp
