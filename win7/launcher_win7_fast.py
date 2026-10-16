#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Win7 快速启动器 - 服务器下载模式（浏览器外壳）。

先从云端下载数据库（多源轮换 + SHA-256 完整性校验 + 备份回滚），
再本地起 Flask，并优先调用 Supermium（Win7 专用 Chromium 浏览器）
等 Chromium 内核浏览器打开页面；找不到时退回系统默认浏览器。
与 launcher_win7.py 的区别：不内嵌 Qt，体积更小、启动更快。
"""

import os
import sys
import json
import threading
import time
import socket
import ctypes
import subprocess  # nosec B404
import urllib.request
import ssl
import sqlite3

# Server endpoints
# 主源走 HTTPS；回退源目前是明文 HTTP，仅作主源不可达时的兜底，
# 配合 download_db 中的 SHA-256 校验降低被篡改风险。
# 部署侧应尽快为回退源启用 HTTPS 并发布 stock_data.db.sha256。
# 备用源不再硬编码，改由环境变量提供；为空时自动剔除（仅保留主源）
DB_URLS = [u for u in (
    os.environ.get('STOCKCHART_DB_URL_PRIMARY', 'https://data.hsdc.cloud/stock_data.db'),
    os.environ.get('STOCKCHART_DB_URL_FALLBACK', ''),
) if u]
STATE_URLS = [u for u in (
    os.environ.get('STOCKCHART_STATE_URL_PRIMARY', 'https://data.hsdc.cloud/fetch_state.json'),
    os.environ.get('STOCKCHART_STATE_URL_FALLBACK', ''),
) if u]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
sys.path.insert(0, BASE_DIR)

DATA_DIR = os.path.join(os.path.expanduser('~'), 'Documents', 'StockChart')
LOCAL_DB = os.path.join(DATA_DIR, 'stock_data.db')

LOG_FILE = os.path.join(BASE_DIR, 'stockchart_fast.log')

# Supermium + Chromium-based browsers (priority order)
# 常见安装目录逐个探测；环境变量缺失时回退到默认安装位置
_PF = os.environ.get('ProgramFiles', 'C:/Program Files')
_PF86 = os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')
_LAD = os.environ.get('LOCALAPPDATA', os.path.join(os.path.expanduser('~'), 'AppData', 'Local'))
BROWSER_PATHS = [
    # Supermium (Win7 Chromium)
    os.path.join(_PF, 'Supermium', 'Supermium.exe'),
    os.path.join(_PF86, 'Supermium', 'Supermium.exe'),
    os.path.join(_LAD, 'Supermium', 'Application', 'Supermium.exe'),
    # Chromium (open source)
    os.path.join(_PF, 'Chromium', 'chrome.exe'),
    os.path.join(_PF86, 'Chromium', 'chrome.exe'),
    os.path.join(_LAD, 'Chromium', 'Application', 'chrome.exe'),
    # Brave
    os.path.join(_PF, 'BraveSoftware', 'Brave-Browser', 'Application', 'brave.exe'),
    os.path.join(_PF86, 'BraveSoftware', 'Brave-Browser', 'Application', 'brave.exe'),
    os.path.join(_LAD, 'BraveSoftware', 'Brave-Browser', 'Application', 'brave.exe'),
    # Chrome
    os.path.join(_PF, 'Google', 'Chrome', 'Application', 'chrome.exe'),
    os.path.join(_PF86, 'Google', 'Chrome', 'Application', 'chrome.exe'),
    os.path.join(_LAD, 'Google', 'Chrome', 'Application', 'chrome.exe'),
    # Vivaldi
    os.path.join(_PF, 'Vivaldi', 'Application', 'vivaldi.exe'),
    os.path.join(_PF86, 'Vivaldi', 'Application', 'vivaldi.exe'),
    os.path.join(_LAD, 'Vivaldi', 'Application', 'vivaldi.exe'),
    # Opera
    os.path.join(_PF, 'Opera', 'launcher.exe'),
    os.path.join(_LAD, 'Programs', 'Opera', 'launcher.exe'),
    # Firefox
    os.path.join(_PF, 'Mozilla Firefox', 'firefox.exe'),
    os.path.join(_PF86, 'Mozilla Firefox', 'firefox.exe'),
    # Edge (Win10+)
    os.path.join(_PF86, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
    os.path.join(_PF, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
    # Portable / custom locations
    os.path.join(os.path.expanduser('~'), 'Desktop', 'Supermium', 'Supermium.exe'),
    os.path.join(os.path.expanduser('~'), 'Supermium', 'Supermium.exe'),
    'D:/Program Files/Supermium/Supermium.exe',
    'D:/Supermium/Supermium.exe',
    'E:/Program Files/Supermium/Supermium.exe',
    'E:/Supermium/Supermium.exe',
]


def log(msg):
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(msg + '\n')
    except Exception:  # nosec B110
        pass
    print(msg)


def find_browser():
    """查找浏览器 - 优先 Supermium / Chromium 内核"""
    # 1. 检查预定义路径
    for path in BROWSER_PATHS:
        expanded = os.path.expandvars(path)
        if os.path.isfile(expanded):
            log(f'[Browser] Found: {expanded}')
            return expanded

    # 2. 注册表查找 Supermium / Chrome / Brave / Chromium
    try:
        import winreg
        reg_paths = [
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Supermium.exe'),
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe'),
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\brave.exe'),
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe'),
            (winreg.HKEY_CURRENT_USER, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Supermium.exe'),
            (winreg.HKEY_CURRENT_USER, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe'),
        ]
        for root, path in reg_paths:
            try:
                key = winreg.OpenKey(root, path)
                exe_path, _ = winreg.QueryValueEx(key, '')
                winreg.CloseKey(key)
                if exe_path and os.path.isfile(exe_path):
                    log(f'[Browser] Registry: {exe_path}')
                    return exe_path
            except Exception:  # nosec B110
                pass
    except ImportError:
        pass

    # 3. 扫描开始菜单快捷方式（快速版）
    try:
        import glob
        start_menu_paths = [
            os.path.expandvars(r'%APPDATA%\Microsoft\Windows\Start Menu\Programs'),
            os.path.expandvars(r'%ProgramData%\Microsoft\Windows\Start Menu\Programs'),
        ]
        for sm_path in start_menu_paths:
            if os.path.isdir(sm_path):
                for lnk in glob.glob(os.path.join(sm_path, '**', '*.supermium*'), recursive=True):
                    # .lnk 是快捷方式，把扩展名换成 .exe 的目标路径不一定存在；
                    # 必须校验存在性，否则 Popen 会 FileNotFoundError 直接崩
                    candidate = lnk.replace('.lnk', '.exe')
                    if os.path.isfile(candidate):
                        log(f'[Browser] StartMenu: {candidate}')
                        return candidate
                    log(f'[Browser] StartMenu shortcut target missing: {candidate}')
    except Exception:  # nosec B110
        pass

    log('[Browser] No Chromium browser found')
    return None


def download_db(progress=None):
    """Download DB from server

    *progress* 为可选回调 ``progress(pct, msg)``（pct 为 0~100 的浮点数，
    None 表示不确定进度），供启动器把下载进度展示到浏览器引导页。

    完整性校验分两层：
    1. 若服务器提供 ``<url>.sha256``，下载后必须匹配，否则拒绝落库；
    2. 无论有无摘要文件，都要求能通过 SQLite 打开并查到两张表。
    替换本地库采用"备份 → 替换 → 失败回滚"流程，避免把可用旧库弄丢。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    import hashlib

    def _fetch_remote_sha256(base_url):
        """获取服务器端 SHA-256 摘要；拿不到返回 None（降级只做结构校验）"""
        try:
            req = urllib.request.Request(base_url + '.sha256',
                                         headers={'User-Agent': 'StockChart/1.0'})
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:  # nosec B310 - URL 为硬编码可信数据源，主源经 HTTPS + 证书校验
                text = resp.read(128).decode('ascii', 'ignore').strip()
            return text.split()[0].lower() if text else None
        except Exception:
            return None

    for url in DB_URLS:
        # 多源轮换：按顺序尝试主源/回退源，任一成功即返回
        tmp = None
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'StockChart/1.0'})
            with urllib.request.urlopen(req, timeout=300, context=ctx) as resp:  # nosec B310 - URL 为硬编码可信数据源，主源经 HTTPS + 证书校验
                import tempfile
                total = int(resp.headers.get('Content-Length') or 0)
                fd, tmp = tempfile.mkstemp(prefix='stockchart_', suffix='.db.tmp', dir=os.path.dirname(LOCAL_DB))
                os.close(fd)
                downloaded = 0
                last_pct = -1
                with open(tmp, 'wb') as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress and total > 0:
                            pct = min(100.0, downloaded / total * 100)
                            # 每 1% 上报一次，避免回调风暴
                            if int(pct) > last_pct:
                                last_pct = int(pct)
                                progress(pct, '正在下载数据库… %.0f%% (%.1f/%.1f MB)'
                                         % (pct, downloaded / 1048576, total / 1048576))

            if progress:
                progress(None, '校验数据完整性…')
            # SHA-256 完整性校验：服务器发布了摘要就必须匹配
            expected_sha = _fetch_remote_sha256(url)
            if expected_sha:
                h = hashlib.sha256()
                with open(tmp, 'rb') as f:
                    for chunk in iter(lambda: f.read(65536), b''):
                        h.update(chunk)
                if h.hexdigest() != expected_sha:
                    raise ValueError(
                        f'SHA-256 mismatch: expected {expected_sha}, got {h.hexdigest()}')
                log('[DB] SHA-256 checksum OK')
            else:
                log('[DB] WARN: no .sha256 published by server, skipping checksum')

            # 验证 SQLite 结构
            conn = sqlite3.connect(tmp, timeout=5)
            try:
                cur = conn.cursor()
                cur.execute('SELECT COUNT(*) FROM kline_data')
                klines = cur.fetchone()[0]
                cur.execute('SELECT COUNT(*) FROM stock_metadata')
                stocks = cur.fetchone()[0]
            finally:
                conn.close()
            log(f'[DB] Downloaded: {klines} klines, {stocks} stocks')

            # 备份旧库 → 原子性尽量好的替换 → 失败回滚
            backup = None
            if os.path.exists(LOCAL_DB):
                backup = LOCAL_DB + '.bak'
                if os.path.exists(backup):
                    os.remove(backup)
                os.rename(LOCAL_DB, backup)
            try:
                os.rename(tmp, LOCAL_DB)
                tmp = None  # 已移动，except 分支不再删除
            except OSError:
                if backup and os.path.exists(backup) and not os.path.exists(LOCAL_DB):
                    os.rename(backup, LOCAL_DB)
                    log('[DB] Replace failed, restored backup')
                raise
            return True
        except Exception as e:
            log(f'[DB] Download failed: {e}')
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return False


