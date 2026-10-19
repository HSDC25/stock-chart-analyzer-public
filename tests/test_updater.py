"""Auto-update tests: version compare, manifest/URL trust, download integrity,
zip-slip defence, UI-overlay activation, and the /api/update/* routes.

A real HTTP server bound to 127.0.0.1 stands in for data.hsdc.cloud (the updater
allows plain HTTP on loopback only, which is the sanctioned test hook).
"""
import hashlib
import json
import os
import sys
import threading
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common import updater  # noqa: E402
from common.version import is_newer, version_key  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """把 DATA_DIR 关进 tmp：overlay/下载都不许碰真实的 ~/Documents/StockChart。"""
    monkeypatch.setattr(updater, 'DATA_DIR', tmp_path / 'StockChart')
    monkeypatch.delenv('STOCKCHART_UI_OVERLAY', raising=False)
    monkeypatch.delenv('STOCKCHART_OVERLAY_ACTIVE', raising=False)
    monkeypatch.delenv('STOCKCHART_DISABLE_AUTO_UPDATE', raising=False)
    # 活应用登记是模块级状态，留着上一轮的 Flask 实例会让别的测试走热切换分支
    monkeypatch.setattr(updater, '_live_app', None)
    monkeypatch.setattr(updater, '_live_app_template_subdir', 'templates')
    # _state 也是模块级的：上一轮留下的 staged_exe 会让 /api/update/install 拿到
    # 一个还在磁盘上的临时包，从而走到完全不同的分支
    monkeypatch.setattr(updater, '_state', dict(updater._state, phase='idle',
                                                staged_exe='', restart_required=False))


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------

def test_version_key_and_is_newer():
    assert version_key('2026.10.04') == (2026, 10, 4)
    assert is_newer('2026.10.05', '2026.10.04') is True
    assert is_newer('2026.10.04', '2026.10.04') is False
    assert is_newer('2026.9.30', '2026.10.01') is False
    assert is_newer('2027.01.01', '2026.12.31') is True
    assert is_newer('garbage', '2026.10.04') is False
    assert is_newer(None, '2026.10.04') is False


# ---------------------------------------------------------------------------
# trust / manifest lookup
# ---------------------------------------------------------------------------

def test_only_https_is_trusted():
    assert updater.is_trusted_url('https://data.hsdc.cloud/update.json')
    assert updater.is_trusted_url('http://127.0.0.1:8000/update.json')   # 测试钩子
    assert not updater.is_trusted_url('http://203.0.113.10/stockdata/update.json')
    assert not updater.is_trusted_url('http://data.hsdc.cloud/update.json')
    assert not updater.is_trusted_url('ftp://data.hsdc.cloud/update.json')
    # 主机白名单：只认发布主机，任意 HTTPS 主机不能再下发载荷
    assert not updater.is_trusted_url('https://evil.example/a.exe')
    assert not updater.is_trusted_url('https://data.hsdc.cloud.evil.example/update.json')


def test_variant_entry_matches_case_and_suffix():
    manifest = {'variants': {'StockChart_Win7_64': {'version': '2026.10.04'}}}
    assert updater.variant_entry(manifest, 'stockchart_win7_64')
    assert updater.variant_entry(manifest, 'StockChart_Win7_64')
    assert updater.variant_entry(manifest, 'StockChart_Win11_Dark') is None


# ---------------------------------------------------------------------------
# fixture: local update server
# ---------------------------------------------------------------------------

@pytest.fixture
def update_server(tmp_path, monkeypatch):
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    class Handler(SimpleHTTPRequestHandler):
        def translate_path(self, path=''):
            return str(tmp_path / path.lstrip('/'))

        def log_message(self, *args):  # 静音
            pass

    srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setitem(os.environ, 'STOCKCHART_UPDATE_URL',
                        'http://127.0.0.1:{}/update.json'.format(port))
    yield tmp_path, port
    srv.shutdown()
    srv.server_close()


def write_overlay(base_dir, version, templates='bundled', static='body{}'):
    (base_dir / 'templates').mkdir(parents=True, exist_ok=True)
    (base_dir / 'static').mkdir(parents=True, exist_ok=True)
    (base_dir / 'templates' / 'index.html').write_text(templates, encoding='utf-8')
    (base_dir / 'static' / 'style.css').write_text(static, encoding='utf-8')
    (base_dir / 'build_info.json').write_text(
        json.dumps({'version': version, 'variant': 't'}), encoding='utf-8')


def make_frozen_app(tmp_path, name='hot'):
    """造一个已经绑定「EXE 内置」模板/静态目录的 Flask，模拟启动器里活着的那个 app。"""
    from flask import Flask, render_template

    frozen = tmp_path / 'frozen'
    (frozen / 'templates').mkdir(parents=True, exist_ok=True)
    (frozen / 'static').mkdir(parents=True, exist_ok=True)
    (frozen / 'templates' / 'index.html').write_text('FROZEN-OLD', encoding='utf-8')
    (frozen / 'static' / 'style.css').write_text('frozen{}', encoding='utf-8')

    app = Flask(name, template_folder=str(frozen / 'templates'),
                static_folder=str(frozen / 'static'))

    @app.route('/')
    def index():
        return render_template('index.html')

    return app, app.test_client()


# ---------------------------------------------------------------------------
# download integrity
# ---------------------------------------------------------------------------

def test_download_requires_sha256(update_server, tmp_path):
    base, port = update_server
    (base / 'ui.zip').write_bytes(b'new-ui-bytes')
    with pytest.raises(updater.UpdateError, match='sha256'):
        updater.download('http://127.0.0.1:{}/ui.zip'.format(port),
                         tmp_path / 'out.zip', '', 'UI')


