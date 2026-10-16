"""构建期与发布期共用的更新工具：版本信息注入、UI 包打包、update.json 生成。

被 ``win7/build_win7.py``、``win11/build_win11_webview.py`` 和
``scripts/publish_update.py`` 复用，三处对版本格式与清单字段的理解必须一致。

版本格式是构建日期 ``YYYY.MM.DD``（与 ``common/version.py`` 的比较规则配套）。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# 变体 -> (templates 目录, static 目录)，相对于仓库根
VARIANT_UI_SOURCES = {
    'StockChart_Win7_64': ('win7/templates', 'win7/static'),
    'StockChart_Win7_32': ('win7/templates', 'win7/static'),
    'StockChart_Win7_64_browser': ('win7/templates', 'win7/static'),
    'StockChart_Win7_32_browser': ('win7/templates', 'win7/static'),
    'StockChart_Win11_Dark': ('win11/themes/dark/templates', 'win11/themes/dark/static'),
    'StockChart_Win11_Minimalist': ('win11/themes/minimalist/templates', 'win11/themes/minimalist/static'),
    'StockChart_Win11_HighEnd': ('win11/themes/highend/templates', 'win11/themes/highend/static'),
    'StockChart_Win11_Brutalist': ('win11/themes/brutalist/templates', 'win11/themes/brutalist/static'),
    'StockChart_Win11_Sync': ('win11/themes/dark/templates', 'win11/themes/dark/static'),
}

# 打进 UI 包的共享资源目录（字体太大且几乎不变，不进包）
SHARED_ASSET_DIRS = ('assets/js',)


def today_version() -> str:
    return _dt.date.today().strftime('%Y.%m.%d')


def build_info_payload(version: str, variant: str) -> dict:
    return {
        'version': version,
        'variant': variant,
        'build_time': _dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


def write_build_info(directory: Path, version: str, variant: str) -> Path:
    """写 build_info.json 到构建目录，由 --add-data 打进 exe 根。"""
    path = Path(directory) / 'build_info.json'
    path.write_text(json.dumps(build_info_payload(version, variant),
                               ensure_ascii=False, indent=2), encoding='utf-8')
    return path


def write_sidecar(outputs_dir: Path, version: str, variant: str, exe: Path) -> Path:
    """在 outputs/ 里留一个 <variant>.json，供发布脚本读取本次构建的版本与产物。"""
    path = Path(outputs_dir) / f'{variant}.json'
    payload = build_info_payload(version, variant)
    payload['exe'] = str(Path(exe).relative_to(REPO)).replace('\\', '/')
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(str(path), 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def ui_sources(variant: str, sidecar: dict | None = None) -> tuple:
    if sidecar:
        tpl = sidecar.get('ui_templates')
        stat = sidecar.get('ui_static')
        if tpl and stat:
            return tpl, stat
    return VARIANT_UI_SOURCES[variant]


def make_ui_zip(variant: str, version: str, dest: Path) -> Path:
    """打包 templates/ + static/ + 共享 js，作为「界面覆盖层」下发（约几百 KB）。

    包内根部的 build_info.json 记录 ui_version，客户端据此决定是否优先于 exe 内置界面。
    """
    templates, static = ui_sources(variant)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(dest), 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr('build_info.json', json.dumps(
            build_info_payload(version, variant), ensure_ascii=False, indent=2))
        for source_dir, arc_prefix in ((templates, 'templates'), (static, 'static')):
            root = REPO / source_dir
            if not root.is_dir():
                raise FileNotFoundError(f'缺少界面资源目录: {root}')
            for file in sorted(root.rglob('*')):
                if file.is_file():
                    zf.write(str(file), f'{arc_prefix}/{file.relative_to(root)}'.replace('\\', '/'))
        for shared in SHARED_ASSET_DIRS:
            root = REPO / shared
            if root.is_dir():
                for file in sorted(root.rglob('*')):
                    if file.is_file():
                        zf.write(str(file), f'{shared}/{file.relative_to(root)}'.replace('\\', '/'))
    return dest


def iter_sidecars() -> list:
    found = []
    for dirpath in (REPO / 'win7' / 'outputs', REPO / 'win11' / 'outputs'):
        if dirpath.is_dir():
            found.extend(sorted(dirpath.glob('*.json')))
    return found


def build_manifest(base_url: str, out_dir: Path, notes: str = '',
                   sidecars: list | None = None, ui_version: str | None = None) -> dict:
    """扫描本次构建产物，生成 exe + UI 双包的 update.json（含 sha256）。

    ``ui_version`` 单独给界面包定版：版本按日期比，同一天里第二次发界面包必须抬一位
    （如 ``2026.10.05.2``），否则当天已经吃到覆盖层的客户端因为清单的 ``ui_version``
    不大于本地 ``overlay_version``，永远不会再下这一份。清单与包内 ``build_info.json``
    必须写同一个值，写歪了会让客户端每轮都重复下载。

    返回 ``{'manifest': {...可发布的内容…}, 'files': {variant: {'exe': Path, 'ui': Path}}}``;
    本地路径只出现在 files 里，不会混进 update.json。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = base_url.rstrip('/')
    variants = {}
    files = {}
    for sidecar_path in (sidecars if sidecars is not None else iter_sidecars()):
        try:
            info = json.loads(sidecar_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        variant = info.get('variant') or sidecar_path.stem
        version = info.get('version') or ''
        exe = REPO / info.get('exe', '')
        if not variant or not version or not exe.is_file():
            print(f'[skip] {sidecar_path.name}: 找不到产物 {exe}')
            continue
        if variant not in VARIANT_UI_SOURCES and not (info.get('ui_templates') and info.get('ui_static')):
            print(f'[skip] {variant}: 不知道它的界面资源在哪，请补 VARIANT_UI_SOURCES')
            continue

        exe_sha = sha256_file(exe)
        zip_version = ui_version or version
        ui_zip = make_ui_zip(variant, zip_version, out_dir / f'{variant}_ui.zip')
        ui_sha = sha256_file(ui_zip)

        variants[variant] = {
            'version': version,
            'exe_url': f'{base}/{exe.name}',
            'exe_sha256': exe_sha,
            'exe_size': exe.stat().st_size,
            'ui_version': zip_version,
            'ui_url': f'{base}/{ui_zip.name}',
            'ui_sha256': ui_sha,
            'ui_size': ui_zip.stat().st_size,
            'notes': notes,
        }
        files[variant] = {'exe': exe, 'ui': ui_zip}
        print(f'[ok] {variant}: v{version} exe={exe.stat().st_size / 1048576:.1f}MB '
              f'ui={ui_zip.stat().st_size / 1024:.0f}KB')

    manifest = {
        'schema': 1,
        'updated_at': _dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'variants': variants,
    }
    (out_dir / 'update.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'manifest': manifest, 'files': files}
