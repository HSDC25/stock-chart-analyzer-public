#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
为 Win7 构建 32/64 位单文件 exe（PyInstaller 打包脚本，一次性构建工具）。

使用官方 Python 3.8.10 embeddable，禁用 UPX，并把所有 VC 运行时打包进 exe，
避免目标机器缺少 VC redist。入口是 launcher_win7.py（PyQt5 桌面壳）。
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

# 仓库根的共享包：PyInstaller 静态分析依赖 --paths + hiddenimports 才能收进单文件 exe
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
        else:
            print(f"[warn] 缺失运行时 DLL（不影响继续打包）: {dll}")
    return dst


def build(arch: str, python_dir: str, name: str, dll_list: list):
    """用指定架构的 Python 3.8 执行 PyInstaller，产出单文件 exe 并复制到 outputs/。"""
    py = ROOT / python_dir / 'python.exe'
    if not py.exists():
        raise FileNotFoundError(f"找不到 Python 解释器: {py}")

    print(f"\n=== 构建 {arch} 版本: {name} ===")

    # 0. 版本信息随 exe 一起打包：自动更新靠它判断「我该不该更新」
    update_lib.write_build_info(ROOT, BUILD_VERSION, name)

    # 1. 复制 VC 运行时到打包临时目录
    runtime_dir = copy_runtime(arch, dll_list)

    # 2. 执行 PyInstaller
    cmd = [
        str(py), '-m', 'PyInstaller',
        '--clean', '--noconfirm',
        '--onefile',
        '--windowed',
        '--noupx',
        # 仓库根必须在搜索路径里，否则共享包 common/ 会被静默跳过，exe 启动即 ModuleNotFoundError
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
        'launcher_win7.py',
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
        # 旧 exe 可能仍在运行，重命名后再次尝试
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
    os.makedirs(ROOT / 'outputs', exist_ok=True)
    try:
        build('amd64', 'python38-amd64', 'StockChart_Win7_64', RUNTIME_DLLS_64)
        build('win32', 'python38-win32', 'StockChart_Win7_32', RUNTIME_DLLS_32)
    except subprocess.CalledProcessError as e:
        print(f"打包失败: {e}")
        sys.exit(1)