def test_download_rejects_plain_http_for_code(tmp_path):
    with pytest.raises(updater.UpdateError):
        updater.download('http://203.0.113.10/a.exe', tmp_path / 'a.exe',
                         'deadbeef', '程序更新包')


def test_download_rejects_checksum_mismatch(update_server, tmp_path):
    base, port = update_server
    (base / 'ui.zip').write_bytes(b'content')
    dest = tmp_path / 'sub' / 'ui.zip'
    with pytest.raises(updater.UpdateError, match='sha256'):
        updater.download('http://127.0.0.1:{}/ui.zip'.format(port), dest,
                         '0' * 64, 'UI')
    assert not dest.exists()


def test_download_success_moves_file(update_server, tmp_path):
    base, port = update_server
    payload = b'hello-update-payload' * 100
    (base / 'ui.zip').write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    dest = tmp_path / 'ui.zip'
    updater.download('http://127.0.0.1:{}/ui.zip'.format(port), dest, sha, 'UI')
    assert dest.read_bytes() == payload
    assert not list(tmp_path.glob('*.part'))


# ---------------------------------------------------------------------------
# zip-slip
# ---------------------------------------------------------------------------

def test_safe_extract_rejects_traversal(tmp_path):
    evil = tmp_path / 'evil.zip'
    with zipfile.ZipFile(str(evil), 'w') as zf:
        zf.writestr('../escaped.txt', 'boom')
    with pytest.raises(updater.UpdateError):
        updater._safe_extract(zipfile.ZipFile(str(evil)), tmp_path / 'out')


def test_safe_extract_rejects_absolute(tmp_path):
    evil = tmp_path / 'evil2.zip'
    with zipfile.ZipFile(str(evil), 'w') as zf:
        zf.writestr('/etc/passwd', 'boom')
    with pytest.raises(updater.UpdateError):
        updater._safe_extract(zipfile.ZipFile(str(evil)), tmp_path / 'out')


def test_safe_extract_rejects_zip_bomb(tmp_path, monkeypatch):
    """解压上限：声明体积超限即拒绝，不必真的往磁盘写几百 MB。"""
    monkeypatch.setattr(updater, '_MAX_EXTRACT_BYTES', 1024)
    bomb = tmp_path / 'bomb.zip'
    with zipfile.ZipFile(str(bomb), 'w') as zf:
        zf.writestr('big.bin', b'\x00' * 4096)
    with pytest.raises(updater.UpdateError, match='体积'):
        updater._safe_extract(zipfile.ZipFile(str(bomb)), tmp_path / 'out_bomb')
    assert not (tmp_path / 'out_bomb' / 'big.bin').exists()


def test_safe_extract_rejects_too_many_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, '_MAX_EXTRACT_ENTRIES', 3)
    many = tmp_path / 'many.zip'
    with zipfile.ZipFile(str(many), 'w') as zf:
        for i in range(5):
            zf.writestr('f{}.txt'.format(i), 'x')
    with pytest.raises(updater.UpdateError, match='条目'):
        updater._safe_extract(zipfile.ZipFile(str(many)), tmp_path / 'out_many')


# ---------------------------------------------------------------------------
# Authenticode signature on EXE payloads
# ---------------------------------------------------------------------------

def test_exe_signature_rejects_unsigned_binary(tmp_path):
    if os.name != 'nt':
        pytest.skip('Authenticode 只在 Windows 上有意义')
    fake = tmp_path / 'fake.exe'
    fake.write_bytes(b'MZ' + b'\x00' * 128)
    with pytest.raises(updater.UpdateError, match='签名'):
        updater.verify_exe_signature(fake)


def test_exe_signature_accepts_trusted_system_binary():
    if os.name != 'nt':
        pytest.skip('Authenticode 只在 Windows 上有意义')
    # 系统自带的 explorer.exe 带内嵌 Authenticode 签名且签名者在本机受信任根中；
    # 注意 System32 下大量文件（where.exe/notepad.exe…）其实只有 catalog 签名，
    # WinVerifyTrust 按文件查不到，不适合当正例。
    candidates = [
        os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'explorer.exe'),
        r'C:\Program Files\Git\cmd\git.exe',
    ]
    for cand in candidates:
        if os.path.isfile(cand):
            updater.verify_exe_signature(cand)     # 不抛异常即通过
            return
    pytest.skip('本机找不到可用于正例的官方签名程序')


def test_signature_retries_with_legacy_struct_on_invalidarg(monkeypatch, tmp_path):
    """Win7 不认新版 WINTRUST_DATA 的额外字段，回 E_INVALIDARG 时必须换旧结构重试。"""
    calls = []

    def fake_verify(path, data_cls):
        calls.append(data_cls.__name__)
        return (updater._E_INVALIDARG, None) if len(calls) == 1 else (0, None)

    monkeypatch.setattr(updater, '_wintrust_verify', fake_verify)
    monkeypatch.setattr(updater.os, 'name', 'nt')     # 骗过平台判断，走真实分支
    updater.verify_exe_signature(tmp_path / 'x.exe')
    assert calls == ['_WINTRUST_DATA', '_WINTRUST_DATA_V1']


def test_signature_does_not_retry_when_new_struct_works(monkeypatch, tmp_path):
    calls = []

    def fake_verify(path, data_cls):
        calls.append(data_cls.__name__)
        return (0, None)

    monkeypatch.setattr(updater, '_wintrust_verify', fake_verify)
    monkeypatch.setattr(updater.os, 'name', 'nt')
    updater.verify_exe_signature(tmp_path / 'x.exe')
    assert calls == ['_WINTRUST_DATA']


