"""Shared Flask assembly for the win7 / win11 entry points.

两个变体的 ``app.py`` 曾经各抄一份"建 app → 注册安全钩子 → 注册蓝图 → 建库"
和一模一样的 ``_LAZY_HEAVY_EXPORTS`` / ``__getattr__``，改一处漏一处就会让
某个变体少注册蓝图或重模块退回顶层导入（启动多等 2~5 秒）。这里收成一份：
平台差异（保存对话框、旧版 preload 全局）留在各自的 ``app.py`` 里。

注意：PyInstaller 的 spec 用 hiddenimports 白名单收集 ``common.*``，新增本模块
时必须同步加进每个 spec（``tests/test_packaging.py`` 会守住这条）。
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Optional

from flask import Flask

BLUEPRINT_MODULES = (
    'api_routes',
    'screen_routes',
    'ml_routes',
    'chart_routes',
    'assets_routes',
    'update_routes',
)

#: 重模块（携带 pandas/numpy/baostock，约 2~5s）的惰性导出表。
#: 路由与启动器都是请求/使用时才 ``from app import <名字>``，这里只登记映射，
#: 首次访问才真正 import；启动器会在 Flask 就绪后用后台线程预热。
LAZY_HEAVY_EXPORTS = {
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


def ensure_project_root():
    # type: () -> Path
    """把仓库根（``win7/``、``win11/`` 的上一级）塞进 sys.path。

    启动器可能只把自己所在目录加进 sys.path，没有这一步 ``import common`` 会失败。
    """
    import sys

    root = Path(__file__).resolve().parents[1]
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    return root


def lazy_export(name):
    # type: (str) -> object
    """PEP 562 的 ``module.__getattr__`` 实现：按登记表按需导入重模块。"""
    mod_name = LAZY_HEAVY_EXPORTS.get(name)
    if mod_name is None:
        raise AttributeError('module {!r} has no attribute {!r}'.format('app', name))
    return getattr(importlib.import_module(mod_name), name)


def create_app(import_name, json_ascii=False, save_dialog_fn=None):
    # type: (str, bool, Optional[object]) -> Flask
    """组装 Flask 应用：安全钩子 → 保存对话框回调 → 建库 → 注册蓝图。

    *import_name* 传调用方的 ``__name__``：Flask 用它解析 root_path（模板 /
    静态文件的兜底位置），传错会让相对路径落空。
    ``register_security`` 负责 ``secret_key``、``MAX_CONTENT_LENGTH``、CSRF 与安全响应头。
    """
    from common.db import init_db
    from common.security import register_security

    app = Flask(import_name)
    if json_ascii is not None:
        app.config['JSON_AS_ASCII'] = json_ascii
    register_security(app)
    app.config['SAVE_DIALOG_FN'] = save_dialog_fn
    init_db()
    register_blueprints(app)
    return app


def register_blueprints(app):
    # type: (Flask) -> None
    for module_name in BLUEPRINT_MODULES:
        module = importlib.import_module('common.routes.' + module_name)
        app.register_blueprint(getattr(module, module_name[:-len('_routes')] + '_bp'))
