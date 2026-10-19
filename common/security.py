"""Security utilities for Stock Chart Analyzer Flask applications.

Extracted from win11/app.py: rate limiting, CSRF protection, security headers,
CORS enforcement, and provider health / circuit-breaker logic.

Compatible with Python 3.8+.

简介：安全与稳定性基础设施——IP 限流、CSRF 校验、安全响应头、CORS
白名单，以及数据源健康度/熔断与全源封锁冷却。Flask 应用启动时通过
register_security(app) 一次性挂载；RateLimiter/ProviderHealth 也被
fetcher 直接使用。
"""
from __future__ import annotations

import hmac as _hmac_mod
import logging
import os
import secrets
import threading
import time
from functools import wraps
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("stockchart")


# =============================================================================
# Allowed origins (CORS whitelist)
# =============================================================================

ALLOWED_ORIGINS = os.environ.get(
    'FLASK_ALLOWED_ORIGINS',
    'http://127.0.0.1:5000,http://localhost:5000',
).split(',')
ALLOWED_ORIGINS = [o.strip() for o in ALLOWED_ORIGINS if o.strip()]


# =============================================================================
# Rate limiter (Token Bucket algorithm)
# =============================================================================

class RateLimiter:
    """Simple Token Bucket rate limiter."""

    def __init__(self, rate, burst=1):
        # type: (float, int) -> None
        """
        Args:
            rate: Allowed requests per second.
            burst: Maximum burst size.
        """
        self.rate = rate
        self.burst = burst
        self.tokens = float(burst)
        self.last_time = time.time()
        self.lock = threading.Lock()

    def acquire(self):
        # type: () -> float
        """Acquire one token.  Returns 0 on success, else seconds to wait."""
        with self.lock:
            now = time.time()
            elapsed = now - self.last_time
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            self.last_time = now

            if self.tokens >= 1:
                self.tokens -= 1
                return 0
            else:
                wait_time = (1 - self.tokens) / self.rate
                return wait_time

    def wait(self):
        # type: () -> None
        """Block until a token is available."""
        while True:
            wait_time = self.acquire()
            if wait_time == 0:
                return
            time.sleep(wait_time)


# =============================================================================
# API call-count rate limiting (per-IP sliding window)
# =============================================================================

# Internal module-level state -- not part of the public API but documented here
# so callers understand what lives at module scope.
_api_call_count = {}   # type: Dict[str, List[float]]
_api_lock = threading.Lock()
_api_count_last_cleanup = time.time()
_api_count_cleanup_interval = 300  # clean up stale records every 5 minutes


def _cleanup_api_call_count():
    # type: () -> None
    """Periodically purge expired entries from ``_api_call_count``."""
    global _api_count_last_cleanup, _api_call_count
    now = time.time()
    if now - _api_count_last_cleanup < _api_count_cleanup_interval:
        return
    with _api_lock:
        expired = []
        for ip, times in _api_call_count.items():
            _api_call_count[ip] = [t for t in times if t > now - 120]
            if not _api_call_count[ip]:
                expired.append(ip)
        for ip in expired:
            del _api_call_count[ip]
    _api_count_last_cleanup = now