def test_download_signature_flag_blocks_unsigned_exe(update_server, tmp_path):
    """download(verify_signature=True) 必须在本体落盘前挡住未签名载荷。"""
    if os.name != 'nt':
        pytest.skip('Authenticode 只在 Windows 上有意义')
    base, port = update_server
    payload = b'MZ' + b'\x00' * 256
    (base / 'a.exe').write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    # 落到子目录：update_server 的 base 就是 tmp_path，直接写 tmp_path/'a.exe'
    # 会与测试自己造的"服务端"文件撞名，断言就失去意义了
    dest = tmp_path / 'dl' / 'a.exe'
    with pytest.raises(updater.UpdateError, match='签名'):
        updater.download('http://127.0.0.1:{}/a.exe'.format(port), dest, sha,
                         '程序更新包', verify_signature=True)
    assert not dest.exists()


# ---------------------------------------------------------------------------
# 自签证书指纹白名单：目标机没装根证书时的第二条放行通道
# ---------------------------------------------------------------------------

_PROBE_DER = b'\x30\x82\x01\x00probe-signer-certificate-der'
_PROBE_FP = hashlib.sha256(_PROBE_DER).hexdigest()


def _pin_verify(monkeypatch, result, der):
    """把 _wintrust_verify 换成"信任链失败 + 固定签名者证书 DER"。"""
    monkeypatch.setattr(updater, '_wintrust_verify',
                        lambda path, data_cls: (result, der))
    monkeypatch.setattr(updater.os, 'name', 'nt')     # 骗过平台判断，走真实分支


def test_signature_accepts_pinned_signer_cert(monkeypatch, tmp_path):
    """系统信任链不过（自签不受信）但指纹命中白名单 -> 放行，更新链不能自己锁死。"""
    _pin_verify(monkeypatch, 0x800B0109, _PROBE_DER)       # CERT_E_UNTRUSTEDROOT
    monkeypatch.setenv('STOCKCHART_TRUSTED_CERT_SHA256', _PROBE_FP)
    updater.verify_exe_signature(tmp_path / 'x.exe')       # 不抛异常即通过


def test_signature_rejects_unpinned_signer_cert(monkeypatch, tmp_path):
    _pin_verify(monkeypatch, 0x800B0109, _PROBE_DER)
    monkeypatch.setenv('STOCKCHART_TRUSTED_CERT_SHA256', 'a' * 64)
    with pytest.raises(updater.UpdateError, match='指纹'):
        updater.verify_exe_signature(tmp_path / 'x.exe')


@pytest.mark.parametrize('code', [0x800B0100, 0x80096010])
def test_signature_never_pins_unsigned_or_tampered(monkeypatch, tmp_path, code):
    """未签名 / 摘要被篡改时白名单绝不能兜底，否则白名单就成了篡改后门。"""
    _pin_verify(monkeypatch, code, _PROBE_DER)
    monkeypatch.setenv('STOCKCHART_TRUSTED_CERT_SHA256', _PROBE_FP)
    with pytest.raises(updater.UpdateError, match='签名校验失败'):
        updater.verify_exe_signature(tmp_path / 'x.exe')


def test_signature_requires_nonempty_whitelist(monkeypatch, tmp_path):
    """白名单为空 = 只认系统信任链，不做无意义的指纹比对。"""
    _pin_verify(monkeypatch, 0x800B0109, _PROBE_DER)
    monkeypatch.delenv('STOCKCHART_TRUSTED_CERT_SHA256', raising=False)
    with pytest.raises(updater.UpdateError, match='签名校验失败'):
        updater.verify_exe_signature(tmp_path / 'x.exe')


def test_trusted_cert_sha256_merges_and_normalizes(monkeypatch):
    monkeypatch.setattr(updater, 'TRUSTED_CERT_SHA256', ('AA' * 32, ''))
    monkeypatch.setenv('STOCKCHART_TRUSTED_CERT_SHA256',
                       ' ' + 'BB' * 32 + ' , ' + 'CC' * 32 + ',')
    got = updater._trusted_cert_sha256()
    assert ('aa' * 32) in got       # 内置项，大写 -> 小写
    assert ('bb' * 32) in got       # env 首项，首尾空白被 strip
    assert ('cc' * 32) in got       # env 次项
    assert '' not in got            # 空项丢弃


def test_signer_cert_der_extracted_for_trusted_binary():
    """真实签名程序上证书 DER 必须取得到 —— 这是指纹兜底的前提条件。"""
    if os.name != 'nt':
        pytest.skip('Authenticode 只在 Windows 上有意义')
    candidates = [
        os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'explorer.exe'),
        r'C:\Program Files\Git\cmd\git.exe',
    ]
    for cand in candidates:
        if os.path.isfile(cand):
            result, der = updater._wintrust_verify(cand, updater._WINTRUST_DATA)
            assert result == 0
            assert isinstance(der, (bytes, bytearray)) and len(der) > 100
            assert len(hashlib.sha256(der).hexdigest()) == 64
            return
    pytest.skip('本机找不到可用于正例的官方签名程序')


# ---------------------------------------------------------------------------
# overlay activation
# ---------------------------------------------------------------------------

def test_overlay_dirs_ignored_in_source_run(monkeypatch):
    write_overlay(updater.overlay_dir(), '2999.01.01')
    assert updater.overlay_dirs() is None          # 源码运行默认不用覆盖层


