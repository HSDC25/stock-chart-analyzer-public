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
# 2~5. Flask 应用、安全钩子、蓝图注册、建库
#
#    统一由 common.app_factory.create_app 完成（与 win7 版共用同一份），
#     顺序是：安全钩子 → 保存对话框回调 → 建库 → 注册蓝图。
#     本文件末尾才调用，因为保存对话框要先定义好。
# ---------------------------------------------------------------------------
from common.app_factory import create_app, lazy_export  # noqa: E402

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
#    Default is None so the app works without pywebview.
# ---------------------------------------------------------------------------
_save_dialog_fn = None

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

    _save_dialog_fn = _default_save_dialog
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
# 导出表见 common/app_factory.py 的 LAZY_HEAVY_EXPORTS。
# ---------------------------------------------------------------------------

def __getattr__(name):
    """PEP 562：路由 ``from app import <重模块名字>`` 时按需导入。"""
    return lazy_export(name)


# ---------------------------------------------------------------------------
# 12. Flask application（安全钩子 → 保存对话框 → 建库 → 蓝图，全在 factory 内）
# ---------------------------------------------------------------------------
app = create_app(__name__, save_dialog_fn=_save_dialog_fn)

logger.info("app.py thin wrapper initialised successfully")
