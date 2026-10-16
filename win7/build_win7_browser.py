#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Win7 浏览器版打包脚本（fallback，不依赖 Qt，一次性构建工具）。

把 launcher_win7_fast.py 打成 32/64 位单文件 exe，运行时调用系统
已装的 Chromium 内核浏览器打开页面；同样把 VC 运行时 DLL 打进 exe，
避免目标机器缺少 redist。
"""
import os
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT.parent / 'scripts'))
import update_lib  # noqa: E402

BUILD_VERSION = update_lib.today_version()

COMMON_MODULES = [
    'common', 'common.cache', 'common.config', 'common.db', 'common.fetcher',
    'common.indicators', 'common.models', 'common.preload', 'common.screening',
    'common.security', 'common.trading_calendar', 'common.routes',
    'common.routes.api_routes', 'common.routes.chart_routes',
    'common.routes.ml_routes', 'common.routes.screen_routes',
    'common.routes.assets_routes', 'common.routes.update_routes',
    'common.version', 'common.updater',
]

RUNTIME_DLLS_64 = [
    r'C:\Windows\System32\msvcp140.dll',
    r'C:\Windows\System32\msvcp140_1.dll',
    r'C:\Windows\System32\msvcp140_2.dll',
    r'C:\Windows\System32\concrt140.dll',
    r'C:\Windows\System32\vcruntime140.dll',
    r'C:\Windows\System32\vcruntime140_1.dll',
    r'C:\Windows\System32\ucrtbase.dll',
]

RUNTIME_DLLS_32 = [
    r'C:\Windows\SysWOW64\msvcp140.dll',
    r'C:\Windows\SysWOW64\msvcp140_1.dll',
    r'C:\Windows\SysWOW64\msvcp140_2.dll',
    r'C:\Windows\SysWOW64\concrt140.dll',
    r'C:\Windows\SysWOW64\vcruntime140.dll',
    r'C:\Windows\SysWOW64\vcruntime140_1.dll',
    r'C:\Windows\SysWOW64\ucrtbase.dll',
]


def copy_runtime(arch: str, dll_list: list):
    """把 VC/UCRT 运行时 DLL 复制到打包临时目录，随 exe 一起分发。"""
    dst = ROOT / 'win7_runtime' / arch
    dst.mkdir(parents=True, exist_ok=True)
    for dll in dll_list:
        if os.path.exists(dll):
            shutil.copy(dll, dst / os.path.basename(dll))
    return dst


def build(arch: str, python_dir: str, name: str, dll_list: list):
    """用指定架构的 Python 3.8 执行 PyInstaller，产出单文件 exe 并复制到 outputs/。"""
    py = ROOT / python_dir / 'python.exe'
    if not py.exists():
        raise FileNotFoundError(f"找不到 Python 解释器: {py}")

    runtime_dir = copy_runtime(arch, dll_list)
    # 版本信息随 exe 打包，自动更新据此判断是否需要更新
    update_lib.write_build_info(ROOT, BUILD_VERSION, name)
    cmd = [
        str(py), '-m', 'PyInstaller',
        '--clean', '--noconfirm',
        '--onefile',
        '--windowed',
        '--noupx',
        # The repo root must be on the search path or the shared common/ package
        # is silently skipped and the exe dies with ModuleNotFoundError at startup.
        '--paths', str(ROOT.parent),
        '--add-data', 'templates;templates',
        '--add-data', 'static;static',
        '--add-data', '../assets/fonts;assets/fonts',
        '--add-data', '../assets/js;assets/js',
        '--add-data', 'build_info.json;.',
        '--add-data', f'{runtime_dir};.',
        '--hidden-import', 'flask',
        '--hidden-import', 'pandas',
        '--hidden-import', 'numpy',
        '--hidden-import', 'baostock',
        '--hidden-import', 'certifi',
        '--hidden-import', 'ssl',
        '--hidden-import', 'sqlite3',
        '--hidden-import', 'requests',
        '--name', name,
        'launcher_win7_fast.py',
    ]
    for module in COMMON_MODULES:
        cmd += ['--hidden-import', module]
    subprocess.run(cmd, check=True, cwd=ROOT)  # nosec B603

    src = ROOT / 'dist' / f'{name}.exe'
    # dist/ 存带构建日期的版本号，outputs/ 存稳定名（始终是最新构建）
    versioned = ROOT / 'dist' / f'{name}_v{BUILD_VERSION}.exe'
    src.replace(versioned)
    for stale in ROOT.glob(f'dist/{name}_v*.exe'):
        if stale != versioned:
            stale.unlink()

    dst = ROOT / 'outputs' / f'{name}.exe'
    try:
        shutil.copy2(versioned, dst)
    except PermissionError:
        backup = ROOT / 'outputs' / f"{name}_old.exe"
        if backup.exists():
            backup.unlink()
        try:
            dst.rename(backup)
            shutil.copy2(versioned, dst)
        except OSError as rename_err:
            print(f"[WARN] 旧 exe 被占用且无法改名，保留旧文件跳过覆盖: {rename_err}")
    print(f"打包完成: {versioned.name} -> {dst} ({dst.stat().st_size / 1024 / 1024:.1f} MB)")
    update_lib.write_sidecar(ROOT / 'outputs', BUILD_VERSION, name, versioned)


if __name__ == '__main__':
    try:
        build('amd64', 'python38-amd64', 'StockChart_Win7_64_browser', RUNTIME_DLLS_64)
        build('win32', 'python38-win32', 'StockChart_Win7_32_browser', RUNTIME_DLLS_32)
    except subprocess.CalledProcessError as e:
        print(f"打包失败: {e}")
        sys.exit(1)