def test_overlay_dirs_used_when_newer(monkeypatch):
    monkeypatch.setenv('STOCKCHART_UI_OVERLAY', '1')
    write_overlay(updater.overlay_dir(), '2999.01.01')
    tpl, stat = updater.overlay_dirs()
    assert os.path.isdir(tpl) and os.path.isfile(os.path.join(stat, 'style.css'))


def test_overlay_dirs_ignored_when_not_newer(monkeypatch):
    monkeypatch.setenv('STOCKCHART_UI_OVERLAY', '1')
    monkeypatch.setattr(updater, 'APP_VERSION', '2026.10.05')
    write_overlay(updater.overlay_dir(), '2026.10.04')   # 比本体旧 -> 用本体那份
    assert updater.overlay_dirs() is None


def test_use_overlay_flips_flask_folders(monkeypatch):
    from flask import Flask
    monkeypatch.setenv('STOCKCHART_UI_OVERLAY', '1')
    write_overlay(updater.overlay_dir(), '2999.01.01')
    app = Flask(__name__)
    assert updater.use_overlay(app) is True
    assert app.template_folder.endswith(os.path.join('ui', 'dev', 'templates'))
    assert os.environ['STOCKCHART_OVERLAY_ACTIVE'] == '1'


def test_repoint_overlay_switches_a_running_flask(tmp_path, monkeypatch):
    """不重启进程换新界面：template_folder、jinja_loader、jinja_env.cache 三处缺一处都是旧页面。"""
    monkeypatch.setenv('STOCKCHART_UI_OVERLAY', '1')
    app, client = make_frozen_app(tmp_path)
    assert client.get('/').get_data(as_text=True) == 'FROZEN-OLD'
    assert client.get('/static/style.css').get_data(as_text=True) == 'frozen{}'

    write_overlay(updater.overlay_dir(), '2999.01.01',
                  templates='OVERLAY-NEW', static='overlay{}')
    assert updater.repoint_overlay(app) is True
    assert client.get('/').get_data(as_text=True) == 'OVERLAY-NEW'
    assert client.get('/static/style.css').get_data(as_text=True) == 'overlay{}'
    assert updater.overlay_is_active() is True


def test_repoint_overlay_noop_without_usable_overlay(tmp_path, monkeypatch):
    from flask import Flask
    monkeypatch.setenv('STOCKCHART_UI_OVERLAY', '1')
    app, client = make_frozen_app(tmp_path)
    assert updater.repoint_overlay(app) is False
    assert client.get('/').get_data(as_text=True) == 'FROZEN-OLD'
    assert 'STOCKCHART_OVERLAY_ACTIVE' not in os.environ

    # 覆盖层比本体旧时同样不动（EXE 更新后内置那份已经够新）
    write_overlay(updater.overlay_dir(), '2020.01.01', templates='STALE')
    monkeypatch.setattr(updater, 'APP_VERSION', '2026.10.05')
    assert updater.repoint_overlay(Flask(__name__)) is False


def test_use_overlay_registers_app_for_hot_update(tmp_path, monkeypatch):
    """启动时哪怕还没有覆盖层，也要把 app 登记下来，否则第一次热更只能靠重启。"""
    monkeypatch.setenv('STOCKCHART_UI_OVERLAY', '1')
    app, client = make_frozen_app(tmp_path)
    assert updater.use_overlay(app) is False          # 此时没有覆盖层
    assert updater._live_app is app

    write_overlay(updater.overlay_dir(), '2999.01.01', templates='HOT-AFTER-STARTUP')
    assert updater.hot_repoint_overlay() is True
    assert client.get('/').get_data(as_text=True) == 'HOT-AFTER-STARTUP'


def test_hot_repoint_without_registered_app_is_false(monkeypatch):
    monkeypatch.setenv('STOCKCHART_UI_OVERLAY', '1')
    write_overlay(updater.overlay_dir(), '2999.01.01')
    assert updater._live_app is None
    assert updater.hot_repoint_overlay() is False


def test_hot_repoint_swallows_flask_incompatibility(monkeypatch, tmp_path):
    """Flask 内部结构对不上时退回重启路径，不能让更新线程把异常抛到日志外面。"""
    monkeypatch.setenv('STOCKCHART_UI_OVERLAY', '1')
    write_overlay(updater.overlay_dir(), '2999.01.01')

    class Broken(object):
        @property
        def jinja_env(self):
            raise RuntimeError('版本不匹配')

    updater.register_live_app(Broken())
    assert updater.hot_repoint_overlay() is False


def test_apply_ui_overlay_swaps_directory(tmp_path):
    overlay = updater.overlay_dir()
    overlay.mkdir(parents=True)
    write_overlay(overlay, '2020.01.01', templates='OLD')
    zip_path = tmp_path / 'ui.zip'
    with zipfile.ZipFile(str(zip_path), 'w') as zf:
        zf.writestr('build_info.json', json.dumps({'version': '2030.01.01', 'variant': 't'}))
        zf.writestr('templates/index.html', 'NEW')
        zf.writestr('static/style.css', 'body{color:red}')
    assert updater.apply_ui_overlay(zip_path) == 'applied'
    assert (overlay / 'templates' / 'index.html').read_text(encoding='utf-8') == 'NEW'
    assert not (overlay.parent / (overlay.name + '.old')).exists()


# ---------------------------------------------------------------------------
# plan / end-to-end update
# ---------------------------------------------------------------------------

