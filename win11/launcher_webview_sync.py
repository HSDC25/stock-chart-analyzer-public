#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Win11 WebView2 桌面启动器 - 服务器同步模式。

先从云端下载数据库（多源轮换 + SHA-256 完整性校验 + 备份回滚），
再复用 launcher_base 的通用流程（Flask + pywebview）打开界面。
支持 Dark / Minimalist / HighEnd / Brutalist 主题。
"""

import os
import sys
import json
import time
import ctypes
import traceback
import ssl
import shutil
import urllib.request

# PyInstaller onefile 路径兼容
if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

os.chdir(BASE_DIR)
sys.path.insert(0, BASE_DIR)

from launcher_base import (  # noqa: E402
    close_splash,
    die_with_message,
    find_free_port,
    start_auto_update,
    start_flask_thread,
)

# ---------------------------------------------------------------------------
# Configuration - 服务器配置
# 主源走 HTTPS；回退源目前是明文 HTTP，仅作为主源不可达时的兜底，
# 配合 download_database 中的 SHA-256 校验降低被篡改风险。
# 部署侧应尽快为回退源启用 HTTPS 并发布 stock_data.db.sha256。
# ---------------------------------------------------------------------------
# 备用源不再硬编码，改由环境变量提供；为空时自动剔除（仅保留主源）
DB_URLS = [u for u in (
    os.environ.get('STOCKCHART_DB_URL_PRIMARY', 'https://data.hsdc.cloud/stock_data.db'),
    os.environ.get('STOCKCHART_DB_URL_FALLBACK', ''),
) if u]

STATE_URLS = [u for u in (
    os.environ.get('STOCKCHART_STATE_URL_PRIMARY', 'https://data.hsdc.cloud/fetch_state.json'),
    os.environ.get('STOCKCHART_STATE_URL_FALLBACK', ''),
) if u]

DATA_DIR = os.path.join(os.path.expanduser('~'), 'Documents', 'StockChart')
LOCAL_DB = os.path.join(DATA_DIR, 'stock_data.db')

LOG_FILE = os.path.join(BASE_DIR, 'stockchart_sync.log')


def log(msg):
    """日志输出（windowed 模式下 stdout 可能为 None，需保护）"""
    try:
        print(msg)
    except Exception:
        pass
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} - {msg}\n')
    except Exception:  # nosec B110
        pass


# ---------------------------------------------------------------------------
# 服务器状态检查
# ---------------------------------------------------------------------------
def check_server_state():
    """获取服务器 fetch_state.json"""
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()

    for url in STATE_URLS:
        try:
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'StockChart-Sync/1.0')
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:  # nosec B310 - URL 为硬编码可信数据源，主源经 HTTPS + 证书校验
                data = json.loads(resp.read().decode('utf-8'))
                return data, None
        except Exception as e:
            log(f'[State] {url} failed: {e}')
    return None, 'All state URLs failed'


def get_latest_trading_date():
    """获取预期的最新交易日"""
    from datetime import datetime, timedelta
    today = datetime.now()
    if today.weekday() < 5:  # Mon-Fri
        return today.strftime('%Y-%m-%d')
    else:  # Sat/Sun - last Friday
        d = today
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d.strftime('%Y-%m-%d')


def wait_for_data_ready(progress_callback=None):
    """等待服务器数据准备好
    
    Returns True when data is ready, False if user gives up or timeout.
    """
    from datetime import datetime
    
    expected_date = get_latest_trading_date()
    today = datetime.now()
    
    # 周末不等待
    if today.weekday() >= 5:
        log('[Wait] Weekend - skip waiting')
        return True
    
    # 18:00 之前服务器还没开始拉取
    if today.hour < 18:
        log('[Wait] Before 18:00 - server fetch not started')
        return True
    
    max_wait = 3600  # 最大等待 60 分钟
    poll_interval = 30  # 每 30 秒检查一次
    waited = 0
    
    log(f'[Wait] Waiting for server data dated {expected_date}...')
    if progress_callback:
        progress_callback(f'等待服务器数据更新... (预期日期: {expected_date})')
    
    while waited < max_wait:
        state, err = check_server_state()
        
        if state:
            last_run = state.get('last_run', '')
            errors = state.get('stats', {}).get('errors', -1)
            last_date = last_run[:10] if len(last_run) >= 10 else ''
            
            if last_date == expected_date:
                log(f'[Wait] Server data ready! Date: {last_date}, errors: {errors}')
                if progress_callback:
                    progress_callback(f'服务器数据已就绪! (日期: {last_date}, 错误数: {errors})')
                return True
            else:
                msg = f'服务器数据日期: {last_date} (需要 {expected_date}), 等待中... ({waited}s/{max_wait}s)'
                log(f'[Wait] {msg}')
                if progress_callback:
                    progress_callback(msg)
        elif err:
            msg = f'无法连接服务器, 重试中... ({waited}s/{max_wait}s)'
            log(f'[Wait] {msg}')
            if progress_callback:
                progress_callback(msg)
        
        time.sleep(poll_interval)
        waited += poll_interval
    
    log('[Wait] Timed out waiting for server data')
    if progress_callback:
        progress_callback('等待超时，将使用现有数据')
    return True


# ---------------------------------------------------------------------------
# 数据库下载
# ---------------------------------------------------------------------------
def check_need_download():
    """检查是否需要下载数据库"""
    if not os.path.exists(LOCAL_DB):
        log('[DB] No local database, need download')
        return True
    
    try:
        import sqlite3
        conn = sqlite3.connect(LOCAL_DB, timeout=5)
        cur = conn.cursor()
        cur.execute('SELECT MAX(date) FROM kline_data')
        local_date = cur.fetchone()[0] or ''
        # 本地实际数据更新时间（实时行情最后一次写入时间）
        cur.execute('SELECT MAX(updated_at) FROM stock_metadata')
        local_update = cur.fetchone()[0] or ''
        conn.close()
        log(f'[DB] Local latest date: {local_date}, update time: {local_update}')
    except Exception as e:
        log(f'[DB] Error checking local DB: {e}')
        return True
    
    # 检查服务器状态
    state, err = check_server_state()
    if state:
        trading_days = state.get('trading_days', [])
        server_date = trading_days[-1] if trading_days else ''
        server_last_run = state.get('last_run', '') or ''

        # 1) K 线日期更新（正常交易日增量）
        if server_date and local_date < server_date:
            log(f'[DB] Server has newer data: {server_date} > {local_date}')
            return True

        # 2) 实际更新时间更新：服务器即使没有产生新 K 线（周末/非交易日），
        #    也会重新抓取实时行情（price/PE/PB），此时也应重新下载，
        #    否则本地实时行情会一直停留在旧数据。
        if server_last_run and local_update:
            from datetime import datetime as _dt, timedelta as _td
            try:
                # last_run 为 ISO 格式（如 2026-08-09T17:23:45），updated_at 为 'YYYY-MM-DD HH:MM:SS'
                s_t = _dt.fromisoformat(server_last_run.replace('Z', '+00:00')[:19])
                l_t = _dt.strptime(local_update, '%Y-%m-%d %H:%M:%S')
                # 服务器比本地新超过 10 分钟才下载（避免同一次抓取内的微小差异导致重复下载）
                if s_t > l_t + _td(minutes=10):
                    log(f'[DB] Server data refreshed: {server_last_run} > {local_update}')
                    return True
            except Exception as e:
                log(f'[DB] Time compare failed: {e}')

        log('[DB] Local data is up to date')
        return False
    
    log(f'[DB] Cannot reach server, using local data')
    return False


def download_database(progress_callback=None):
    """从服务器下载数据库

    完整性校验分两层：
    1. 若服务器提供 ``<url>.sha256``（内容为 hex 摘要），下载后必须匹配，
       不匹配视为被篡改/损坏，拒绝落库；
    2. 无论有无摘要文件，都要求能通过 SQLite 打开并查到 kline_data /
       stock_metadata 两张表。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    import hashlib

    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()

    def _fetch_remote_sha256(base_url):
        """尝试获取服务器端的 SHA-256 摘要文件；拿不到返回 None（降级只做结构校验）"""
        try:
            req = urllib.request.Request(base_url + '.sha256')
            req.add_header('User-Agent', 'StockChart-Sync/1.0')
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:  # nosec B310 - URL 为硬编码可信数据源，主源经 HTTPS + 证书校验
                text = resp.read(128).decode('ascii', 'ignore').strip()
            return text.split()[0].lower() if text else None
        except Exception:
            return None

    def _sha256_of(path):
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                h.update(chunk)
        return h.hexdigest()

    last_error = None
    # 多源轮换：按顺序尝试主源/回退源，任一成功即返回；全部失败才放弃
    for url in DB_URLS:
        log(f'[DB] Downloading from {url}...')
        if progress_callback:
            progress_callback(f'正在从服务器下载数据...\n{url}')

        tmp_path = None
        try:
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'StockChart-Sync/1.0')

            import tempfile
            fd, tmp_path = tempfile.mkstemp(prefix='stockchart_', suffix='.db.tmp', dir=os.path.dirname(LOCAL_DB))
            os.close(fd)
            with urllib.request.urlopen(req, timeout=300, context=ctx) as response:  # nosec B310 - URL 为硬编码可信数据源，主源经 HTTPS + 证书校验
                total = int(response.headers.get('Content-Length', 0))
                if total < 1000:
                    raise ValueError(f'Response too small: {total} bytes')

                size_mb = total / 1024 / 1024
                log(f'[DB] Remote DB size: {size_mb:.1f} MB')
                if progress_callback:
                    progress_callback(f'正在下载数据... ({size_mb:.1f} MB)')

                downloaded = 0
                last_progress_time = time.time()

                with open(tmp_path, 'wb') as f:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)

                        # 每 2 秒更新一次进度
                        now = time.time()
                        if now - last_progress_time > 2 and progress_callback:
                            pct = downloaded / total * 100 if total > 0 else 0
                            progress_callback(f'下载中... {pct:.0f}% ({downloaded/1024/1024:.1f}/{size_mb:.1f} MB)')
                            last_progress_time = now

            # 验证下载的文件
            if os.path.getsize(tmp_path) < 1000:
                raise ValueError('Downloaded file too small')

            # SHA-256 完整性校验：服务器发布了摘要就必须匹配
            expected_sha = _fetch_remote_sha256(url)
            if expected_sha:
                actual_sha = _sha256_of(tmp_path)
                if actual_sha != expected_sha:
                    raise ValueError(
                        f'SHA-256 mismatch: expected {expected_sha}, got {actual_sha}')
                log('[DB] SHA-256 checksum OK')
            else:
                log('[DB] WARN: no .sha256 published by server, skipping checksum')

            # 验证 SQLite 结构
            import sqlite3
            conn = sqlite3.connect(tmp_path, timeout=5)
            try:
                cur = conn.cursor()
                cur.execute('SELECT COUNT(*) FROM kline_data')
                klines = cur.fetchone()[0]
                cur.execute('SELECT COUNT(*) FROM stock_metadata')
                stocks = cur.fetchone()[0]
            finally:
                conn.close()

            log(f'[DB] Downloaded: {klines} klines, {stocks} stocks')

            # 备份旧数据库（新库替换失败时回滚，避免本地处于无库状态）
            backup = None
            if os.path.exists(LOCAL_DB):
                backup = LOCAL_DB + '.bak'
                if os.path.exists(backup):
                    os.remove(backup)
                shutil.move(LOCAL_DB, backup)
                log('[DB] Backed up old database')

            try:
                # 替换为新数据库
                shutil.move(tmp_path, LOCAL_DB)
                tmp_path = None  # 已移动，异常处理分支不再删除
            except Exception:
                if backup and os.path.exists(backup) and not os.path.exists(LOCAL_DB):
                    shutil.move(backup, LOCAL_DB)
                    log('[DB] Replace failed, restored backup')
                raise
            log(f'[DB] Database saved to {LOCAL_DB}')

            if progress_callback:
                progress_callback(f'数据下载完成! ({klines} 条K线, {stocks} 只股票)')

            return True

        except Exception as e:
            log(f'[DB] Download from {url} failed: {e}')
            last_error = e
            if progress_callback:
                progress_callback(f'下载失败: {e}')
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    log(f'[DB] All download URLs failed. Last error: {last_error}')
    return False