def check_need_download():
    """Check if download needed"""
    if not os.path.exists(LOCAL_DB):
        return True
    try:
        conn = sqlite3.connect(LOCAL_DB, timeout=5)
        cur = conn.cursor()
        cur.execute('SELECT MAX(date) FROM kline_data')
        local_date = cur.fetchone()[0] or ''
        conn.close()
    except Exception:
        return True

    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    for url in STATE_URLS:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'StockChart/1.0'})
              # nosec B310
            with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:  # nosec B310
                state = json.loads(resp.read().decode('utf-8'))
                trading_days = state.get('trading_days', [])
                server_date = trading_days[-1] if trading_days else ''
                if server_date and local_date < server_date:
                    return True
                return False
        except Exception:  # nosec B112
            continue
    return False


def find_free_port():
    """随机绑定 127.0.0.1 的一个空闲端口并返回端口号。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


# 引导页：浏览器先打开此页轮询启动进度，数据就绪后自动跳转主界面。
# 解决"下载 31MB 期间用户面对黑箱"的问题（win7 运行时无 tkinter，无法弹进度窗）。
_BOOT_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>StockChart 启动中</title>
<style>
 body{margin:0;height:100vh;display:flex;flex-direction:column;justify-content:center;
      align-items:center;background:#0d1117;color:#e6edf3;
      font-family:'Microsoft YaHei',sans-serif}
 .bar-wrap{width:340px;height:10px;background:#21262d;border-radius:5px;margin:18px 0 10px;overflow:hidden}
 .bar{height:100%;width:0%;background:#14b143;border-radius:5px;transition:width .4s}
 #msg{color:#8b949e;font-size:14px;max-width:420px;text-align:center;line-height:1.6;white-space:pre-line}
 .err{color:#ef232a}
</style></head><body>
 <h2 style="margin:0 0 4px">StockChart 正在启动…</h2>
 <div id="phase">初始化</div>
 <div class="bar-wrap"><div class="bar" id="bar"></div></div>
 <div id="msg">正在连接本地服务…</div>
<script>
 function poll() {
  fetch('/api/boot/status', {cache: 'no-store'}).then(function(r) { return r.json(); }).then(function(s) {
   document.getElementById('phase').textContent = s.phase_text;
   document.getElementById('msg').textContent = s.message;
   document.getElementById('msg').className = s.phase === 'failed' ? 'err' : '';
   if (s.progress != null) {
    document.getElementById('bar').style.width = s.progress + '%';
   }
   if (s.phase === 'ready') { location.replace('/'); return; }
   if (s.phase === 'failed') { return; }
   setTimeout(poll, 800);
  }).catch(function() { setTimeout(poll, 1500); });
 }
 poll();
</script></body></html>"""

