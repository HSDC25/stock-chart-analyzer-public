"""Self-update for the packaged desktop apps.

Two update granularities, both served from ``data.hsdc.cloud``:

* **UI overlay** - ``templates/`` + ``static/`` repacked as a few-hundred-KB zip,
  unpacked into ``~/Documents/StockChart/ui/<variant>/`` and used in preference to
  the copies frozen inside the EXE. Needs no restart and no write access to the
  EXE's own directory: the Flask instance that is already serving requests is
  re-pointed at the overlay in place (``repoint_overlay``), so the browser tab or
  webview just reloads.
* **Executable** - a full EXE swap. A running image cannot be overwritten but can
  be renamed, so a detached batch script waits for this process to disappear,
  renames the old EXE aside, copies the new one in and relaunches it. The
  ``.old`` file is removed on the next successful launch.

Security posture: the manifest and every payload are fetched over HTTPS only
(the plain-HTTP IP fallback used for the database is deliberately not trusted for
code), and a ``sha256`` in the manifest is mandatory for both payloads.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .config import DATA_DIR
from .version import APP_VERSION, VARIANT, is_newer

DEFAULT_MANIFEST_URL = 'https://data.hsdc.cloud/update.json'
USER_AGENT = 'StockChart-Updater/1.0'
PLACEHOLDER = '-'

# ── shared progress state (written by the worker thread, read by /api/update/status)
_state = {
    'phase': 'idle',          # idle | checking | downloading | installing | done | failed
    'message': '',
    'progress': None,         # 0..100 while downloading
    'local_version': APP_VERSION,
    'variant': VARIANT,
    'remote_version': '',
    'notes': '',
    'restart_required': False,
    'staged_exe': '',
    'error': '',
}
_state_lock = threading.Lock()
_worker = None  # type: threading.Thread | None
_live_app = None  # type: object | None      # 正在服务的 Flask 实例，界面热更时改它的指向
_live_app_template_subdir = 'templates'

# ── bat template: wait for this process to leave, then swap and relaunch
_RESTART_BAT = """@echo off
setlocal
set "MYPID=%~1"
set "PPID=%~2"
set "EXE=%~3"
set "NEW=%~4"
set "OLD=%EXE%.old"
set "TRIES=0"
:wait
tasklist /FI "PID eq %MYPID%" 2>nul | find "%MYPID%" >nul
if not errorlevel 1 goto sleep
tasklist /FI "PID eq %PPID%" 2>nul | find "%PPID%" >nul
if not errorlevel 1 goto sleep
goto swap
:sleep
set /a TRIES+=1
if %TRIES% GTR 600 goto swap
ping -n 2 -w 500 127.0.0.1 >nul
goto wait
:swap
if "%NEW%"=="%PLACEHOLDER%" goto launch
set "TRIES=0"
:rename
move /Y "%EXE%" "%OLD%" >nul 2>nul
if not errorlevel 1 goto copy
set /a TRIES+=1
if %TRIES% GTR 30 goto abort
ping -n 3 -w 500 127.0.0.1 >nul
goto rename
:copy
copy /Y "%NEW%" "%EXE%" >nul 2>nul
if not errorlevel 1 goto cleanup
move /Y "%OLD%" "%EXE%" >nul 2>nul
goto abort
:cleanup
del /Q "%NEW%" >nul 2>nul
:launch
start "" "%EXE%"
:abort
endlocal
del /Q "%~f0" >nul 2>nul
"""


class UpdateError(RuntimeError):
    """Raised for any unrecoverable update step; shown to the user as-is."""


def _set(**kwargs):
    with _state_lock:
        _state.update(kwargs)


def status():
    # type: () -> dict
    with _state_lock:
        return dict(_state)


# ────────────────────────────────────────────────────────────────────────────
# Manifest
# ────────────────────────────────────────────────────────────────────────────

def manifest_url():
    # type: () -> str
    return os.environ.get('STOCKCHART_UPDATE_URL', DEFAULT_MANIFEST_URL)


def is_trusted_url(url):
    # type: (str) -> bool
    """Accept HTTPS only, plus plain HTTP on loopback so the test suite works."""
    parsed = urlparse(url)
    if parsed.scheme == 'https':
        return True
    if parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost', '::1'):
        return True
    return False


def _ssl_context():
    try:
        import certifi
        import ssl
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        import ssl
        return ssl.create_default_context()


def _request(url, timeout=20):
    if not is_trusted_url(url):
        raise UpdateError('更新源必须使用 HTTPS，已拒绝: {}'.format(url))
    req = Request(url)
    req.add_header('User-Agent', USER_AGENT)
    return urlopen(req, timeout=timeout, context=_ssl_context())  # nosec B310


def fetch_manifest(url=None, timeout=20):
    # type: (str | None, int) -> dict
    target = url or manifest_url()
    try:
        with _request(target, timeout) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError('无法获取更新清单: {}'.format(exc))
    if not isinstance(payload, dict):
        raise UpdateError('更新清单格式错误')
    return payload


def variant_entry(manifest, variant=None):
    # type: (dict, str | None) -> dict | None
    """Return this variant's entry, tolerating case and ``.exe`` suffix drift."""
    variants = manifest.get('variants') or {}
    wanted = (variant or VARIANT).strip().lower()
    for key, value in variants.items():
        if key.strip().lower().rstrip('.exe') == wanted and isinstance(value, dict):
            return value
    return None