def api_rate_limit(max_calls=30, window=60):
    # type: (int, int) -> Callable
    """Decorator: limit each IP to *max_calls* requests per *window* seconds.

    Counting is keyed on ``request.remote_addr`` (the TCP peer), deliberately
    NOT on ``X-Forwarded-For``: that header is client-controlled, so an
    attacker could rotate it per request to bypass the limit entirely. This
    app binds to 127.0.0.1 without a reverse proxy, so remote_addr is the
    honest client address.
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            _cleanup_api_call_count()
            from flask import request, jsonify
            ip = request.remote_addr or '0.0.0.0'
            now = time.time()
            with _api_lock:
                if ip not in _api_call_count:
                    _api_call_count[ip] = []
                _api_call_count[ip] = [t for t in _api_call_count[ip] if t > now - window]
                if len(_api_call_count[ip]) >= max_calls:
                    logger.warning(
                        "Rate limit hit: ip=%s path=%s count=%d",
                        ip, request.path, len(_api_call_count[ip]),
                    )
                    return jsonify({
                        'success': False,
                        'error': '请求过于频繁，请在 %d 秒后重试' % window,
                    }), 429
                _api_call_count[ip].append(now)
            return f(*args, **kwargs)
        return wrapper
    return decorator


# =============================================================================
# Provider health / circuit breaker
# =============================================================================

class ProviderHealth:
    """Simple circuit breaker: enters cooldown after consecutive failures."""

    def __init__(self, fail_threshold=2, cooldown=300):
        # type: (int, int) -> None
        self.fail_threshold = fail_threshold
        self.cooldown = cooldown
        self.lock = threading.Lock()
        self.data = {}  # type: Dict[str, Dict[str, Any]]

    def _ensure(self, name):
        # type: (str) -> None
        """确保 *name* 的计数条目存在（惰性初始化）。"""
        if name not in self.data:
            self.data[name] = {'consecutive': 0, 'cooldown_until': 0.0}

    def ok(self, name):
        # type: (str) -> bool
        """数据源 *name* 当前是否可用（不在熔断冷却期）。"""
        with self.lock:
            self._ensure(name)
            return time.time() >= self.data[name]['cooldown_until']

    def record_success(self, name):
        # type: (str) -> None
        """记录一次成功：清零连续失败计数。"""
        with self.lock:
            self._ensure(name)
            self.data[name]['consecutive'] = 0

    def record_fail(self, name):
        # type: (str) -> None
        """记录一次失败：连续失败达到阈值即进入冷却期（熔断）。"""
        with self.lock:
            self._ensure(name)
            self.data[name]['consecutive'] += 1
            if self.data[name]['consecutive'] >= self.fail_threshold:
                self.data[name]['cooldown_until'] = time.time() + self.cooldown


# =============================================================================
# Global block detection (all-provider cooldown)
# =============================================================================

_global_block_lock = threading.Lock()
_global_block_until = 0.0
_global_fail_count = 0


def global_block_detect(cooldown=60):
    # type: (int) -> None
    """Trigger a global cooldown when all data sources fail repeatedly."""
    global _global_fail_count, _global_block_until
    with _global_block_lock:
        _global_fail_count += 1
        if _global_fail_count >= 3:
            _global_block_until = time.time() + cooldown
            logger.info(
                "[preload] Global cooldown triggered: %ds", cooldown,
            )


def global_block_wait(max_wait=60):
    # type: (int) -> bool
    """Wait for the global cooldown to expire.  Returns True if we waited."""
    with _global_block_lock:
        wait = min(_global_block_until - time.time(), max_wait)
    if wait > 0:
        time.sleep(wait)
        return True
    return False


def global_block_success():
    # type: () -> None
    """Reset the global failure counter after any successful fetch."""
    global _global_fail_count
    with _global_block_lock:
        _global_fail_count = 0


# =============================================================================
# CSRF helpers
# =============================================================================

# =============================================================================
# CSRF-exempt paths (module-level so launchers can register extra ones, e.g.
# the local-only /shutdown endpoint — the route itself must then enforce its
# own auth, typically a random per-run token)
# =============================================================================

_CSRF_EXEMPT_PATHS = {
    '/', '/api/preload/status', '/api/data-status',
    '/api/market_status', '/api/default-save-dir',
}


def exempt_csrf_path(path):
    # type: (str) -> None
    """Exclude *path* from CSRF validation.

    Only for machine-to-machine local endpoints that carry their own
    authentication (e.g. a random startup token). Never exempt a path that
    is reachable from a browser without auth.
    """
    _CSRF_EXEMPT_PATHS.add(path)


def generate_csrf_token():
    # type: () -> str
    """Generate a CSRF token and store it in the Flask session."""
    from flask import session
    token = secrets.token_urlsafe(32)
    session['csrf_token'] = token
    return token


def validate_csrf_token():
    # type: () -> bool
    """Validate the CSRF token supplied in the current request."""
    from flask import request, session
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return True  # safe methods need no validation

    token = (
        request.headers.get('X-CSRF-Token')
        or request.form.get('csrf_token')
        or (request.get_json(silent=True) or {}).get('csrf_token')
    )

    expected = session.get('csrf_token')
    if not expected or not token:
        logger.warning(
            "CSRF validation failed: token missing [ip=%s]",
            request.remote_addr,
        )
        return False

    return hmac_compare(expected, token)


def hmac_compare(a, b):
    # type: (str, str) -> bool
    """Constant-time string comparison to prevent timing attacks."""
    return _hmac_mod.compare_digest(str(a), str(b))


# =============================================================================
# register_security -- single entry point to wire everything onto a Flask app
# =============================================================================

def register_security(app):
    # type: (Any) -> None
    """Register all security hooks, routes, and context processors on *app*.

    This wires up:
    * Security response headers (after_request)
    * CORS whitelist enforcement (after_request)
    * CSRF protection (before_request)
    * ``/api/csrf-token`` GET route
    * ``inject_csrf_token`` template context processor
    * ``MAX_CONTENT_LENGTH`` from ``common.config``
    """
    from common.config import MAX_CONTENT_LENGTH

    # -- Limit request body size -----------------------------------------------
    app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

    # -- Secret key (for session signing / CSRF) --------------------------------
    if not app.secret_key:
        app.secret_key = os.environ.get('FLASK_SECRET_KEY', os.urandom(32))

    # -- Security headers (after_request) --------------------------------------
    @app.after_request
    def add_security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['X-XSS-Protection'] = '1; mode=block'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        # script/style 仅允许本站 + 内联（内联 onclick 处理器仍在使用，
        # 移除 'unsafe-inline' 需先完成 nonce 基建 + 前端去内联化）。
        # 曾放行的 cdn.jsdelivr.net / cdnjs.cloudflare.com 已移除：
        # 全部模板的 echarts/xlsx 均为本地静态资源，无 CDN 引用。
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-ancestors 'self'"
        )
        response.headers['Strict-Transport-Security'] = (
            'max-age=31536000; includeSubDomains'
        )
        return response

    # -- CORS whitelist enforcement (after_request) ----------------------------
    @app.after_request
    def cors_enforcement(response):
        from flask import request
        origin = request.headers.get('Origin', '')
        if origin in ALLOWED_ORIGINS:
            response.headers['Access-Control-Allow-Origin'] = origin
        elif not origin:
            pass  # same-origin request, allow through
        else:
            response.headers.pop('Access-Control-Allow-Origin', None)
        response.headers['Access-Control-Allow-Headers'] = (
            'Content-Type,Authorization'
        )
        response.headers['Access-Control-Allow-Methods'] = (
            'GET,PUT,POST,DELETE,OPTIONS'
        )
        response.headers['Vary'] = 'Origin'
        return response

    # -- CSRF protection (before_request) --------------------------------------
    @app.before_request
    def csrf_protect():
        from flask import request, jsonify
        if request.path in _CSRF_EXEMPT_PATHS:
            return
        if request.method in ('POST', 'PUT', 'DELETE'):
            if not validate_csrf_token():
                logger.warning(
                    "CSRF rejected: path=%s method=%s ip=%s",
                    request.path, request.method, request.remote_addr,
                )
                return jsonify({
                    'success': False,
                    'error': 'CSRF token 无效，请刷新页面重试',
                }), 403

    # -- CSRF token endpoint ---------------------------------------------------
    @app.route('/api/csrf-token', methods=['GET'])
    def api_csrf_token():
        from flask import jsonify
        return jsonify({'success': True, 'csrf_token': generate_csrf_token()})

    # -- Inject CSRF token into Jinja templates --------------------------------
    @app.context_processor
    def inject_csrf_token():
        from flask import session
        return {'csrf_token': session.get('csrf_token', '')}
