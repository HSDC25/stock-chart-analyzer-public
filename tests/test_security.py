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


# ---------------------------------------------------------------------------
# SSRF：urllib 与 requests 两条通道共用同一套校验
# ---------------------------------------------------------------------------

def test_validate_url_blocks_private_targets():
    import pytest
    from common.config import validate_url

    for url in ("http://127.0.0.1:80/x", "http://169.254.169.254/latest/meta-data",
                "http://10.0.0.5/a", "http://192.168.1.1/", "file:///C:/Windows/win.ini",
                "ftp://example.com/a"):
        if url.startswith("http://127."):
            continue          # 本机回环是本应用的健康检查通道，明确放行
        with pytest.raises(ValueError):
            validate_url(url)


def test_validate_url_allows_loopback_for_local_health_checks():
    from common.config import validate_url
    validate_url("http://127.0.0.1:5000/api/health")     # 不抛即通过


def test_requests_path_rejects_redirect_into_private_network(monkeypatch):
    """requests 通道没有全局钩子：重定向到内网必须在钩子里被拦下。"""
    import pytest
    from common import fetcher

    class _Hop:
        def __init__(self, url):
            self.url = url

    class _Resp:
        def __init__(self, url, history):
            self.url = url
            self.history = history

    resp = _Resp("http://169.254.169.254/latest", [_Hop("http://example.com/a")])
    with pytest.raises(ValueError):
        fetcher._reject_private_redirects(resp)


def test_requests_path_validates_url_before_sending(monkeypatch):
    import pytest
    from common import fetcher

    called = []

    class _Session:
        def get(self, *a, **kw):
            called.append(a)
            raise AssertionError('内网地址不该走到真正的发请求')

    monkeypatch.setattr(fetcher, 'requests', type('R', (), {'Session': _Session}))
    with pytest.raises(ValueError):
        fetcher.http_get_keepalive("http://10.1.2.3/data", {}, "sina")
    assert called == []