# ────────────────────────────────────────────────────────────────────────────
# Downloads
# ────────────────────────────────────────────────────────────────────────────

def _sha256_of(path, cancel=None):
    # type: (Path, threading.Event | None) -> str
    h = hashlib.sha256()
    with open(str(path), 'rb') as fh:
        while True:
            if cancel is not None and cancel.is_set():
                raise UpdateError('更新已取消')
            chunk = fh.read(1 << 16)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def download(url, dest, sha256, label, cancel=None, timeout=600):
    # type: (str, Path, str, str, threading.Event | None, int) -> None
    """Stream *url* to *dest* and require its sha256 to match before returning."""
    if not sha256:
        raise UpdateError('{} 缺少 sha256，拒绝安装'.format(label))
    if not is_trusted_url(url):
        raise UpdateError('更新源必须使用 HTTPS，已拒绝: {}'.format(url))
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + '.part')
    try:
        with _request(url, timeout) as resp:
            total = int(resp.headers.get('Content-Length') or 0)
            got = 0
            last_tick = 0.0
            with open(str(partial), 'wb') as fh:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise UpdateError('更新已取消')
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
                    got += len(chunk)
                    now = time.time()
                    if total and now - last_tick > 1.0:
                        last_tick = now
                        _set(phase='downloading', message='正在下载{}…'.format(label),
                             progress=round(got * 100.0 / total, 1))
        actual = _sha256_of(partial, cancel)
        if actual.lower() != sha256.strip().lower():
            raise UpdateError('{} 校验失败（sha256 不匹配，可能被篡改或下载不完整）'.format(label))
        if dest.exists():
            dest.unlink()
        shutil.move(str(partial), str(dest))
    finally:
        if partial.exists():
            try:
                partial.unlink()
            except OSError:
                pass


# ────────────────────────────────────────────────────────────────────────────
# UI overlay
# ────────────────────────────────────────────────────────────────────────────

def overlay_dir(variant=None):
    # type: (str | None) -> Path
    return DATA_DIR / 'ui' / (variant or VARIANT)


