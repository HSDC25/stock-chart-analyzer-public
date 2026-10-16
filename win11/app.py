#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StockChart Flask 主应用入口（Win11 桌面壳用）。

本文件是对 ``common`` 包的薄封装：所有业务逻辑（路由、安全、数据库、
数据抓取）都在 common/ 中实现，这里只负责组装 Flask 应用，并把
launcher 与各路由蓝图延迟导入所需的名字（fetcher、preload_* 等）
重新导出到模块级别。桌面启动器（launcher_*.py）通过 ``import app``
取得 Flask 实例，在本地 127.0.0.1 起服务后交给 WebView2 窗口渲染。
"""

import os
import sys
import threading
from pathlib import Path
from typing import Optional

# Ensure the project root (parent of win11/) is on sys.path so that
# ``common`` is importable even when a launcher only adds its own
# directory (win11/) to sys.path.
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ---------------------------------------------------------------------------
# 1. Logging & urllib patch (must happen before any network / library code)
# ---------------------------------------------------------------------------
from common.config import configure_logging, install_urllib_patch  # noqa: E402

logger = configure_logging()
install_urllib_patch()

# ---------------------------------------------------------------------------
# 2. Flask application
# ---------------------------------------------------------------------------
from flask import Flask  # noqa: E402

app = Flask(__name__)
# 未设置环境变量时每次启动随机生成 secret_key：本地单用户场景可接受，
# 重启后旧会话失效，但避免把固定密钥硬编码进发布包
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(32))
# 限制请求体大小，防止异常大包拖垮本地服务
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB

# ---------------------------------------------------------------------------
# 3. Security hooks (headers, CORS, CSRF, MAX_CONTENT_LENGTH)
# ---------------------------------------------------------------------------
from common.security import register_security  # noqa: E402

register_security(app)

# ---------------------------------------------------------------------------
# 4. Register blueprints
# ---------------------------------------------------------------------------
from common.routes.api_routes import api_bp  # noqa: E402
from common.routes.screen_routes import screen_bp  # noqa: E402
from common.routes.ml_routes import ml_bp  # noqa: E402
from common.routes.chart_routes import chart_bp  # noqa: E402
from common.routes.assets_routes import assets_bp  # noqa: E402
from common.routes.update_routes import update_bp  # noqa: E402

app.register_blueprint(api_bp)
app.register_blueprint(screen_bp)
app.register_blueprint(ml_bp)
app.register_blueprint(chart_bp)
app.register_blueprint(assets_bp)
app.register_blueprint(update_bp)

# ---------------------------------------------------------------------------
# 5. Database initialisation
# ---------------------------------------------------------------------------
from common.db import init_db  # noqa: E402

init_db()

# ---------------------------------------------------------------------------
# 6. Fetcher singleton
#
#    pandas / numpy / baostock 都随 common.fetcher 加载，约占启动黑箱期
#    2~5 秒。路由全部在请求时才 ``from app import fetcher``，因此这里不再
#    顶层导入，改由文件末尾的模块级 __getattr__（PEP 562）惰性提供；
#    启动器会在 Flask 就绪后用后台线程预热（launcher_base.warmup_heavy_modules）。
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 7. Preload infrastructure
#
#    api_routes.py does deferred imports of these module-level names:
#      _preload_thread, _preload_stop_event, _preload_lock,
#      _preload_progress, background_preload
#    We keep them as plain globals for backward compatibility AND expose
#    the newer PreloadState class for forward migration.
# ---------------------------------------------------------------------------
from common.preload import (  # noqa: E402
    PreloadState,
    background_preload,
    set_thread_low_priority,
    load_preload_state,
    save_preload_state,
    clear_preload_state,
)

# Module-level preload state object (new API)
preload_state = PreloadState()

# Legacy globals expected by api_routes.py deferred imports.
# These are thin proxies so existing route code keeps working unchanged.
_preload_thread: Optional[threading.Thread] = None
_preload_stop_event = preload_state._stop_event   # same Event object
_preload_lock = preload_state._lock                # same Lock object
_preload_progress = preload_state._progress        # same dict object

# ---------------------------------------------------------------------------
# 8. Indicators
#    prepare_chart_data 同样走惰性导出（见文件末尾 __getattr__）。
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 9. Save-dialog callback for chart_routes (pywebview integration)
#
#    chart_routes reads ``current_app.config['SAVE_DIALOG_FN']``.
#    Launchers that use pywebview should override this after import.
#    We set a default of None so the app works without pywebview.
# ---------------------------------------------------------------------------
app.config["SAVE_DIALOG_FN"] = None

try:
    import webview  # type: ignore[import-untyped]

    def _default_save_dialog():
        """Default save dialog using pywebview's native file dialog."""
        try:
            if webview.windows:
                return webview.windows[0].create_file_dialog(
                    webview.SAVE_DIALOG,
                    directory="",
                    save_filename="chart.png",
                    file_types=("PNG Images (*.png)", "All Files (*.*)"),
                )
        except Exception as exc:
            logger.debug("save dialog failed: %s", exc)
        return None

    app.config["SAVE_DIALOG_FN"] = _default_save_dialog
except ImportError:
    pass  # pywebview not available (testing / headless)

# ---------------------------------------------------------------------------
# 10. Re-exports for launcher backward compatibility
#
#     Launchers do ``import app`` then access ``app.app``, which already
#     works because the Flask instance is named ``app`` in this module.
#     Some launchers also reference other names — make sure they're here.
# ---------------------------------------------------------------------------
from common.config import (  # noqa: E402, F401
    DATA_DIR,
    DB_PATH,
    PRELOAD_STATE_FILE,
    SKIP_SSL_VERIFY,
    SSL_CONTEXT,
)
from common.models import (  # noqa: E402, F401
    is_allowed_stock_code,
    is_stock_st,
    validate_stock_code_format,
    FilterCriteria,
)
# common.screening 的导出已移至文件末尾惰性导出区（pandas 随其加载）
from common.trading_calendar import (  # noqa: E402, F401
    A_SHARE_HOLIDAYS_2026,
    is_trading_day,
    is_market_open_now,
    trading_day_fetch_range,
)

# ---------------------------------------------------------------------------
# 11. 重模块惰性导出（PEP 562）
#
# fetcher / indicators / screening 三个模块携带 pandas、numpy、baostock，
# 约占启动黑箱期 2~5 秒。路由都是请求时才 ``from app import <名字>``，
# 这里只挂模块级 __getattr__，首次访问才真正导入；
# 启动器会在 Flask 就绪后用后台线程预热（launcher_base.warmup_heavy_modules）。
# ---------------------------------------------------------------------------
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
}


def __getattr__(name):
    """PEP 562：路由 ``from app import <重模块名字>`` 时按需导入。"""
    mod_name = _LAZY_HEAVY_EXPORTS.get(name)
    if mod_name is None:
        raise AttributeError(f"module 'app' has no attribute {name!r}")
    return getattr(_importlib.import_module(mod_name), name)


logger.info("app.py thin wrapper initialised successfully")
