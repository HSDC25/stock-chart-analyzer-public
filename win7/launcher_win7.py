#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StockChart Windows 7 兼容启动器（PyQt5 + QWebEngineView 桌面壳）。

使用 PyQt5 内置的 Chromium 引擎渲染界面，不依赖系统 WebView2，
兼容 Win7/Win10/Win11。流程：随机端口起 Flask（app.py）→ 轮询就绪 →
打开 QWebEngineView 主窗口；窗口关闭时用启动时生成的随机 token
调用 /shutdown 优雅停掉 Flask。
"""

import os
import sys
import threading
import time
import socket
import logging
import secrets
import tempfile
import shutil
import traceback

# 在导入 Qt 之前设置环境，提升 Win7 兼容性与稳定性
# --no-sandbox: 某些 Win7 环境沙箱初始化会失败
# --disable-gpu: 避免旧显卡/GPU 驱动导致白屏/崩溃
os.environ.setdefault('QTWEBENGINE_DISABLE_SANDBOX', '1')
os.environ.setdefault(
    'QTWEBENGINE_CHROMIUM_FLAGS',
    '--no-sandbox --disable-gpu --disable-software-rasterizer --disable-logging'
)

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)


def _show_error_box(title, message):
    """在出错时尽量弹窗提示（兼容无 Qt 的情况）"""
    try:
        from PyQt5.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(None, title, message)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)
        except Exception:  # nosec B110
            pass


def find_free_port():
    """随机绑定 127.0.0.1 的一个空闲端口并返回端口号。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _shutdown_flask(port, token):
    """优雅停掉 Flask（需携带启动时生成的随机 token，防止外部页面 CSRF 关服）"""
    try:
        import urllib.request
        req = urllib.request.Request(
            f'http://127.0.0.1:{port}/shutdown',
            method='POST', data=b'',
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'X-Shutdown-Token': token,
            }
        )
        with urllib.request.urlopen(req, timeout=3) as resp:  # nosec B310
            resp.read()
    except Exception:  # nosec B110
        pass


def _prepare_qt_cache():
    """为 QtWebEngine 创建独立、可写的缓存目录，避免多实例/权限冲突"""
    try:
        cache_root = os.path.join(tempfile.gettempdir(), 'StockChart_QtWebEngine')
        # 使用进程 ID 后缀，避免多个实例互相覆盖
        cache_dir = os.path.join(cache_root, f'cache_{os.getpid()}')
        if os.path.exists(cache_dir):
            shutil.rmtree(cache_dir, ignore_errors=True)
        os.makedirs(cache_dir, exist_ok=True)

        from PyQt5.QtWebEngineWidgets import QWebEngineProfile
        profile = QWebEngineProfile.defaultProfile()
        profile.setCachePath(cache_dir)
        profile.setPersistentStoragePath(cache_dir)
        return cache_dir
    except Exception as e:
        logger.warning(f'设置 QtWebEngine 缓存目录失败: {e}')
        return None


def _apply_ui_overlay(app):
    """把模板/静态目录指向自动更新的「界面覆盖层」（比 EXE 内那份更新时才指向）。

    覆盖层落在 Documents 下（始终可写），所以小版本的前端更新只需要几百 KB，
    不用换 200MB 的 EXE；模板热重载打开后，页面刷新即可看到新界面。
    """
    try:
        from common import updater
        if updater.use_overlay(app):
            logger.info('界面覆盖层已启用: %s', app.template_folder)
            return True
    except Exception as exc:  # nosec B110 - 覆盖层不可用则用 EXE 内置界面
        logger.warning('界面覆盖层检查失败: %s', exc)
    return False


def start_auto_update():
    """窗口出现后在后台检查并安装更新，失败不影响使用。"""
    try:
        from common import updater
        updater.cleanup_leftovers()
        updater.start_auto_update()
    except Exception as exc:  # nosec B110
        logger.warning('自动更新启动失败: %s', exc)


def main():
    """启动入口：起 Flask → 等就绪 → 建 Qt 主窗口并进入事件循环。"""
    port = find_free_port()
    logger.info(f'Flask 端口: {port}')

    from app import app
    from flask import request, jsonify
    from common import security as _security
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

    _apply_ui_overlay(app)

    # /shutdown 仅限本机启动器调用：CSRF 豁免 + 随机 token 双重校验
    _shutdown_token = secrets.token_hex(16)
    _security.exempt_csrf_path('/shutdown')

    @app.route('/shutdown', methods=['POST'])
    def shutdown():
        if not secrets.compare_digest(
                request.headers.get('X-Shutdown-Token', ''), _shutdown_token):
            logger.warning('拒绝无 token 的 /shutdown 请求')
            return jsonify({'success': False, 'error': 'forbidden'}), 403
        func = request.environ.get('werkzeug.server.shutdown')
        if func:
            func()
        return 'OK'

    def run_flask():
        try:
            app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False)
        except Exception as e:
            logger.error(f'Flask error: {e}')

    t = threading.Thread(target=run_flask, daemon=True)
    t.start()

    # 等待 Flask 就绪
    for _ in range(100):
        try:
            import urllib.request
            urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=1)  # nosec B310
            logger.info('Flask 就绪')
            break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError('Flask 启动失败')

    # 服务就绪、窗口即将出现：后台预热 pandas/numpy/baostock（app.py 惰性导出）
    def _warmup():
        import importlib
        for _m in ('common.fetcher', 'common.indicators', 'common.screening'):
            try:
                importlib.import_module(_m)
            except Exception:  # nosec B110 - 预热失败不影响启动
                pass
    threading.Thread(target=_warmup, daemon=True).start()
    start_auto_update()

    url = f'http://127.0.0.1:{port}/'

    # ── PyQt5 + QWebEngineView 内置浏览器窗口 ──
    from PyQt5.QtCore import QUrl
    from PyQt5.QtWidgets import QApplication, QMainWindow
    from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEngineSettings

    qt_app = QApplication(sys.argv)

    # 在创建 QWebEngineView 前配置缓存路径
    _prepare_qt_cache()

    class MainWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle('股票图表分析工具')
            self.resize(1400, 900)
            self.setMinimumSize(1200, 700)

            self.web = QWebEngineView()
            self.web.setUrl(QUrl(url))

            # 启用完整 Web 功能
            settings = self.web.settings()
            settings.setAttribute(QWebEngineSettings.JavascriptEnabled, True)
            settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
            settings.setAttribute(QWebEngineSettings.PluginsEnabled, True)

            self.setCentralWidget(self.web)

        def closeEvent(self, event):
            logger.info('窗口关闭，停止 Flask...')
            _shutdown_flask(port, _shutdown_token)
            event.accept()

    window = MainWindow()
    window.show()

    exit_code = qt_app.exec_()
    _shutdown_flask(port, _shutdown_token)
    sys.exit(exit_code)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        tb = traceback.format_exc()
        logger.error(f'启动失败: {exc}\n{tb}')
        _show_error_box('启动失败', f'{exc}\n\n详细错误：\n{tb}')
        sys.exit(1)
