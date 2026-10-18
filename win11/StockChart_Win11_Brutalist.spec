# -*- mode: python ; coding: utf-8 -*-

import os

# 以 spec 文件所在目录定位工程根目录，避免硬编码开发者机器上的绝对路径
_SPEC_DIR = os.path.abspath(SPECPATH)
_ROOT = os.path.abspath(os.path.join(_SPEC_DIR, '..'))


a = Analysis(
    ['launcher_webview_brutalist.py'],
    pathex=[_ROOT],
    binaries=[],
    datas=[(os.path.join(_SPEC_DIR, 'build_info.json'), '.'), (os.path.join(_SPEC_DIR, 'themes', 'brutalist', 'templates'), 'templates'), (os.path.join(_SPEC_DIR, 'themes', 'brutalist', 'static'), 'static'), (os.path.join(_ROOT, 'assets', 'fonts'), 'assets/fonts'), (os.path.join(_ROOT, 'assets', 'js'), 'assets/js')],
    hiddenimports=['flask', 'pandas', 'numpy', 'baostock', 'certifi', 'ssl', 'sqlite3', 'jinja2', 'markupsafe', 'werkzeug', 'click', 'itsdangerous', 'webview', 'requests', 'urllib3', 'charset_normalizer', 'idna', 'common', 'common.cache', 'common.config', 'common.db', 'common.fetcher', 'common.indicators', 'common.models', 'common.preload', 'common.screening', 'common.security', 'common.trading_calendar', 'common.routes', 'common.routes.api_routes', 'common.routes.screen_routes', 'common.routes.ml_routes', 'common.routes.chart_routes', 'common.routes.assets_routes', 'common.routes.update_routes', 'common.version', 'common.updater'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['cefpython3'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
splash = Splash(
    os.path.join(_SPEC_DIR, 'splash.png'),
    binaries=a.binaries,
    datas=a.datas,
    text_pos=None,
    text_size=12,
    minify_script=True,
    always_on_top=True,
)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    splash,
    splash.binaries,
    [],
    name='StockChart_Win11_Brutalist',
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
