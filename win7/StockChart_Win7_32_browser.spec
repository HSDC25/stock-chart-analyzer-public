# -*- mode: python ; coding: utf-8 -*-


block_cipher = None


a = Analysis(
    ['launcher_win7_fast.py'],
    pathex=['C:\\Users\\ASUS\\.qoderworkcn\\workspace\\mqxnujmu7xwpkdh9'],
    binaries=[],
    datas=[('templates', 'templates'), ('static', 'static'), ('../assets/fonts', 'assets/fonts'), ('../assets/js', 'assets/js'), ('build_info.json', '.'), ('C:\\Users\\ASUS\\.qoderworkcn\\workspace\\mqxnujmu7xwpkdh9\\win7\\win7_runtime\\win32', '.')],
    hiddenimports=['flask', 'pandas', 'numpy', 'baostock', 'certifi', 'ssl', 'sqlite3', 'requests', 'common', 'common.cache', 'common.config', 'common.db', 'common.fetcher', 'common.indicators', 'common.models', 'common.preload', 'common.screening', 'common.security', 'common.trading_calendar', 'common.routes', 'common.routes.api_routes', 'common.routes.chart_routes', 'common.routes.ml_routes', 'common.routes.screen_routes', 'common.routes.assets_routes', 'common.routes.update_routes', 'common.version', 'common.updater'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='StockChart_Win7_32_browser',
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
