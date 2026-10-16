#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Win11 各主题桌面壳共用的 WebView 启动器基础设施。

dark / minimalist / highend / brutalist 四个主题 launcher（以及
launcher_webview_sync.py）都复用本模块的统一启动流程：
随机找空闲端口 → 守护线程启动 Flask（app.py）→ 轮询健康检查就绪 →
打开 pywebview 窗口渲染本地页面。兼容 Python 3.10+。
"""
from __future__ import annotations

import ctypes
import logging
import os
import socket
import sys
import threading
import time
import traceback
import urllib.request

if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

os.chdir(BASE_DIR)
sys.path.insert(0, BASE_DIR)


def find_free_port():
    """Bind to an ephemeral port on 127.0.0.1 and return the port number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def start_flask_thread(port, template_subdir=None):
    """Import app, configure Flask folders, start Flask in a daemon thread.

    Blocks until Flask responds to a health-check, or raises RuntimeError
    after 10 seconds.
    """
    import app as app_module
    from common import updater

    flask_app = app_module.app

    # 自动更新的「界面覆盖层」优先：它比 EXE 里冻结的那份新时（几百 KB 的小更新），
    # 模板/静态目录直接指向 Documents 下的解包结果。
    template_sub = template_subdir or 'templates'
    if not updater.use_overlay(flask_app, template_sub):
        flask_app.template_folder = os.path.join(BASE_DIR, template_sub)
        flask_app.static_folder = os.path.join(BASE_DIR, 'static')
    flask_app.static_url_path = '/static'

    logging.getLogger('werkzeug').setLevel(logging.ERROR)

    threading.Thread(
        target=flask_app.run,
        kwargs=dict(host='127.0.0.1', port=port, debug=False, use_reloader=False),
        daemon=True,
    ).start()

    for _ in range(200):
        try:
            urllib.request.urlopen(
                'http://127.0.0.1:{}/'.format(port), timeout=1,
            )
            break
        except urllib.request.HTTPError:
            break  # 端口已在服务，页面自身的 4xx/5xx 交给应用报错，不该算启动失败
        except Exception:
            time.sleep(0.05)
    else:
        raise RuntimeError('Flask failed to start within 10 s')

    # 服务已就绪、主窗口即将出现：后台预热 pandas/numpy/baostock 等重模块
    # （app.py 已改为惰性导出，避免它们阻塞窗口出现；预热保证首次请求不卡）
    threading.Thread(target=warmup_heavy_modules, daemon=True).start()
    return flask_app


def warmup_heavy_modules():
    """Pre-import the heavy modules deferred by app.py (PEP 562 lazy exports)."""
    import importlib
    for name in ('common.fetcher', 'common.indicators', 'common.screening'):
        try:
            importlib.import_module(name)
        except Exception:
            pass  # nosec B110 - 预热失败不影响启动，首次请求时会再试


def close_splash():
    """关闭 PyInstaller onefile 启动画面。

    仅在带 --splash 打包的 EXE 里存在 pyi_splash 模块；直接运行/未启用
    splash 时静默跳过。
    """
    try:
        import pyi_splash  # noqa: F401 - PyInstaller 打包环境才有
        pyi_splash.close()
    except Exception:
        pass


def run_webview(title, port, width=1280, height=800, min_width=1024, min_height=600):
    """Create and show a pywebview window pointed at the local Flask server."""
    import webview

    close_splash()
    webview.create_window(
        title,
        url='http://127.0.0.1:{}/'.format(port),
        width=width,
        height=height,
        min_size=(min_width, min_height),
        text_select=True,
    )
    webview.start(debug=False)


def start_auto_update():
    """窗口出现后在后台跑自动更新：先收尾上次换体留下的残留，再检查并安装。

    任何失败都不影响使用——更新只是锦上添花。
    """
    try:
        from common import updater
        updater.cleanup_leftovers()
        updater.start_auto_update()
    except Exception:  # nosec B110 - 更新链路异常不阻塞启动
        pass


def launch(title, template_subdir=None):
    """One-call launcher: start Flask + open webview.

    Suitable for use as the ``main()`` body of every theme launcher.
    """
    port = find_free_port()
    start_flask_thread(port, template_subdir=template_subdir)
    start_auto_update()
    run_webview(title, port)


def die_with_message(exc):
    """Show a Windows error dialog and exit."""
    tb = traceback.format_exc()
    try:
        print('{}\n{}'.format(exc, tb))
    except Exception:
        pass
    ctypes.windll.user32.MessageBoxW(
        0, 'Error: {}\n\n{}'.format(exc, tb), 'Error', 0x10,
    )
    sys.exit(1)
