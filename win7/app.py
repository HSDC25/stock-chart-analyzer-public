#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StockChart Flask 主应用入口（Win7 版，兼容 Python 3.8+）。

本文件是对 ``common`` 包的薄封装：所有业务逻辑（路由、安全、数据库、
数据抓取）都在 common/ 中实现，这里只负责组装 Flask 应用，并把各路由
蓝图延迟导入所需的名字（fetcher、background_preload 等）重新导出到
模块级别。与 win11/app.py 的区别：Win7 无 WebView2，图表保存对话框
走 PyQt5 回退实现；launcher_win7.py 通过 ``from app import app`` 取得
Flask 实例后在本地起服务。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import warnings
from pathlib import Path
from typing import Dict, Optional

# Suppress noisy SSL / third-party warnings early
warnings.filterwarnings("ignore")

try:
    import requests as _requests
    _requests.packages.urllib3.disable_warnings()
except Exception:
    pass

# Ensure the project root (parent of win7/) is on sys.path so that
# ``common`` is importable even when a launcher only adds its own
# directory (win7/) to sys.path.
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ────────────────────────────────────────────────────────────────────────────
# common/ imports — single source of truth for all backend logic
# ────────────────────────────────────────────────────────────────────────────

from common.config import (          # noqa: F401  (re-exported)
    CACHE_TTL_SECONDS,
    DATA_DIR,
    DB_PATH,
    KLINE_KEEP_DAYS,
    LIMIT_UP_GEM,
    LIMIT_UP_MAIN_BOARD,
    MAX_CONTENT_LENGTH,
    PRELOAD_BATCH_SIZE,
    PRELOAD_STATE_FILE,
    RATE_LIMIT_BURST,
    RATE_LIMIT_REFILL,
    SKIP_SSL_VERIFY,
    SSL_CONTEXT,
    configure_logging,
    install_urllib_patch,
)

from common.security import (       # noqa: F401
    ALLOWED_ORIGINS,
    ProviderHealth,
    RateLimiter,
    api_rate_limit,
    generate_csrf_token,
    global_block_detect,
    global_block_success,
    global_block_wait,
    hmac_compare,
    register_security,
    validate_csrf_token,
)

from common.db import (             # noqa: F401
    check_local_data_status,
    get_kline_from_db,
    get_read_conn,
    init_db,
    is_data_fresh,
    load_stock_metadata,
    read_last_bars_from_db,
    save_kline_to_db,
)

# common.fetcher / screening / indicators 三个模块携带 pandas、numpy、
# baostock（约占启动黑箱期 2~5 秒），不再顶层导入——见文末惰性导出区。

from common.preload import (        # noqa: F401
    PreloadState,
    clear_preload_state,
    load_preload_state,
    save_preload_state,
    set_thread_low_priority,
)

from common.models import (         # noqa: F401
    FilterCriteria,
    is_allowed_stock_code,
    is_stock_st,
    validate_stock_code_format,
)

from common.trading_calendar import (  # noqa: F401
    is_market_open_now,
    is_trading_day,
    trading_day_fetch_range,
)

from common.routes.api_routes import api_bp
from common.routes.screen_routes import screen_bp
from common.routes.ml_routes import ml_bp
from common.routes.chart_routes import chart_bp
from common.routes.assets_routes import assets_bp
from common.routes.update_routes import update_bp


# ────────────────────────────────────────────────────────────────────────────
# Logging & urllib patch
# ────────────────────────────────────────────────────────────────────────────

logger = configure_logging()
install_urllib_patch()


# ────────────────────────────────────────────────────────────────────────────
# Module-level state required by route blueprints (deferred ``from app import …``)
# ────────────────────────────────────────────────────────────────────────────

#: Background preload state object (new API used by api_routes).
preload_state = PreloadState()

# 旧版 _preload_* 全局已由 PreloadState 取代（无调用方），不再导出；
# background_preload 同样走文末惰性导出（路由会显式传入 preload_state）。


# ────────────────────────────────────────────────────────────────────────────
# Save-dialog helper (no webview on Win7; try PyQt5 fallback)
# ────────────────────────────────────────────────────────────────────────────

SAVE_DIALOG_FN = None