def test_plan_update_detects_exe_and_ui(update_server, monkeypatch):
    base, port = update_server
    monkeypatch.setattr(updater, 'APP_VERSION', '2020.01.01')
    monkeypatch.setattr(updater, 'VARIANT', 'StockChart_Win11_Dark')
    (base / 'update.json').write_text(json.dumps({'variants': {
        'StockChart_Win11_Dark': {
            'version': '2030.01.01', 'exe_url': 'http://127.0.0.1:{}/a.exe'.format(port),
            'exe_sha256': 'x' * 64, 'ui_version': '2030.01.01',
            'ui_url': 'http://127.0.0.1:{}/ui.zip'.format(port), 'ui_sha256': 'y' * 64,
        }}}), encoding='utf-8')
    plan = updater.plan_update(updater.fetch_manifest())
    assert plan['exe'] and plan['ui']


def test_run_update_installs_ui_only(update_server, monkeypatch):
    base, port = update_server
    with zipfile.ZipFile(str(base / 'ui.zip'), 'w') as zf:
        zf.writestr('build_info.json', json.dumps({'version': '2030.01.01', 'variant': 't'}))
        zf.writestr('templates/index.html', 'HOT')
        zf.writestr('static/a.css', 'a{}')
    ui_sha = hashlib.sha256((base / 'ui.zip').read_bytes()).hexdigest()
    (base / 'update.json').write_text(json.dumps({'variants': {
        'StockChart_Win11_Dark': {
            'version': '2020.01.01',            # 与本体同版本 -> 不动 exe
            'ui_version': '2030.01.01',
            'ui_url': 'http://127.0.0.1:{}/ui.zip'.format(port),
            'ui_sha256': ui_sha,
            'notes': '界面小修'}}}), encoding='utf-8')
    monkeypatch.setattr(updater, 'APP_VERSION', '2020.01.01')
    monkeypatch.setattr(updater, 'VARIANT', 'StockChart_Win11_Dark')

    result = updater.run_update(auto_apply=True, restart=False)
    assert result['updated'] and result['kind'] == 'ui'
    assert result['restart_required'] is True          # 没有登记活着的 app，只能靠重启
    index = updater.overlay_dir() / 'templates' / 'index.html'
    assert index.read_text(encoding='utf-8') == 'HOT'
    st = updater.status()
    assert st['phase'] == 'done' and st['remote_version'] == '2020.01.01'
    assert not list((updater.DATA_DIR / 'updates').glob('stockchart_update_*'))


def test_run_update_hot_repoints_live_app_without_restart(update_server, monkeypatch, tmp_path):
    """win7 浏览器版的使用者是老人：界面热更必须原地生效，不能重启进程、不能换端口。"""
    base, port = update_server
    with zipfile.ZipFile(str(base / 'ui.zip'), 'w') as zf:
        zf.writestr('build_info.json', json.dumps({'version': '2030.01.01', 'variant': 't'}))
        zf.writestr('templates/index.html', 'HOT-PAGE')
        zf.writestr('static/style.css', 'hot{}')
    sha = hashlib.sha256((base / 'ui.zip').read_bytes()).hexdigest()
    (base / 'update.json').write_text(json.dumps({'variants': {
        'StockChart_Win7_64_browser': {
            'version': '2020.01.01',            # 本体同版 -> 只热更界面
            'ui_version': '2030.01.01',
            'ui_url': 'http://127.0.0.1:{}/ui.zip'.format(port),
            'ui_sha256': sha,
        }}}), encoding='utf-8')
    monkeypatch.setattr(updater, 'APP_VERSION', '2020.01.01')
    monkeypatch.setattr(updater, 'VARIANT', 'StockChart_Win7_64_browser')
    monkeypatch.setenv('STOCKCHART_UI_OVERLAY', '1')

    app, client = make_frozen_app(tmp_path)
    updater.register_live_app(app, 'templates')

    def no_restart(*args, **kwargs):
        raise AssertionError('热切换成功后不该再安排重启')
    monkeypatch.setattr(updater, 'schedule_restart', no_restart)

    result = updater.run_update(auto_apply=True, restart=True)
    assert result['updated'] is True and result['kind'] == 'ui'
    assert result['hot'] is True and result['restart_required'] is False
    assert client.get('/').get_data(as_text=True) == 'HOT-PAGE'
    assert client.get('/static/style.css').get_data(as_text=True) == 'hot{}'

    st = updater.status()
    assert st['phase'] == 'done' and st['restart_required'] is False
    assert '刷新即生效' in st['message']
    assert updater.overlay_is_active() is True
    assert not list((updater.DATA_DIR / 'updates').glob('stockchart_update_*'))


def test_exe_update_leaves_the_serving_overlay_alone(update_server, monkeypatch, tmp_path):
    """换本体时不许删覆盖层：进程很可能正在用它渲染，删掉到退出前每个页面都是 500。"""
    import shutil as _shutil

    base, port = update_server
    (base / 'update.json').write_text(json.dumps({'variants': {
        'StockChart_Win11_HighEnd': {
            'version': '2030.01.01',
            'exe_url': 'http://127.0.0.1:{}/a.exe'.format(port),
            'exe_sha256': '0' * 64,
        }}}), encoding='utf-8')
    monkeypatch.setattr(updater, 'APP_VERSION', '2020.01.01')
    monkeypatch.setattr(updater, 'VARIANT', 'StockChart_Win11_HighEnd')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    fake_exe = tmp_path / 'bin' / 'StockChart_Win11_HighEnd.exe'
    fake_exe.parent.mkdir(parents=True, exist_ok=True)
    fake_exe.write_bytes(b'old-exe')
    monkeypatch.setattr(sys, 'executable', str(fake_exe))
    monkeypatch.setattr(updater, '_writable_exe_dir', lambda p: True)

    payload = tmp_path / 'a.exe'
    payload.write_bytes(b'new-exe')

    def fake_download(url, dest, sha, label, cancel=None, **kwargs):
        _shutil.copy2(str(payload), str(dest))

    restarted = []
    monkeypatch.setattr(updater, 'download', fake_download)
    monkeypatch.setattr(updater, 'schedule_restart', lambda new_exe='': restarted.append(new_exe))

    write_overlay(updater.overlay_dir(), '2020.01.01', templates='SERVING')
    result = updater.run_update(auto_apply=True, restart=True)
    assert result['kind'] == 'exe' and restarted and result['restart_required'] is True
    index = updater.overlay_dir() / 'templates' / 'index.html'
    assert index.read_text(encoding='utf-8') == 'SERVING'