def overlay_build_info(variant=None):
    # type: (str | None) -> dict
    try:
        data = json.loads((overlay_dir(variant) / 'build_info.json').read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def overlay_version(variant=None):
    # type: (str | None) -> str
    return str(overlay_build_info(variant).get('version') or '')


def overlay_is_active():
    # type: () -> bool
    """True when this process really serves the overlay (not the frozen copies)."""
    return os.environ.get('STOCKCHART_OVERLAY_ACTIVE') == '1'


def use_overlay(flask_app, template_subdir='templates'):
    # type: (object, str) -> bool
    """启动时调用：登记正在服务的 Flask 实例，并在覆盖层可用时立刻指向它。

    登记是无条件的，即使此刻还没有覆盖层 —— 后续热更新要靠这份登记找到活着的 app。
    """
    register_live_app(flask_app, template_subdir)
    return repoint_overlay(flask_app, template_subdir)


def register_live_app(flask_app, template_subdir='templates'):
    # type: (object, str) -> None
    """记住正在服务的 Flask 实例，界面热更新时直接改它的指向，不用重启进程。"""
    global _live_app, _live_app_template_subdir
    _live_app = flask_app
    _live_app_template_subdir = template_subdir


def repoint_overlay(flask_app, template_subdir='templates', static_subdir='static'):
    # type: (object, str, str) -> bool
    """把一个正在运行的 Flask 切到覆盖层；覆盖层不可用则原样返回 False。

    三处都要改，少一处页面就还是旧的（2026-10-05 在 Flask 3.1.2 与 win7 运行时的
    3.0.3 上实测）：``template_folder`` 只是 ``jinja_loader`` 这个 cached_property
    的输入，属性已经算过一次就不会再算，所以必须直接换掉 loader；换完 loader 仍旧是
    旧页面，因为 Jinja 握着编译好的模板，再 ``jinja_env.cache.clear()`` 才生效。
    ``static_folder`` 是普通属性，改完 ``/static/...`` 立刻走新目录。
    """
    dirs = overlay_dirs(template_subdir, static_subdir)
    if not dirs:
        return False
    from jinja2 import FileSystemLoader
    tpl, stat = dirs
    flask_app.template_folder = tpl
    flask_app.static_folder = stat
    flask_app.jinja_loader = FileSystemLoader(tpl, encoding='utf-8')
    flask_app.config['TEMPLATES_AUTO_RELOAD'] = True
    flask_app.jinja_env.auto_reload = True
    flask_app.jinja_env.cache.clear()
    os.environ['STOCKCHART_OVERLAY_ACTIVE'] = '1'
    return True


def hot_repoint_overlay():
    # type: () -> bool
    """界面覆盖层刚解包完后调用：把活着的 Flask 指过去，成功就不需要重启。"""
    app = _live_app
    if app is None:
        return False
    try:
        return repoint_overlay(app, _live_app_template_subdir)
    except Exception:
        # Flask 内部结构对不上（版本漂移）时退回重启换体，不能让更新线程把异常抛出去
        return False


def overlay_dirs(template_subdir='templates', static_subdir='static'):
    # type: (str, str) -> tuple | None
    """Return (template_dir, static_dir) from the overlay when it is usable.

    The overlay only wins over what is inside the EXE when it is strictly newer;
    after an EXE update the frozen UI is at least as fresh as the overlay. Source
    runs ignore it unless ``STOCKCHART_UI_OVERLAY=1`` so editing the repo always
    takes effect.
    """
    if not getattr(sys, 'frozen', False):
        if os.environ.get('STOCKCHART_UI_OVERLAY') != '1':
            return None
    base = overlay_dir()
    tpl, stat = base / template_subdir, base / static_subdir
    if not (tpl.is_dir() and stat.is_dir()):
        return None
    version = overlay_version()
    if not version or not is_newer(version, APP_VERSION):
        return None
    return str(tpl), str(stat)


def _safe_extract(zf, dest):
    # type: (zipfile.ZipFile, Path) -> None
    """Extract *zf* into *dest*, rejecting absolute or ``..`` member names."""
    dest = Path(dest)
    for member in zf.infolist():
        name = member.filename.replace('\\', '/')
        if name.startswith('/') or re.match(r'^[A-Za-z]:', name):
            raise UpdateError('更新包内含非法绝对路径: {}'.format(member.filename))
        parts = [p for p in name.split('/') if p not in ('', '.')]
        if '..' in parts:
            raise UpdateError('更新包内含越界路径: {}'.format(member.filename))
    zf.extractall(str(dest))


def apply_ui_overlay(zip_path):
    # type: (Path) -> str
    """Unpack *zip_path* over the overlay directory. Returns 'applied' or 'pending'."""
    zip_path = Path(zip_path)
    base = overlay_dir()
    staging = base.parent / (base.name + '.new')
    backup = base.parent / (base.name + '.old')
    if staging.exists():
        shutil.rmtree(str(staging), ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        with zipfile.ZipFile(str(zip_path)) as zf:
            _safe_extract(zf, staging)
    except UpdateError:
        shutil.rmtree(str(staging), ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(str(staging), ignore_errors=True)
        raise UpdateError('更新包解压失败: {}'.format(exc))

    if not base.exists():
        staging.rename(base)
        return 'applied'

    moved = False
    for attempt in range(5):
        try:
            if backup.exists():
                shutil.rmtree(str(backup), ignore_errors=True)
            os.rename(str(base), str(backup))
            moved = True
            break
        except OSError:
            time.sleep(0.6)

    if not moved:
        # A file inside the overlay is still open (a page mid-request).
        _set(message='界面文件正被占用，已尽力更新，部分内容将在下次启动生效')
        return _copy_in_place(staging, base)

    try:
        os.rename(str(staging), str(base))
    except OSError:
        os.rename(str(backup), str(base))
        _set(message='界面文件正被占用，已尽力更新，部分内容将在下次启动生效')
        return _copy_in_place(staging, base)
    shutil.rmtree(str(backup), ignore_errors=True)
    if zip_path.exists():
        zip_path.unlink()
    return 'applied'


def _copy_in_place(staging, base):
    # type: (Path, Path) -> str
    """Non-atomic fallback: overwrite file by file, skipping locked ones."""
    copied = 0
    for src in staging.rglob('*'):
        if not src.is_file():
            continue
        dst = base / src.relative_to(staging)
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(str(src), str(dst))
            copied += 1
        except OSError:
            pass
    shutil.rmtree(str(staging), ignore_errors=True)
    return 'pending' if copied == 0 else 'applied'


# ────────────────────────────────────────────────────────────────────────────
# Executable swap
# ────────────────────────────────────────────────────────────────────────────

def _work_root():
    # type: () -> Path
    """下载临时目录的父级；DATA_DIR 在 DATA 目录被挪走或测试隔离时可能还不存在。"""
    root = DATA_DIR / 'updates'
    root.mkdir(parents=True, exist_ok=True)
    return root


def _writable_exe_dir(exe):
    # type: (Path) -> bool
    d = exe.parent
    if not os.access(str(d), os.W_OK | os.X_OK):
        return False
    probe = d / '.stockchart_write_test'
    try:
        probe.write_text('x', encoding='utf-8')
        probe.unlink()
    except OSError:
        return False
    return True


def _launch_bat(exe_dir, my_pid, ppid, exe_path, new_exe):
    # type: (Path, int, int, Path, str) -> Path
    """Write the detached swap-and-relaunch batch file."""
    script = _RESTART_BAT.replace('%PLACEHOLDER%', PLACEHOLDER)
    bat = exe_dir / 'stockchart_update.cmd'
    if bat.exists():
        try:
            bat.unlink()
        except OSError:
            bat = exe_dir / 'stockchart_update_{}.cmd'.format(int(time.time()))
    bat.write_text(script, encoding='ascii', newline='\r\n')
    cmd = ['cmd', '/c', str(bat), str(my_pid), str(ppid), str(exe_path), new_exe]
    flags = 0
    if os.name == 'nt':
        # 必须用 CREATE_NO_WINDOW，不能用 DETACHED_PROCESS：没有控制台的 cmd 执行到脚本里
        # 第一处 `>nul` 重定向就中断整个批处理，换体静默不发生（2026-10-05 实测）。
        # CREATE_NO_WINDOW 给 cmd 一个隐藏控制台，既不闪窗口又能正常重定向。
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
    subprocess.Popen(  # nosec B603 - 固定调用本机 cmd，参数均为本地路径
        cmd, cwd=str(exe_dir), creationflags=flags, close_fds=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return bat


def schedule_restart(new_exe=None):
    # type: (str | None) -> None
    """Hand the swap-and-relaunch to a detached script, then leave immediately.

    ``os._exit`` on the frozen child is what PyInstaller's parent bootloader
    waits for, so its ``_MEIPASS`` temp directory is still cleaned up.
    """
    exe = Path(sys.executable) if getattr(sys, 'frozen', False) else None
    if exe is None:
        raise UpdateError('源码运行状态下无法自替换，请直接重新运行')
    _launch_bat(exe.parent, os.getpid(), os.getppid(), exe, new_exe or PLACEHOLDER)
    _set(phase='done', message='正在安装更新并重新启动…', restart_required=False,
         progress=100)
    time.sleep(0.5)
    os._exit(0)


# ────────────────────────────────────────────────────────────────────────────
# Orchestration
# ────────────────────────────────────────────────────────────────────────────

def plan_update(manifest=None):
    # type: (dict | None) -> dict
    """Return ``{'entry':…, 'exe': bool, 'ui': bool}`` for this variant."""
    manifest = manifest or fetch_manifest()
    entry = variant_entry(manifest)
    if not entry:
        raise UpdateError('更新清单中没有本变体: {}'.format(VARIANT))
    remote = str(entry.get('version') or '')
    return {
        'entry': entry,
        'remote_version': remote,
        'exe': bool(remote) and is_newer(remote, APP_VERSION),
        'ui': bool(entry.get('ui_version')) and is_newer(str(entry['ui_version']),
                                                          overlay_version() or APP_VERSION),
    }


def run_update(auto_apply=True, restart=True, cancel=None):
    # type: (bool, bool, threading.Event | None) -> dict
    """Check and (when *auto_apply*) install updates. Never raises: reports state."""
    _set(phase='checking', message='正在检查更新…', error='', progress=None,
         remote_version='', notes='')
    workdir = None
    try:
        manifest = fetch_manifest()
        plan = plan_update(manifest)
        entry = plan['entry']
        remote = plan['remote_version']
        _set(remote_version=remote, notes=str(entry.get('notes') or ''))
        if not remote:
            raise UpdateError('更新清单缺少版本号')

        if not plan['exe'] and not plan['ui']:
            _set(phase='done', message='已是最新版本 v{}'.format(APP_VERSION), progress=100)
            return {'updated': False, 'phase': 'done'}

        if not auto_apply:
            _set(phase='done', message='发现新版本 v{}，等待确认'.format(remote), progress=100)
            return {'updated': False, 'phase': 'available', 'plan': plan}

        workdir = Path(tempfile.mkdtemp(prefix='stockchart_update_',
                                        dir=str(_work_root())))

        if plan['exe']:
            exe_path = Path(sys.executable) if getattr(sys, 'frozen', False) else None
            if exe_path is None:
                raise UpdateError('源码运行状态下无法替换程序本体')
            if not _writable_exe_dir(exe_path):
                raise UpdateError(
                    '程序所在目录不可写，无法自动更新本体。请把程序放到用户目录后重试，'
                    '例如 %LOCALAPPDATA%\\Programs\\StockChart')
            if not entry.get('exe_url'):
                raise UpdateError('更新清单缺少 exe_url')
            new_exe = workdir / exe_path.name
            download(str(entry['exe_url']), new_exe, str(entry.get('exe_sha256') or ''),
                     '程序更新包', cancel)
            # 覆盖层不在这里删：这个进程很可能正用它渲染页面（界面热更后就一直挂着），
            # 删掉之后到 os._exit 之前若有请求就是整页 500，走 staged 分支更是能挂到下次重启。
            # 新 EXE 内置界面不比它旧，overlay_dirs() 的版本闸门本来就会忽略这份，
            # 真正的回收交给下次启动的 cleanup_leftovers()。
            _set(phase='installing', message='正在替换程序本体…', progress=100)
            if restart:
                schedule_restart(str(new_exe))     # os._exit()s, workdir cleanup below is skipped
            staged = staged_exe_dir() / exe_path.name
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(new_exe), str(staged))
            _set(restart_required=True, staged_exe=str(staged),
                 message='新版本 v{} 已下载，重启后生效'.format(remote))
            return {'updated': True, 'kind': 'exe', 'restart_required': True}

        # UI-only: a few hundred KB, no EXE write access needed
        if not entry.get('ui_url'):
            raise UpdateError('更新清单缺少 ui_url')
        ui_zip = workdir / 'ui.zip'
        download(str(entry['ui_url']), ui_zip, str(entry.get('ui_sha256') or ''),
                 '界面更新包', cancel)
        result = apply_ui_overlay(ui_zip)
        # 覆盖层已解压到目标目录，包体随即作废。重启走 os._exit 跳过 finally，
        # 不在这里删就会每次热更都在 Documents 里留几百 KB。
        shutil.rmtree(str(workdir), ignore_errors=True)
        workdir = None
        ui_ver = str(entry.get('ui_version') or remote)
        if result == 'applied' and hot_repoint_overlay():
            # 活着的 Flask 已经指向新覆盖层：刷新页面就生效。进程不重启，端口和浏览器
            # 标签页都不变——win7 浏览器版的使用者是老人，任何"重新打开程序"都是障碍。
            _set(phase='done', progress=100, restart_required=False,
                 message='界面已更新到 v{}，刷新即生效'.format(ui_ver))
            return {'updated': True, 'kind': 'ui', 'restart_required': False, 'hot': True}
        if result == 'applied':
            _set(phase='done', progress=100, message='界面已更新到 v{}'.format(ui_ver))
        else:
            _set(phase='done', progress=100, message='界面已部分更新，重启后完整生效')
        if overlay_is_active():
            # 本来就在服务覆盖层（模板热重载已开）：页面刷新即可，不用重启进程
            _set(restart_required=False)
            return {'updated': True, 'kind': 'ui', 'restart_required': False}
        # The frozen folders are already bound to Flask: relaunch to pick up the
        # overlay unless this process is already serving it.
        if restart and result == 'applied':
            schedule_restart()
        _set(restart_required=True)
        return {'updated': True, 'kind': 'ui', 'restart_required': True}
    except UpdateError as exc:
        _set(phase='failed', message=str(exc), error=str(exc), progress=None)
        return {'updated': False, 'phase': 'failed', 'error': str(exc)}
    except Exception as exc:
        text = '更新失败: {}'.format(exc)
        _set(phase='failed', message=text, error=text, progress=None)
        return {'updated': False, 'phase': 'failed', 'error': text}
    finally:
        if workdir and workdir.exists():
            shutil.rmtree(str(workdir), ignore_errors=True)


def start_auto_update(wait_for_ui=True):
    # type: (bool) -> threading.Thread | None
    """Kick off the startup update in a daemon thread; safe to call twice."""
    global _worker
    if _worker is not None and _worker.is_alive():
        return _worker
    if os.environ.get('STOCKCHART_DISABLE_AUTO_UPDATE') == '1':
        _set(phase='idle', message='已禁用自动更新')
        return None

    def _job():
        if wait_for_ui:
            # Let the window paint first: the update must never delay startup.
            time.sleep(2.0)
        run_update(auto_apply=True, restart=True)

    _worker = threading.Thread(target=_job, daemon=True, name='stockchart-update')
    _worker.start()
    return _worker


def start_manual_check():
    # type: () -> threading.Thread
    """User pressed "检查更新": same pipeline, no silent install of a new EXE."""
    global _worker
    if _worker is not None and _worker.is_alive():
        return _worker

    def _job():
        run_update(auto_apply=True, restart=True)

    _worker = threading.Thread(target=_job, daemon=True, name='stockchart-update-manual')
    _worker.start()
    return _worker


def cleanup_leftovers():
    # type: () -> None
    """After a successful launch, drop the ``.old`` EXE and a stale overlay.

    Called by the launchers on startup: the presence of ``<exe>.old`` proves the
    previous update really booted, so both it and the now-redundant overlay (the
    new EXE carries the same UI) can go.
    """
    if not getattr(sys, 'frozen', False):
        return
    exe = Path(sys.executable)
    for stale in (Path(str(exe) + '.old'), exe.parent / 'stockchart_update.cmd'):
        try:
            if stale.exists():
                stale.unlink()
        except OSError:
            pass
    staged = staged_exe_dir()
    if staged.exists():
        shutil.rmtree(str(staged), ignore_errors=True)
    # 换体脚本坏掉的旧版本（2026-10-05 之前发布的）每启动一次就在下载目录里留下
    # 一份完整包体且无人回收，半小时前的目录当作已废弃处理，正在下载的那份不动。
    cutoff = time.time() - 1800
    for stale in _work_root().glob('stockchart_update_*'):
        try:
            if stale.is_dir() and stale.stat().st_mtime < cutoff:
                shutil.rmtree(str(stale), ignore_errors=True)
        except OSError:
            pass

    # 覆盖层的回收：换过本体后内置那份已经够新，这份副本没人再读（几百 KB~几 MB）。
    # .old/.new 是 apply_ui_overlay 被硬杀后的残骸。此刻 app 用的要么是内置目录、要么
    # 是"仍然更新"的覆盖层，删掉的都是版本闸门已经忽略的那份，不会把服务中的页面删空。
    overlay = overlay_dir()
    for leftover in (Path(str(overlay) + '.old'), Path(str(overlay) + '.new')):
        try:
            if leftover.is_dir():
                shutil.rmtree(str(leftover), ignore_errors=True)
        except OSError:
            pass
    version = overlay_version()
    if overlay.is_dir() and (not version or not is_newer(version, APP_VERSION)):
        shutil.rmtree(str(overlay), ignore_errors=True)


def staged_exe_dir():
    # type: () -> Path
    return DATA_DIR / 'updates' / 'pending_exe'


def apply_staged_update():
    # type: () -> bool
    """Swap in an EXE downloaded earlier (restart=False) and relaunch."""
    staged = Path(status().get('staged_exe') or '')
    if not staged.is_file():
        pending = list(staged_exe_dir().glob('*.exe'))
        staged = pending[0] if pending else staged
    if not staged.is_file():
        raise UpdateError('没有待安装的更新包')
    schedule_restart(str(staged))
    return True
