"""Stock Chart Analyzer - centralized configuration and shared utilities.

Extracts logging, SSL, data-path, and urllib patching logic from win11/app.py
into a reusable module that is safe to import without side effects (logging
and urllib patching are opt-in via explicit function calls).

Compatible with Python 3.8+.

简介：全局配置中心——集中定义常量、数据目录/数据库路径、SSL 上下文、
日志初始化，并可选地给 urllib.urlopen 打上 SSRF 防护补丁。
应用启动时最先导入本模块；导入无副作用，日志与补丁均需显式调用开启。
"""
from __future__ import annotations

import logging
import os
import ssl
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


# ────────────────────────────────────────────────────────────────────────────
# Named constants (replaces magic numbers scattered through the codebase)
# ────────────────────────────────────────────────────────────────────────────

CACHE_TTL_SECONDS: int = 300
LIMIT_UP_MAIN_BOARD: float = 9.8
LIMIT_UP_GEM: float = 19.5          # ChiNext / STAR board
MAX_CONTENT_LENGTH: int = 16 * 1024 * 1024   # 16 MB
PRELOAD_BATCH_SIZE: int = 50
KLINE_KEEP_DAYS: int = 120
RATE_LIMIT_BURST: int = 12
RATE_LIMIT_REFILL: int = 5


# ────────────────────────────────────────────────────────────────────────────
# Data directory & database path
# ────────────────────────────────────────────────────────────────────────────

DATA_DIR: Path = Path.home() / "Documents" / "StockChart"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH: str = str(DATA_DIR / "stock_data.db")
PRELOAD_STATE_FILE: Path = DATA_DIR / "preload_state.json"


# ────────────────────────────────────────────────────────────────────────────
# SSL context setup
# ────────────────────────────────────────────────────────────────────────────

SKIP_SSL_VERIFY: bool = os.environ.get("STOCKCHART_SKIP_SSL_VERIFY", "0") == "1"

try:
    import certifi
    SSL_CONTEXT: ssl.SSLContext = ssl.create_default_context(cafile=certifi.where())
except Exception:
    SSL_CONTEXT = ssl.create_default_context()

if SKIP_SSL_VERIFY:
    SSL_CONTEXT.check_hostname = False
    SSL_CONTEXT.verify_mode = ssl.CERT_NONE


# ────────────────────────────────────────────────────────────────────────────
# Logging configuration
# ────────────────────────────────────────────────────────────────────────────

class _SensitiveDataFilter(logging.Filter):
    """Prevent sensitive values (secrets, tokens, passwords) from appearing in logs."""

    _SENSITIVE_PATTERNS = [
        "secret", "token", "password", "api_key", "apikey", "auth",
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.getMessage()).lower()
        for pattern in self._SENSITIVE_PATTERNS:
            if pattern in msg:
                record.msg = "[FILTERED - 敏感信息已过滤]"
                record.args = ()
                break
        return True


def configure_logging() -> logging.Logger:
    """Set up rotating file + console logging and return the ``stockchart`` logger.

    * File handler: rotates at 5 MB, keeps 3 backups, level INFO.
    * Console handler: level WARNING.
    * A ``_SensitiveDataFilter`` is attached automatically.
    """
    log_dir = Path.home() / "Documents" / "StockChart"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "stockchart.log"

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.WARNING)

    logging.basicConfig(
        level=logging.INFO,
        handlers=[file_handler, console_handler],
    )

    _logger = logging.getLogger("stockchart")
    _logger.addFilter(_SensitiveDataFilter())
    _logger.info("日志系统初始化完成 -> %s", log_file)

    if SKIP_SSL_VERIFY:
        _logger.info("[SSL] 警告：SSL 证书验证已禁用（开发模式）")

    return _logger


# ────────────────────────────────────────────────────────────────────────────
# urllib monkey-patch (URL validation + automatic SSL context injection)
# ────────────────────────────────────────────────────────────────────────────

_orig_urlopen = urllib.request.urlopen


def _is_private_ip(ip: str) -> bool:
    """判断 *ip* 是否为内网/链路本地地址（含 IPv6 私有段与 IPv4-mapped IPv6）。"""
    if ip.startswith(("10.", "192.168.", "169.254.", "0.")):
        return True
    if ip.startswith("172."):
        # 172.16.0.0/12
        try:
            second = int(ip.split(".")[1])
        except (IndexError, ValueError):
            return True
        if 16 <= second <= 31:
            return True
    low = ip.lower()
    if low in ("::", "::1", "fe80::", "fc00::"):
        return True
    if low.startswith(("fe80:", "fc", "fd")):  # 链路本地 fe80::/10 与 ULA fc00::/7
        return True
    if low.startswith("::ffff:"):  # IPv4-mapped IPv6，如 ::ffff:10.0.0.1
        return _is_private_ip(low[len("::ffff:"):])
    return False


def _validate_url(url: object) -> None:
    """Reject non-HTTP(S) schemes and private/internal IPs to prevent SSRF.

    注意两点局限（当前威胁模型下可接受，应用仅访问硬编码金融数据源）：
    1. DNS 解析失败视为不可信，直接拒绝（不做放行）；
    2. 解析发生在校验时、请求时各一次，理论上存在 DNS 重绑定窗口。
    """
    from urllib.parse import urlparse
    import socket as _socket

    try:
        raw = url.full_url if hasattr(url, "full_url") else str(url)
        parsed = urlparse(raw)
    except Exception:
        raise ValueError(f"无效的 URL 格式: {url}")

    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"不安全的 URL 协议 '{parsed.scheme}'，仅允许 http/https: {url}"
        )

    hostname = parsed.hostname
    if hostname:
        try:
            addr_info = _socket.getaddrinfo(hostname, None)
        except _socket.gaierror:
            # 解析不了的主机名一律拒绝，避免依赖后续 DNS 层行为
            raise ValueError(f"无法解析的主机名: {hostname}")
        except ValueError:
            raise
        for _family, _type_, _proto, _canonname, sockaddr in addr_info:
            ip = sockaddr[0]
            # Allow loopback (desktop app health-checks against local Flask)
            if ip.startswith("127.") or ip == "::1":
                continue
            if _is_private_ip(ip):
                raise ValueError(f"禁止访问内网地址: {ip}")


def _patched_urlopen(url, data=None, timeout=15, context=None, **kwargs):
    """Drop-in replacement for ``urllib.request.urlopen`` with safety checks.

    请求前先做 SSRF 校验；未显式传 SSL 上下文时注入全局 SSL_CONTEXT，
    并补上 Accept-Encoding 头以便 gzip 压缩传输。
    """
    _validate_url(url)
    if context is None:
        context = SSL_CONTEXT
    if "headers" in kwargs:
        headers = kwargs["headers"]
        if "Accept-Encoding" not in headers:
            headers["Accept-Encoding"] = "gzip, deflate"
    return _orig_urlopen(url, data=data, timeout=timeout, context=context, **kwargs)


def install_urllib_patch() -> None:
    """Monkey-patch ``urllib.request.urlopen`` with URL-validation wrapper.

    Call once during application startup before any network requests.
    """
    urllib.request.urlopen = _patched_urlopen