# 共享启动状态：由下载线程写、/api/boot/status 读
_boot_state = {'phase': 'checking', 'phase_text': '检查数据',
               'progress': None, 'message': '正在检查本地数据…'}


def _register_boot_routes(flask_app):
    """注册启动引导页与进度查询接口（GET 请求，CSRF 天然豁免）"""
    from flask import jsonify as _json, Response

    @flask_app.route('/_boot')
    def _boot_page():
        return Response(_BOOT_PAGE_HTML, mimetype='text/html')

    @flask_app.route('/api/boot/status')
    def _boot_status():
        return _json(dict(_boot_state))


def main():
    """启动入口：起 Flask → 浏览器打开引导页 → 后台下载 → 就绪后自动跳转。"""
    with open(LOG_FILE, 'w', encoding='utf-8') as f:
        f.write('=== Fast Launcher ===\n')

    # Step 1: Start Flask（先起服务，让浏览器能立刻打开引导页）
    log('Starting Flask...')
    os.environ['STOCKCHART_SYNC_MODE'] = '1'

    import app
    from flask import jsonify as _json

    @app.app.route('/api/preload', methods=['POST'])
    def _preg(): return _json({'success': False, 'message': 'Server mode'})
    @app.app.route('/api/preload/stop', methods=['POST'])
    def _pstop(): return _json({'success': True})
    @app.app.route('/api/preload/status')
    def _pstatus(): return _json({'success': True, 'running': False})

    _register_boot_routes(app.app)

    import logging
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

    port = find_free_port()
    log(f'Port: {port}')

    flask_app = app.app

    try:
        from common import updater
        if updater.use_overlay(flask_app):
            log(f'UI overlay active: {flask_app.template_folder}')
    except Exception as exc:  # nosec B110 - 覆盖层不可用则用 EXE 内置界面
        log(f'UI overlay check skipped: {exc}')

    def run_flask():
        flask_app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False)
    threading.Thread(target=run_flask, daemon=True).start()

    import urllib.request as _ur
    for _ in range(200):
        try:
            _ur.urlopen(f'http://127.0.0.1:{port}/_boot', timeout=1)  # nosec B310
            log('Flask ready')
            break
        except Exception:
            time.sleep(0.05)
    else:
        log('Flask failed')
        ctypes.windll.user32.MessageBoxW(0, '本地服务启动失败，请重新运行程序。',
                                         'StockChart', 0x10)
        return

    # 服务就绪：后台跑自动更新（收尾上次换体残留 → 检查 → 下载 → 换皮/换体重启）
    try:
        from common import updater
        updater.cleanup_leftovers()
        updater.start_auto_update()
    except Exception as exc:  # nosec B110 - 更新失败不影响使用
        log(f'auto update skipped: {exc}')

    # 服务就绪：后台预热 pandas/numpy/baostock（app.py 惰性导出），
    # 引导页展示期间正好完成，用户首次查询不卡
    def _warmup():
        import importlib
        for _m in ('common.fetcher', 'common.indicators', 'common.screening'):
            try:
                importlib.import_module(_m)
            except Exception:  # nosec B110 - 预热失败不影响启动
                pass
    threading.Thread(target=_warmup, daemon=True).start()

    url = f'http://127.0.0.1:{port}/'
    boot_url = f'http://127.0.0.1:{port}/_boot'

    # Step 2: 打开浏览器到引导页（数据下载期间用户能看到实时进度）
    browser = find_browser()
    if browser:
        name = os.path.basename(browser).lower()
        cmd = [browser]
        if 'supermium' in name or 'chrome' in name or 'chromium' in name or 'brave' in name or 'vivaldi' in name or 'msedge' in name:
            cmd += ['--no-sandbox', '--disable-gpu', '--disable-gpu-sandbox', '--disable-software-rasterizer']
        cmd.append(boot_url)
        log(f'Opening: {" ".join(cmd)}')
        subprocess.Popen(cmd)  # nosec B603 - 命令参数来自受信任的浏览器路径
    else:
        log('WARNING: No Chromium browser found, falling back to IE')
        import webbrowser
        ctypes.windll.user32.MessageBoxW(0,
            '未找到 Chromium 内核浏览器！\n\n'
            '建议安装 Supermium 或 Chrome 以获得最佳体验。',
            '浏览器提示', 0x30)
        webbrowser.open(boot_url, new=1, autoraise=True)

    # Step 3: 后台准备数据（引导页通过 /api/boot/status 展示进度）
    def prepare_data():
        try:
            if check_need_download():
                _boot_state.update(phase='downloading', phase_text='下载数据',
                                   progress=0, message='正在连接服务器…')
                log('Downloading DB from server...')

                def _progress(pct, msg):
                    _boot_state.update(progress=pct, message=msg)

                if not download_db(progress=_progress):
                    log('Download failed!')
                    if not os.path.exists(LOCAL_DB):
                        _boot_state.update(
                            phase='failed', progress=None,
                            message='下载数据库失败，且本地没有数据文件。\n'
                                    '请检查网络后重新运行程序。')
                        return
                    _boot_state.update(message='下载失败，已改用本地旧数据')
                else:
                    _boot_state.update(progress=100, message='数据下载完成')
            else:
                log('DB up to date')
            _boot_state.update(phase='ready', phase_text='准备就绪',
                               progress=100, message='启动完成，正在进入主界面…')
        except Exception as exc:  # nosec B110 - 引导页兜底展示，不让线程静默死亡
            log(f'Prepare failed: {exc}')
            _boot_state.update(phase='failed', progress=None,
                               message=f'启动异常: {exc}')

    prep = threading.Thread(target=prepare_data, daemon=True)
    prep.start()
    prep.join()  # 引导页此时正在展示进度；就绪后页面自动跳转主界面

    if _boot_state['phase'] == 'failed' and not os.path.exists(LOCAL_DB):
        return

    # Keep running（win7 运行时无 tkinter/托盘，用弹窗驻留；点确定退出程序）
    ctypes.windll.user32.MessageBoxW(0,
        f'StockChart 已在运行。\n\n地址: {url}\n\n'
        f'正常使用期间请保持此窗口。\n点击"确定"退出 StockChart。',
        'StockChart', 0x40)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        log(f'Error: {e}')
        ctypes.windll.user32.MessageBoxW(0, f'Error: {e}', 'Error', 0x10)
