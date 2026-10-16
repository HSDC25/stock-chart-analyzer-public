#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Win11 WebView2 桌面版 PyInstaller 打包脚本（uv 运行，一次性构建工具）。

使用系统自带的 WebView2 (Edge Chromium) 渲染，构建 4 个主题 exe +
1 个服务器同步版（launcher_webview_sync.py）；打包前检测 WebView2
Runtime 是否存在，并把 common 包各子模块列入 hidden-import。
产物复制到 outputs/ 及桌面 StockChart_Win11 目录。
"""
import os
import shutil
import subprocess  # nosec B404 - 构建脚本，命令为硬编码
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT.parent / 'scripts'))
import update_lib  # noqa: E402

# dist/ 中的产物带构建日期版本号，outputs/ 中的产物用稳定名（始终是最新一次构建）
BUILD_VERSION = update_lib.today_version()

# 主题配置: (主题目录, 启动器文件, 输出名称)
THEMES = [
    ('highend', 'launcher_webview_highend.py', 'StockChart_Win11_HighEnd'),
    ('brutalist', 'launcher_webview_brutalist.py', 'StockChart_Win11_Brutalist'),
    # 同步模式 - 从服务器下载数据库
    ('dark', 'launcher_webview_sync.py', 'StockChart_Win11_Sync'),
]

# 隐藏导入
HIDDEN_IMPORTS = [
    'flask',
    'pandas',
    'numpy',
    'baostock',
    'certifi',
    'ssl',
    'sqlite3',
    'jinja2',
    'markupsafe',
    'werkzeug',
    'click',
    'itsdangerous',
    'webview',
    'requests',
    'urllib3',
    'charset_normalizer',
    'idna',
    'common',
    'common.cache',
    'common.config',
    'common.db',
    'common.fetcher',
    'common.indicators',
    'common.models',
    'common.preload',
    'common.screening',
    'common.security',
    'common.trading_calendar',
    'common.routes',
    'common.routes.api_routes',
    'common.routes.screen_routes',
    'common.routes.ml_routes',
    'common.routes.chart_routes',
    'common.routes.assets_routes',
    'common.routes.update_routes',
    'common.version',
    'common.updater',
]


def check_webview2_runtime():
    """检查系统是否安装了 WebView2 Runtime"""
    import winreg
    
    # 检查注册表中的 WebView2 Runtime
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
        ) as key:
            version, _ = winreg.QueryValueEx(key, "pv")
        print(f"[OK] WebView2 Runtime 已安装: {version}")
        return True
    except FileNotFoundError:
        pass

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
        ) as key:
            version, _ = winreg.QueryValueEx(key, "pv")
        print(f"[OK] WebView2 Runtime 已安装: {version}")
        return True
    except FileNotFoundError:
        pass
    
    # 检查是否为 Win11 (自带 WebView2)
    if sys.platform == 'win32':
        import platform
        win_ver = platform.version()
        # Win11 版本号 >= 10.0.22000；取前 3 段，防止带 UBR 补丁号的 4 段版本解包失败
        parts = [int(x) for x in win_ver.split('.')[:3]]
        while len(parts) < 3:
            parts.append(0)
        major, minor, build = parts
        if major >= 10 and build >= 22000:
            print(f"[OK] Windows 11 检测到 (Build {build})，自带 WebView2")
            return True
    
    print("[WARN] 未检测到 WebView2 Runtime，但 Win11 通常自带")
    return True  # Win11 自带，继续打包


def build_theme(theme_dir: str, launcher: str, name: str) -> bool:
    """构建单个主题版本"""
    print(f"\n{'='*60}")
    print(f"  正在构建: {name}")
    print(f"  启动器: {launcher}")
    print(f"  主题: {theme_dir}")
    print(f"{'='*60}")

    # 构建 PyInstaller 命令
    cmd = [
        'uv', 'run', '--python', '3.13', 'pyinstaller',
        '--clean',
        '--noconfirm',
        '--onefile',
        '--windowed',
        '--noupx',
        '--paths', str(ROOT.parent),
        # pywebview hook 会打包全部后端：cefpython3 白占 108MB，且解压 chrome_elf.dll 常被杀软拦停导致 onefile 启动失败
        '--exclude-module', 'cefpython3',
    ]

    # 添加数据文件 (主题文件)——一律基于 ROOT 取绝对路径：
    # 用相对路径判断 exists() 会在「从仓库根执行本脚本」时全部落空，打出没有 templates 的空壳 exe
    templates_path = ROOT / 'themes' / theme_dir / 'templates'
    static_path = ROOT / 'themes' / theme_dir / 'static'

    # 版本信息打进 exe 根：自动更新靠它判断是否落后于服务器清单
    update_lib.write_build_info(ROOT, BUILD_VERSION, name)
    build_info = ROOT / 'build_info.json'
    if build_info.exists():
        cmd.extend(['--add-data', f'{build_info};.'])
    if templates_path.exists():
        cmd.extend(['--add-data', f'{templates_path};templates'])
    if static_path.exists():
        cmd.extend(['--add-data', f'{static_path};static'])
    # 共享字体（所有主题共用仓库根 assets/fonts 一份）
    fonts_path = ROOT.parent / 'assets' / 'fonts'
    if fonts_path.exists():
        cmd.extend(['--add-data', f'{fonts_path};assets/fonts'])
    # 共享脚本（更新徽章 update.js，由 /assets/js/ 路由提供）
    js_path = ROOT.parent / 'assets' / 'js'
    if js_path.exists():
        cmd.extend(['--add-data', f'{js_path};assets/js'])

    # 添加隐藏导入
    for imp in HIDDEN_IMPORTS:
        cmd.extend(['--hidden-import', imp])

    # 启动画面：覆盖 onefile 解压 + import 重库的黑箱期（窗口出现前唯一反馈）
    splash = ROOT / 'splash.png'
    if splash.exists():
        cmd.extend(['--splash', str(splash)])

    # 设置输出名称
    cmd.extend(['--name', name])

    # 启动器脚本
    cmd.append(launcher)

    # 缺了模板/版本会打出「能启动但页面全 404、且永远自认 0.0.0」的空壳 exe，构建前直接拦住
    for required in (templates_path, build_info):
        if not required.exists():
            print(f"\n[FAIL] 缺少打包资源: {required}")
            return False

    print(f"\n命令: {' '.join(cmd)}\n")

    # 安全修复：使用 list 参数 + shell=False（默认），防止命令注入
    # 所有参数均为受信任的硬编码值，无用户输入
    result = subprocess.run(cmd, cwd=ROOT, capture_output=False, shell=False, check=False)  # nosec B603 - 命令参数为硬编码

    if result.returncode != 0:
        print(f"\n[FAIL] {name} 构建失败")
        return False

    # 产物改名加版本号，再复制稳定名到 outputs/
    built = ROOT / 'dist' / f'{name}.exe'
    if not built.exists():
        print(f"\n[FAIL] 未找到产物: {built}")
        return False
    versioned = ROOT / 'dist' / f'{name}_v{BUILD_VERSION}.exe'
    built.replace(versioned)
    # 清掉 dist/ 里同名主题的历史版本，避免旧 exe 重新堆积
    for stale in ROOT.glob(f'dist/{name}_v*.exe'):
        if stale != versioned:
            stale.unlink()

    dst = ROOT / 'outputs' / f'{name}.exe'
    try:
        shutil.copy2(versioned, dst)
        size_mb = dst.stat().st_size / 1024 / 1024
        print(f"\n[OK] {versioned.name} -> outputs/{dst.name} ({size_mb:.1f} MB)")
        update_lib.write_sidecar(ROOT / 'outputs', BUILD_VERSION, name, versioned)
        return True
    except Exception as e:
        print(f"\n[FAIL] 复制 {name} 失败: {e}")
        return False


def create_spec_file(theme_dir: str, launcher: str, name: str) -> Path:
    """为指定主题生成 .spec 文件"""
    spec_content = f'''# -*- mode: python ; coding: utf-8 -*-
"""
{name} - Win11 WebView2 版本
自动生成的 PyInstaller spec 文件
"""

block_cipher = None

a = Analysis(
    ['{launcher}'],
    pathex=[{str(REPO.parent)!r}],
    binaries=[],
    datas=[
        ('build_info.json', '.'),
        ('themes/{theme_dir}/templates', 'templates'),
        ('themes/{theme_dir}/static', 'static'),
        ('../assets/fonts', 'assets/fonts'),
        ('../assets/js', 'assets/js'),
    ],
    hiddenimports={HIDDEN_IMPORTS},
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=['cefpython3'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='{name}',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
'''
    spec_path = ROOT / f'{name}.spec'
    with open(spec_path, 'w', encoding='utf-8') as f:
        f.write(spec_content)
    
    print(f"[OK] 生成 spec 文件: {spec_path}")
    return spec_path


def main():
    """检测环境 → 逐主题构建 → 汇总结果 → 复制成品到桌面。"""
    print("=" * 60)
    print("  StockChart Win11 WebView2 打包工具")
    print("  使用 uv + PyInstaller + 系统 WebView2")
    print("=" * 60)

    # 检查 WebView2 Runtime
    if sys.platform == 'win32':
        check_webview2_runtime()

    # 创建输出目录
    os.makedirs(ROOT / 'outputs', exist_ok=True)

    # 构建所有主题
    results = {}
    for theme_dir, launcher, name in THEMES:
        try:
            results[name] = build_theme(theme_dir, launcher, name)
        except Exception as e:
            print(f"\n[ERROR] {name}: {e}")
            results[name] = False

    # 打印汇总
    print(f"\n{'='*60}")
    print("  构建汇总")
    print(f"{'='*60}")
    for name, success in results.items():
        status = "[OK]" if success else "[FAIL]"
        print(f"  {name}: {status}")

    # 复制到桌面
    desktop = Path.home() / 'Desktop'
    if desktop.exists():
        target_dir = desktop / 'StockChart_Win11'
        target_dir.mkdir(exist_ok=True)

        print(f"\n复制到桌面: {target_dir}")
        for _, _, name in THEMES:
            src = ROOT / 'outputs' / f'{name}.exe'
            if src.exists():
                try:
                    shutil.copy2(src, target_dir / f'{name}.exe')
                    print(f"  {name}.exe")
                except Exception as e:
                    print(f"  复制失败 {name}: {e}")

    print(f"\n完成! 输出目录: {ROOT / 'outputs'}")
    return 0 if all(results.values()) else 1


if __name__ == '__main__':
    sys.exit(main())