def test_run_update_refuses_unsigned_exe_end_to_end(update_server, monkeypatch, tmp_path):
    """真实下载路径：清单摘要对得上但 EXE 没官方签名时，必须整单失败且不动旧本体。"""
    if os.name != 'nt':
        pytest.skip('Authenticode 只在 Windows 上有意义')
    base, port = update_server
    payload = b'MZ' + b'\x00' * 256                     # 冒充 EXE，摘要真实但无签名
    (base / 'a.exe').write_bytes(payload)
    (base / 'update.json').write_text(json.dumps({'variants': {
        'StockChart_Win11_HighEnd': {
            'version': '2030.01.01',
            'exe_url': 'http://127.0.0.1:{}/a.exe'.format(port),
            'exe_sha256': hashlib.sha256(payload).hexdigest(),
        }}}), encoding='utf-8')
    monkeypatch.setattr(updater, 'APP_VERSION', '2020.01.01')
    monkeypatch.setattr(updater, 'VARIANT', 'StockChart_Win11_HighEnd')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    fake_exe = tmp_path / 'bin' / 'StockChart_Win11_HighEnd.exe'
    fake_exe.parent.mkdir(parents=True, exist_ok=True)
    fake_exe.write_bytes(b'old-exe')
    monkeypatch.setattr(sys, 'executable', str(fake_exe))
    monkeypatch.setattr(updater, '_writable_exe_dir', lambda p: True)

    result = updater.run_update(auto_apply=True, restart=True)
    assert result.get('updated') is False
    assert result.get('phase') == 'failed'
    assert '签名' in str(result.get('error') or '')
    assert fake_exe.read_bytes() == b'old-exe'          # 旧本体必须原封不动