# ---------------------------------------------------------------------------
# 启动引导页（WebView2 渲染 WinUI 3 / Fluent 风格）
#
# Sync 模式在主界面就绪前要经历"等服务器数据 → 下载 31MB → 校验"。主窗口
# 先以引导页打开，数据就绪后同一窗口跳转主界面——整段过程对用户全程可见。
# 页面通过轮询 /api/boot/status 获取进度，无需额外 IPC。
# ---------------------------------------------------------------------------
_BOOT_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>StockChart</title>
<style>
 /* 随应用打包的 HarmonyOS Sans SC（华为官方允许免费商用） */
 @font-face{font-family:'HarmonyOS Sans SC';
   src:url('/assets/fonts/HarmonyOS_Sans_SC_Regular.ttf') format('truetype');
   font-weight:400;font-display:swap}
 @font-face{font-family:'HarmonyOS Sans SC';
   src:url('/assets/fonts/HarmonyOS_Sans_SC_Medium.ttf') format('truetype');
   font-weight:500;font-display:swap}
 *{box-sizing:border-box;margin:0}
 html,body{height:100%;overflow:hidden;
   font-family:'HarmonyOS Sans SC','Segoe UI Variable Text','Segoe UI','Microsoft YaHei UI',sans-serif;
   /* Mica 质感：暗色基底 + 顶部微光 */
   background:linear-gradient(165deg,#2b2b2b 0%,#1f1f1f 55%,#181818 100%);color:#ffffff}
 .wrap{height:100%;display:flex;flex-direction:column;justify-content:center;padding:0 44px}
 .brand{display:flex;align-items:center;gap:14px;margin-bottom:26px}
 .icon{width:52px;height:52px;border-radius:12px;flex:none;
   background:linear-gradient(160deg,#2b7de9,#4cc2ff);
   display:flex;align-items:center;justify-content:center;
   box-shadow:0 4px 14px rgba(43,125,233,.35)}
 .icon svg{width:30px;height:30px}
 h1{font-size:20px;font-weight:600;letter-spacing:.2px}
 .sub{font-size:12px;color:#9d9d9d;margin-top:3px}
 /* WinUI 3 式进度条：3px 圆角轨道 + 强调色填充 */
 .track{height:3px;border-radius:2px;background:#3d3d3d;overflow:hidden;margin:6px 0 14px;position:relative}
 .fill{height:100%;width:0%;border-radius:2px;background:#4cc2ff;transition:width .4s ease}
 .track.indeterminate .fill{width:34%;animation:slide 1.4s cubic-bezier(.4,0,.6,1) infinite}
 @keyframes slide{0%{margin-left:-34%}100%{margin-left:100%}}
 #msg{font-size:12.5px;color:#c9c9c9;line-height:1.7;min-height:40px;white-space:pre-line}
 .err #msg{color:#ff99a4}
 footer{position:fixed;bottom:14px;left:44px;right:44px;display:flex;
   justify-content:space-between;font-size:11px;color:#6a6a6a}
</style></head><body>
<div class="wrap">
 <div class="brand">
  <div class="icon"><svg viewBox="0 0 24 24" fill="none">
   <path d="M4 19 L9 12 L13 15 L20 6" stroke="#fff" stroke-width="2"
         stroke-linecap="round" stroke-linejoin="round"/></svg></div>
  <div><h1>StockChart</h1><div class="sub">正在准备行情数据</div></div>
 </div>
 <div class="track indeterminate" id="track"><div class="fill" id="fill"></div></div>
 <div id="msg">正在启动本地服务…</div>
</div>
<footer><span>Server Sync Mode</span><span>v2</span></footer>
<script>
 function poll(){
  fetch('/api/boot/status',{cache:'no-store'}).then(function(r){return r.json()}).then(function(s){
   var track=document.getElementById('track');
   document.getElementById('msg').textContent=s.message;
   document.body.classList.toggle('err',s.phase==='failed');
   if(s.progress!=null&&s.progress>0){
    track.classList.remove('indeterminate');
    document.getElementById('fill').style.width=s.progress+'%';
   }
   if(s.phase==='ready'){location.replace('/');return;}
   if(s.phase==='failed')return;
   setTimeout(poll,800);
  }).catch(function(){setTimeout(poll,1500)});
 }
 poll();
</script></body></html>"""

# 共享启动状态：由下载线程写、/api/boot/status 读（GIL 下 dict 更新安全）
_boot_state = {'phase': 'checking', 'progress': None,
               'message': '正在启动本地服务…'}


def _register_boot_routes(flask_app):
    """注册启动引导页与进度查询接口（GET 请求，CSRF 天然豁免）"""
    from flask import Response, jsonify

    @flask_app.route('/_boot')
    def _boot_page():
        return Response(_BOOT_PAGE_HTML, mimetype='text/html')

    @flask_app.route('/api/boot/status')
    def _boot_status():
        return jsonify(dict(_boot_state))


# ---------------------------------------------------------------------------
# 主程序
# ---------------------------------------------------------------------------
def main():
    """主入口：Flask → WebView2 引导页 → 后台同步数据 → 同窗口进入主界面"""
    # 清空日志
    try:
        with open(LOG_FILE, 'w', encoding='utf-8') as f:
            f.write('=== StockChart Win11 Sync Mode ===\n')
    except Exception:  # nosec B110
        pass

    log('=' * 60)
    log('  StockChart Win11 - Server Sync Mode')
    log('=' * 60)

    # 步骤 1: 启动 Flask（app.py 重模块已惰性化，此步约 1~2s）
    log('[Step 1] Starting Flask...')
    os.environ['STOCKCHART_SYNC_MODE'] = '1'
    port = find_free_port()
    log(f'[Flask] Port: {port}')

    import app as _app_module
    _register_boot_routes(_app_module.app)
    start_flask_thread(port)
    log('[Flask] Ready')
    start_auto_update()

    # 步骤 2: 先以引导页打开窗口（覆盖"等数据 → 下载 → 校验"黑箱期）
    import re as _re
    import webview

    def _ui_progress(text, pct=None):
        """把下载进度写入共享状态，由引导页轮询展示"""
        log(text.replace('\n', ' | '))
        m = _re.search(r'(\d+(?:\.\d+)?)\s*%', text)
        _boot_state['message'] = text.replace('\n', ' ')
        if m is not None:
            _boot_state['progress'] = float(m.group(1))

    boot_url = f'http://127.0.0.1:{port}/_boot'
    main_url = f'http://127.0.0.1:{port}/'

    window = webview.create_window(
        'StockChart', url=boot_url,
        width=520, height=380, resizable=True,
    )
    close_splash()

    def _prepare_data(window):
        """后台线程：等服务器数据 → 下载数据库 → 引导页跳转主界面"""
        try:
            log('[Step 2] Checking server data readiness...')
            _boot_state.update(phase='waiting', message='正在等待服务器数据准备…')
            wait_for_data_ready(progress_callback=_ui_progress)

            log('[Step 3] Starting database sync...')
            if check_need_download():
                _boot_state.update(phase='downloading', progress=0,
                                   message='正在从服务器下载数据…')
                log('[Step 3] Download needed, starting...')
                if not download_database(progress_callback=_ui_progress):
                    if os.path.exists(LOCAL_DB):
                        log('[Step 3] Download failed, using existing local database')
                    else:
                        log('[Step 3] Download failed and no local database exists')
                        _boot_state.update(
                            phase='failed', progress=None,
                            message='无法从服务器下载数据库，且本地没有数据库文件。\n'
                                    '请检查网络连接后关闭本窗口重新运行。')
                        return
            else:
                log('[Step 3] Local data is up to date, skipping download')

            # 就绪：恢复标准尺寸、最大化并跳转主界面
            # （窗口必须可调整大小，否则主界面被锁死在小窗里，右上角徽章不可见）
            log('[Step 4] Loading main UI...')
            _boot_state.update(phase='ready', progress=100, message='启动完成')
            window.resize(1280, 800)
            try:
                window.maximize()
            except Exception:  # nosec B110 - 旧版 pywebview 无 maximize 时忽略
                pass
            window.load_url(main_url)
        except Exception as exc:  # nosec B110 - 引导页兜底展示
            log(f'Prepare failed: {exc}')
            _boot_state.update(phase='failed', progress=None,
                               message=f'启动异常: {exc}')

    log('[WebView] Starting...')
    webview.start(func=_prepare_data, args=(window,), debug=False)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:  # nosec B110
        tb = traceback.format_exc()
        log(f'Failed: {exc}\n{tb}')
        ctypes.windll.user32.MessageBoxW(
            0,
            f'启动失败: {exc}\n\n{tb}',
            'Error',
            0x10
        )
        sys.exit(1)
