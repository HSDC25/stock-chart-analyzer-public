"""构建期与发布期共用的更新工具：版本信息注入、UI 包打包、update.json 生成。

被 ``win7/build_win7.py``、``win11/build_win11_webview.py`` 和
``scripts/publish_update.py`` 复用，三处对版本格式与清单字段的理解必须一致。

版本格式是构建日期 ``YYYY.MM.DD``（与 ``common/version.py`` 的比较规则配套）。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
import subprocess  # nosec B404 - 固定调用 powershell 做 CMS 签名
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
    """构建版本：默认当天日期；同一天发第二次 EXE 时用环境变量抬成四段。"""
    override = os.environ.get('STOCKCHART_BUILD_VERSION', '').strip()
    if override:
        return override
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


DEFAULT_SIGN_SUBJECT = 'CN=HSDC StockChart Code Signing, O=HSDC'

# 用 .NET 的 SignedCms 生成 detached PKCS#7：zip 不是 PE，signtool 签不了。
# 私钥优先取用户证书存储（与 sign_executables.ps1 一致），没有则回落到 PFX
# （路径/密码走环境变量，避免把口令写进仓库）。
_SIGN_PS = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Security
$cert = $null
if ($env:SC_UI_PFX -and (Test-Path $env:SC_UI_PFX)) {
    $sec = ConvertTo-SecureString -String $env:SC_UI_PFX_PASSWORD -AsPlainText -Force
    $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($env:SC_UI_PFX, $sec)
} else {
    $cert = Get-ChildItem Cert:\CurrentUser\My |
            Where-Object {
                $_.Subject -eq $env:SC_UI_SUBJECT -and
                $_.HasPrivateKey -and
                ($_.EnhancedKeyUsageList.ObjectId -contains '1.3.6.1.5.5.7.3.3')
            } |
            Sort-Object NotAfter -Descending | Select-Object -First 1
}
if (-not $cert) { throw "找不到代码签名证书 $($env:SC_UI_SUBJECT)" }
$bytes = [IO.File]::ReadAllBytes($env:SC_UI_PATH)
$content = New-Object System.Security.Cryptography.Pkcs.ContentInfo -ArgumentList @(,$bytes)
$cms = New-Object System.Security.Cryptography.Pkcs.SignedCms($content, $true)
$signer = New-Object System.Security.Cryptography.Pkcs.CmsSigner($cert)
$signer.IncludeOption = [System.Security.Cryptography.X509Certificates.X509IncludeOption]::EndCertOnly
$cms.ComputeSignature($signer)
[Convert]::ToBase64String($cms.Encode())
'''


def _powershell_exe() -> str:
    """优先 pwsh 7（项目默认），没有才退回 Windows PowerShell。"""
    for name in ('pwsh', 'powershell'):
        found = shutil.which(name)
        if found:
            return found
    return 'powershell'


def sign_ui_payload(path: Path, cert_subject: str = DEFAULT_SIGN_SUBJECT,
                    timeout: int = 120) -> str:
    """对 UI 包做 detached PKCS#7 签名，返回 base64；失败时抛 RuntimeError。

    客户端的 ``verify_detached_signature`` 会校验摘要并把签名者证书指纹钉到
    ``common/updater.py`` 的白名单上，两边必须用同一张证书。
    """
    if os.name != 'nt':
        raise RuntimeError('界面包签名依赖 Windows 证书存储，当前平台不可用')
    env = dict(os.environ)
    env['SC_UI_PATH'] = str(Path(path).resolve())
    env['SC_UI_SUBJECT'] = cert_subject
    pfx = env.get('STOCKCHART_SIGN_PFX') or ''
    if pfx:
        env['SC_UI_PFX'] = pfx
        env['SC_UI_PFX_PASSWORD'] = env.get('STOCKCHART_SIGN_PFX_PASSWORD', '')
    proc = subprocess.run(  # nosec B603 - 固定参数，无可注入片段
        [_powershell_exe(), '-NoProfile', '-NonInteractive',
         '-ExecutionPolicy', 'Bypass', '-Command', _SIGN_PS],
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    out = (proc.stdout or '').strip()
    if proc.returncode != 0 or not out:
        raise RuntimeError((proc.stderr or out or '签名命令失败').strip())
    # PowerShell 可能带回 BOM/换行，压成单行 base64
    return ''.join(out.split())


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
        # 界面包必须带官方签名，否则新版客户端会拒收（zip 做不了 Authenticode）
        try:
            ui_sig = sign_ui_payload(ui_zip)
        except Exception as exc:
            ui_sig = ''
            print(f'[WARN] {variant}: 界面包签名失败（{exc}）；'
                  f'这份清单到了新版客户端会被拒绝做界面热更，请先修好证书再发布')

        variants[variant] = {
            'version': version,
            'exe_url': f'{base}/{exe.name}',
            'exe_sha256': exe_sha,
            'exe_size': exe.stat().st_size,
            'ui_version': zip_version,
            'ui_url': f'{base}/{ui_zip.name}',
            'ui_sha256': ui_sha,
            'ui_size': ui_zip.stat().st_size,
            'ui_sig': ui_sig,
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