def test_cleanup_leftovers_drops_only_the_stale_overlay(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    fake_exe = tmp_path / 'StockChart_Win7_64.exe'
    fake_exe.write_bytes(b'exe')
    monkeypatch.setattr(sys, 'executable', str(fake_exe))
    monkeypatch.setattr(updater, 'APP_VERSION', '2026.10.20')

    # 换体之后内置界面更新了，覆盖层没人再读 -> 回收，连同 apply 失败留下的 .old/.new
    write_overlay(updater.overlay_dir(), '2026.10.19', templates='STALE')
    (updater.overlay_dir().parent / (updater.overlay_dir().name + '.old')).mkdir()
    updater.cleanup_leftovers()
    assert not updater.overlay_dir().exists()
    assert not (updater.overlay_dir().parent / (updater.overlay_dir().name + '.old')).exists()

    # 仍然比本体新的覆盖层必须留着，那正是当前在服务的那份
    write_overlay(updater.overlay_dir(), '2026.10.21', templates='FRESH')
    updater.cleanup_leftovers()
    assert (updater.overlay_dir() / 'templates' / 'index.html'
            ).read_text(encoding='utf-8') == 'FRESH'


def test_run_update_rejects_missing_manifest_entry(update_server, monkeypatch):
    base, port = update_server
    (base / 'update.json').write_text(json.dumps({'variants': {}}), encoding='utf-8')
    monkeypatch.setattr(updater, 'VARIANT', 'StockChart_Nobody')
    result = updater.run_update(auto_apply=True, restart=False)
    assert result['updated'] is False and '没有本变体' in result['error']


def test_run_update_fails_when_exe_dir_not_writable(update_server, monkeypatch, tmp_path):
    base, port = update_server
    (base / 'update.json').write_text(json.dumps({'variants': {
        'StockChart_Win11_Dark': {'version': '2030.01.01',
                                  'exe_url': 'https://data.hsdc.cloud/updates/a.exe',
                                  'exe_sha256': 'z'}}}), encoding='utf-8')
    monkeypatch.setattr(updater, 'APP_VERSION', '2020.01.01')
    monkeypatch.setattr(updater, 'VARIANT', 'StockChart_Win11_Dark')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    fake_exe = tmp_path / 'bin' / 'StockChart_Win11_Dark.exe'
    fake_exe.parent.mkdir(parents=True, exist_ok=True)
    fake_exe.write_bytes(b'exe')
    monkeypatch.setattr(sys, 'executable', str(fake_exe))
    monkeypatch.setattr(updater, '_writable_exe_dir', lambda exe: False)

    result = updater.run_update(auto_apply=True, restart=False)
    assert result['updated'] is False and result['phase'] == 'failed'
    assert '不可写' in updater.status()['error']


# ---------------------------------------------------------------------------
# restart script
# ---------------------------------------------------------------------------

def test_restart_script_waits_then_swaps():
    script = updater._RESTART_BAT.replace('%PLACEHOLDER%', updater.PLACEHOLDER)
    assert script.encode('ascii')                          # 纯 ASCII，不受代码页影响
    assert 'tasklist /FI "PID eq %MYPID%"' in script       # 等旧进程退出
    assert 'move /Y "%EXE%" "%OLD%"' in script             # 改名而非覆盖（运行中不可覆盖）
    assert 'copy /Y "%NEW%" "%EXE%"' in script
    assert 'start "" "%EXE%"' in script                     # 拉起新版本
    assert updater.PLACEHOLDER == '-'


@pytest.mark.skipif(os.name != 'nt', reason='换体脚本是 Windows 批处理')
def test_launch_bat_really_swaps_the_exe(tmp_path):
    """真跑一次脱离进程的换体脚本。

    只看脚本文本不够：DETACHED_PROCESS 拉起的 cmd 没有控制台，执行到第一处 ``>nul``
    就中断整个批处理，本体永远换不掉（2026-10-05 实测，改用 CREATE_NO_WINDOW 后通过）。
    """
    import shutil
    import time

    sys32 = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32')
    app = tmp_path / 'StockChart_Fake.exe'
    new = tmp_path / 'new.exe'
    shutil.copy2(os.path.join(sys32, 'where.exe'), str(app))
    shutil.copy2(os.path.join(sys32, 'whoami.exe'), str(new))
    assert app.stat().st_size != new.stat().st_size
    new_size = new.stat().st_size

    # PID 用不会存在的号，脚本直接走到换体分支
    bat = updater._launch_bat(app.parent, 999999, 999998, app, str(new))
    deadline = time.time() + 60
    while time.time() < deadline:
        if app.exists() and app.stat().st_size == new_size and not bat.exists():
            break
        time.sleep(0.5)

    assert app.stat().st_size == new_size                # 本体已被新包替换
    assert (tmp_path / 'StockChart_Fake.exe.old').exists()  # 旧包改名留待下次启动清理
    assert not new.exists()                              # 临时包用完即删
    assert not bat.exists()                              # 脚本自删除


def test_auto_update_disabled_by_env(monkeypatch):
    monkeypatch.setenv('STOCKCHART_DISABLE_AUTO_UPDATE', '1')
    updater._worker = None
    assert updater.start_auto_update() is None
    assert updater.status()['phase'] == 'idle'


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@pytest.fixture
def flask_client():
    from flask import Flask, jsonify
    from common.routes.update_routes import update_bp

    app = Flask(__name__)
    app.register_blueprint(update_bp)

    @app.route('/api/ping')
    def ping():
        return jsonify({'ok': True})

    return app.test_client()


def test_update_status_route(flask_client):
    body = flask_client.get('/api/update/status').get_json()
    assert body['success'] is True
    assert body['local_version']
    assert 'variant' in body and 'overlay_active' in body


def test_update_install_route_without_staged_package(flask_client):
    resp = flask_client.post('/api/update/install')
    assert resp.status_code == 400
    assert '待安装' in resp.get_json()['error']


# ---------------------------------------------------------------------------
# 端到端：发布脚本打的 UI 包 → 覆盖层 → 真实页面渲染出热更新内容
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_repo(tmp_path):
    """把 dark 主题的 templates/static 和共享 assets/js 复制到临时"仓库"，
    并在 index.html 里插一个标记，模拟一次界面改动。"""
    import shutil
    theme = os.path.join(ROOT, 'win11', 'themes', 'dark')
    repo = tmp_path / 'repo'
    for src, dst in ((os.path.join(theme, 'templates'), 'win11/themes/dark/templates'),
                     (os.path.join(theme, 'static'), 'win11/themes/dark/static'),
                     (os.path.join(ROOT, 'assets', 'js'), 'assets/js')):
        shutil.copytree(src, str(repo / dst))
    index = repo / 'win11' / 'themes' / 'dark' / 'templates' / 'index.html'
    html = index.read_text(encoding='utf-8')
    index.write_text(html.replace('</body>', '<div id="e2e-hot-marker">HOT</div></body>', 1),
                     encoding='utf-8')
    return repo


def test_publish_ui_zip_to_hot_overlay_end_to_end(update_server, fake_repo, monkeypatch):
    sys.path.insert(0, os.path.join(ROOT, 'scripts'))
    try:
        import update_lib
    finally:
        sys.path.remove(os.path.join(ROOT, 'scripts'))
    base, port = update_server

    monkeypatch.setattr(update_lib, 'REPO', fake_repo)
    zip_path = update_lib.make_ui_zip('StockChart_Win11_Dark', '2999.12.31', base / 'ui.zip')
    names = zipfile.ZipFile(str(zip_path)).namelist()
    assert 'templates/index.html' in names and 'assets/js/update.js' in names

    (base / 'update.json').write_text(json.dumps({'variants': {
        'StockChart_Win11_Dark': {
            'version': '2020.01.01',           # exe 与本地同版 -> 只热更界面
            'ui_version': '2999.12.31',
            'ui_url': 'http://127.0.0.1:{}/ui.zip'.format(port),
            'ui_sha256': hashlib.sha256(zip_path.read_bytes()).hexdigest(),
        }}}), encoding='utf-8')
    monkeypatch.setattr(updater, 'APP_VERSION', '2020.01.01')
    monkeypatch.setattr(updater, 'VARIANT', 'StockChart_Win11_Dark')
    monkeypatch.setenv('STOCKCHART_UI_OVERLAY', '1')

    result = updater.run_update(auto_apply=True, restart=False)
    assert result['updated'] is True and result['kind'] == 'ui'

    # 用与主题相同的模板/静态目录建一个最小 app，验证启动器那行 use_overlay 的效果
    from flask import Flask, render_template
    from common.routes.assets_routes import assets_bp
    from common.routes.update_routes import update_bp

    theme = os.path.join(ROOT, 'win11', 'themes', 'dark')
    app = Flask('e2e', template_folder=os.path.join(theme, 'templates'),
                static_folder=os.path.join(theme, 'static'))
    app.register_blueprint(assets_bp)
    app.register_blueprint(update_bp)

    @app.route('/')
    def index():
        return render_template('index.html')

    assert updater.use_overlay(app, 'templates') is True
    client = app.test_client()

    page = client.get('/').get_data(as_text=True)
    assert 'e2e-hot-marker' in page                      # 页面来自覆盖层而非内置副本

    js = client.get('/assets/js/update.js')
    assert js.status_code == 200 and 'sc-update-badge' in js.get_data(as_text=True)
    # 目录穿越拿不到仓库源码
    assert client.get('/assets/js/..%2f..%2f..%2fcommon%2fconfig.py').status_code in (403, 404)

    body = client.get('/api/update/status').get_json()
    assert body['overlay_active'] is True
    assert body['overlay_version'] == '2999.12.31'


def test_build_manifest_ui_version_lands_in_both_zip_and_list(tmp_path, fake_repo, monkeypatch):
    """清单里的 ui_version 必须与包内 build_info 同一个值。

    写歪了客户端就会每 30 秒重复下载同一份 zip：它按清单判定有更新，装完读到的却还是旧值。
    """
    sys.path.insert(0, os.path.join(ROOT, 'scripts'))
    try:
        import update_lib
    finally:
        sys.path.remove(os.path.join(ROOT, 'scripts'))
    monkeypatch.setattr(update_lib, 'REPO', fake_repo)

    exe = fake_repo / 'win11' / 'dist' / 'StockChart_Win11_Dark_v2026.10.05.exe'
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b'fake-exe-bytes')
    sidecar = tmp_path / 'StockChart_Win11_Dark.json'
    sidecar.write_text(json.dumps({
        'variant': 'StockChart_Win11_Dark', 'version': '2026.10.05',
        'exe': 'win11/dist/StockChart_Win11_Dark_v2026.10.05.exe'}), encoding='utf-8')

    out = tmp_path / 'update_dist'
    res = update_lib.build_manifest('https://data.hsdc.cloud/updates', out, '说明',
                                    [sidecar], ui_version='2026.10.05.2')
    entry = res['manifest']['variants']['StockChart_Win11_Dark']
    assert entry['version'] == '2026.10.05'          # 本体版本不受界面包定版影响
    assert entry['ui_version'] == '2026.10.05.2'
    with zipfile.ZipFile(str(out / 'StockChart_Win11_Dark_ui.zip')) as zf:
        assert json.loads(zf.read('build_info.json'))['version'] == '2026.10.05.2'

    # 不抬版时保持原行为：两者都跟构建号一致
    res = update_lib.build_manifest('https://data.hsdc.cloud/updates', out, '', [sidecar])
    entry = res['manifest']['variants']['StockChart_Win11_Dark']
    with zipfile.ZipFile(str(out / 'StockChart_Win11_Dark_ui.zip')) as zf:
        assert entry['ui_version'] == json.loads(zf.read('build_info.json'))['version'] == '2026.10.05'