try:
    from PyQt5.QtWidgets import QFileDialog, QApplication  # type: ignore
    from PyQt5.QtCore import (  # type: ignore
        QObject, QMetaObject, Qt, pyqtSlot,
    )

    class _DialogRunner(QObject):
        """在 Qt 主线程中弹出保存对话框的辅助对象。

        QApplication 只允许在主线程使用 GUI；此回调由 Flask 请求线程触发，
        launcher 模式下必须把对话框调度回主线程（BlockingQueuedConnection
        阻塞等待结果），否则 Qt 可能随机崩溃。
        """

        def __init__(self):
            super().__init__()
            self.path = None

        @pyqtSlot()
        def _run(self):
            path, _ = QFileDialog.getSaveFileName(
                None, "Save Chart", "chart.png", "PNG Files (*.png)"
            )
            self.path = path

    def _pyqt5_save_dialog():
        """Open a native save-file dialog via PyQt5."""
        qt_app = QApplication.instance()
        if qt_app is None:
            # 本进程没有 Qt 主循环（直接运行模式），在当前线程创建即可
            QApplication(sys.argv)
            path, _ = QFileDialog.getSaveFileName(
                None, "Save Chart", "chart.png", "PNG Files (*.png)"
            )
            return (path,) if path else None

        # launcher 模式：QApplication 属于主线程，把对话框调度回主线程执行
        runner = _DialogRunner()
        runner.moveToThread(qt_app.thread())
        QMetaObject.invokeMethod(runner, "_run", Qt.BlockingQueuedConnection)
        return (runner.path,) if runner.path else None

    SAVE_DIALOG_FN = _pyqt5_save_dialog
except ImportError:
    # PyQt5 not available — save dialog will return "not supported"
    pass


# ────────────────────────────────────────────────────────────────────────────
# Flask application factory
# ────────────────────────────────────────────────────────────────────────────

from flask import Flask  # noqa: E402 (must be after common imports)

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False  # 让 jsonify 直接输出中文而非 \uXXXX 转义

# Wire up security (headers, CORS, CSRF, rate-limit config)
register_security(app)

# Register the save-dialog callback so chart_routes can use it
app.config["SAVE_DIALOG_FN"] = SAVE_DIALOG_FN

# Initialise the SQLite database schema
init_db()

# Register route blueprints
app.register_blueprint(api_bp)
app.register_blueprint(screen_bp)
app.register_blueprint(ml_bp)
app.register_blueprint(chart_bp)
app.register_blueprint(assets_bp)
app.register_blueprint(update_bp)


# ────────────────────────────────────────────────────────────────────────────
# 重模块惰性导出（PEP 562）
#
# fetcher / indicators / screening 携带 pandas、numpy、baostock（约 2~5s），
# 路由都是请求时才 ``from app import <名字>``，首次访问才真正导入；
# 启动器会在 Flask 就绪后用后台线程预热。
# ────────────────────────────────────────────────────────────────────────────
import importlib as _importlib

_LAZY_HEAVY_EXPORTS = {
    # common.fetcher
    'fetcher': 'common.fetcher',
    'StockDataFetcher': 'common.fetcher',
    'fetch_kline': 'common.fetcher',
    'fetch_realtime_tencent_to_db': 'common.fetcher',
    'random_browser_headers': 'common.fetcher',
    # common.indicators
    'prepare_chart_data': 'common.indicators',
    'calculate_ma': 'common.indicators',
    'calculate_macd': 'common.indicators',
    'calculate_rsi': 'common.indicators',
    'calculate_bollinger': 'common.indicators',
    'clean_nan_values': 'common.indicators',
    # common.screening
    'filter_stocks': 'common.screening',
    'compute_three_day_change': 'common.screening',
    'compute_volume_change_pct': 'common.screening',
    'compute_n_day_change': 'common.screening',
    'is_limit_up': 'common.screening',
    'compute_limit_up_pullback': 'common.screening',
    'compute_big_up_days_count': 'common.screening',
    'batch_compute_metrics': 'common.screening',
    'fetch_recent_trading_bars': 'common.screening',
    # common.preload（路由显式传入 preload_state）
    'background_preload': 'common.preload',
}


def __getattr__(name):
    """PEP 562：路由 ``from app import <重模块名字>`` 时按需导入。"""
    mod_name = _LAZY_HEAVY_EXPORTS.get(name)
    if mod_name is None:
        raise AttributeError(f"module 'app' has no attribute {name!r}")
    return getattr(_importlib.import_module(mod_name), name)


# ────────────────────────────────────────────────────────────────────────────
# Entry point
# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("  Stock Chart Web Service (Win7)")
    logger.info("  URL: http://127.0.0.1:5000")
    logger.info("=" * 60)
    app.run(host="127.0.0.1", port=5000, debug=False)