def test_same_day_ui_republish_needs_a_bumped_ui_version(update_server, monkeypatch, tmp_path):
    """同日重发界面包：清单 ui_version 不抬位，客户端就永远拿不到新 zip。

    客户端拿清单的 ui_version 与本地 overlay_version 比大小；抬一位（2026.10.05.2）才
    会重新下载，而包内 build_info 必须写抬过后的那个值，否则装完再比仍然算"更新"，
    每轮都会重复下同一个包。
    """
    import shutil as _shutil

    base, port = update_server
    monkeypatch.setattr(updater, 'VARIANT', 'StockChart_Win11_Dark')
    monkeypatch.setattr(updater, 'APP_VERSION', '2026.10.04')
    monkeypatch.setenv('STOCKCHART_UI_OVERLAY', '1')
    write_overlay(updater.overlay_dir(), '2026.10.05', templates='TODAY')

    def publish(ui_version):
        name = 'ui_{}.zip'.format(ui_version)
        with zipfile.ZipFile(str(base / name), 'w') as zf:
            zf.writestr('build_info.json', json.dumps(
                {'version': ui_version, 'variant': 'StockChart_Win11_Dark'}))
            zf.writestr('templates/index.html', 'REPUBLISHED')
            zf.writestr('static/style.css', 'republished{}')
        raw = (base / name).read_bytes()
        (base / 'update.json').write_text(json.dumps({'variants': {
            'StockChart_Win11_Dark': {
                'version': '2026.10.04',
                'ui_version': ui_version,
                'ui_url': 'http://127.0.0.1:{}/{}'.format(port, name),
                'ui_sha256': hashlib.sha256(raw).hexdigest(),
            }}}), encoding='utf-8')

    # 同日同版：判定"已是最新"，那份新 zip 根本不会被下载
    publish('2026.10.05')
    assert updater.run_update(auto_apply=True, restart=False)['updated'] is False

    # 抬一位：拿到新界面，且装完后的 overlay_version 与清单一致 -> 不再重复下载
    publish('2026.10.05.2')
    result = updater.run_update(auto_apply=True, restart=False)
    assert result['updated'] is True and result['kind'] == 'ui'
    index = updater.overlay_dir() / 'templates' / 'index.html'
    assert index.read_text(encoding='utf-8') == 'REPUBLISHED'
    assert updater.overlay_version() == '2026.10.05.2'
    assert updater.plan_update()['ui'] is False
