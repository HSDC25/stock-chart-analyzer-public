"""打包与装配一致性回归测试（M9）。

两件事最容易在"改一处漏一处"上翻车：

1. PyInstaller 用 hiddenimports 白名单收 ``common.*``，新增模块忘了同步，
   打出来的 exe 启动时 ModuleNotFoundError（源码跑测试全绿也发现不了）。
2. win7/win11 两个 app.py 各抄一份装配逻辑与惰性导出表，改一边漏一边。

这里把两条都钉住。
"""
import glob
import io
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _spec_files():
    return sorted(glob.glob(os.path.join(ROOT, 'win11', '*.spec')) +
                  glob.glob(os.path.join(ROOT, 'win7', '*.spec')))


def test_specs_are_discovered():
    assert _spec_files(), '没找到任何 .spec，测试前提不成立'


@pytest.mark.parametrize('spec', _spec_files(), ids=lambda p: os.path.basename(p))
def test_every_spec_collects_app_factory(spec):
    text = io.open(spec, encoding='utf-8').read()
    assert "'common.app_factory'" in text, (
        '%s 的 hiddenimports 缺 common.app_factory：打出来的 exe 会在启动时报 '
        'ModuleNotFoundError' % os.path.basename(spec))


def test_build_scripts_collect_app_factory():
    win11 = io.open(os.path.join(ROOT, 'win11', 'build_win11_webview.py'),
                    encoding='utf-8').read()
    win7 = io.open(os.path.join(ROOT, 'win7', 'build_win7.py'), encoding='utf-8').read()
    assert 'common.app_factory' in win11
    assert 'common.app_factory' in win7


@pytest.mark.parametrize('variant', ['win7', 'win11'])
def test_entry_points_delegate_to_factory(variant):
    text = io.open(os.path.join(ROOT, variant, 'app.py'), encoding='utf-8').read()
    assert 'from common.app_factory import' in text
    assert 'create_app(' in text
    # 不许再各自维护一份蓝图注册/惰性导出表
    assert '_LAZY_HEAVY_EXPORTS' not in text
    assert 'register_blueprint' not in text


def test_factory_registers_all_blueprints_and_lazy_exports():
    from common.app_factory import BLUEPRINT_MODULES, LAZY_HEAVY_EXPORTS

    assert set(BLUEPRINT_MODULES) == {
        'api_routes', 'screen_routes', 'ml_routes',
        'chart_routes', 'assets_routes', 'update_routes',
    }
    # 路由通过 ``from app import <名字>`` 取的东西必须都在导出表里
    route_sources = glob.glob(os.path.join(ROOT, 'common', 'routes', '*.py'))
    wanted = set()
    for path in route_sources:
        text = io.open(path, encoding='utf-8').read()
        wanted.update(re.findall(r'from app import ([A-Za-z_][\w]*)', text))
        for group in re.findall(r'from app import ([^#\n]+)', text):
            wanted.update(part.strip() for part in group.split(',') if part.strip())
    assert wanted, '没有从 common/routes 里解析出任何 from app import'
    # preload_state 是入口模块里的普通全局（真正实例化后传给路由），不走惰性表
    module_level = {'app', 'preload_state'}
    missing = {name for name in wanted
               if name not in LAZY_HEAVY_EXPORTS and name not in module_level}
    assert not missing, '这些名字不在 LAZY_HEAVY_EXPORTS 里，路由会拿不到：%s' % sorted(missing)

    for variant in ('win7', 'win11'):
        text = io.open(os.path.join(ROOT, variant, 'app.py'), encoding='utf-8').read()
        assert 'preload_state = PreloadState()' in text, variant


def test_create_app_is_idempotent_per_call(tmp_path, monkeypatch):
    """工厂要能反复建 app（测试/多入口），不能靠模块级单例。"""
    import common.config as config
    monkeypatch.setattr(config, 'DB_PATH', str(tmp_path / 'factory.db'))
    from common.app_factory import create_app

    a = create_app('factory_a')
    b = create_app('factory_b')
    assert a is not b
    assert len(a.url_map._rules) == len(b.url_map._rules)
    assert a.config.get('SAVE_DIALOG_FN') is None


@pytest.mark.parametrize('script', sorted(
    glob.glob(os.path.join(ROOT, 'scripts', '**', '*.ps1'), recursive=True)),
    ids=lambda p: os.path.basename(p))
def test_powershell_scripts_are_utf8_bom(script):
    """`.ps1` 统一存成 UTF-8 带 BOM。

    项目用 pwsh 7（能读无 BOM 的 UTF-8），但带 BOM 可以让编辑器、Get-Content、
    第三方工具都按 UTF-8 解码，中文注释不会变成乱码；也避免哪天被 5.1 打开时
    把引号解析坏（报 MissingExpressionAfterToken）。
    """
    with open(script, 'rb') as fh:
        head = fh.read(3)
    assert head == b'\xef\xbb\xbf', '%s 缺 UTF-8 BOM，请统一存成 UTF-8 with BOM' % script


# ── 密钥不入库 ─────────────────────────────────────────────────────────────
# 项目里出现过 signing\stockchart-codeSigning.pfx 与 signconf 明文口令。
# 一旦被 git 收进来（或被打进备份包），等于把代码签名私钥交出去。
# 真实的私钥现在就放在 C:\ProgramData\StockChart\secrets（不在仓库里）。

SECRET_NAME_RE = re.compile(
    r'(^|/)(signing/|id_rsa|id_ed25519|.*\.pfx$|.*\.p12$|.*\.key$|'
    r'pfx_password|.*_password\.txt$|.*\.pem$)', re.IGNORECASE)

# 注意：docs/ 里会写"id_ed25519_ubuntu"这种文件名，那只是路径说明，不是密钥本体，
# 所以只对"看起来像私钥正文"的内容做拦截。
PRIVATE_KEY_HEADERS = (
    '-----BEGIN OPENSSH PRIVATE KEY-----',
    '-----BEGIN RSA PRIVATE KEY-----',
    '-----BEGIN PRIVATE KEY-----',
    '-----BEGIN EC PRIVATE KEY-----',
)


def _git_ls_files():
    import subprocess
    # 文件名里有中文，别让 subprocess 用系统代码页(GBK)去解码；直接收字节再解
    proc = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT,
                          capture_output=True)
    if proc.returncode != 0:
        pytest.skip('不在 git 工作副本里：%r' % proc.stderr[:200])
    return [p.decode('utf-8', 'surrogateescape')
            for p in proc.stdout.split(b'\0') if p]


def test_no_secret_files_are_tracked():
    leaked = [p for p in _git_ls_files()
              if SECRET_NAME_RE.search(p.replace(chr(92), '/'))]
    assert not leaked, '这些密钥文件被 git 收录了，必须移出仓库并加进 .gitignore：%s' % leaked


def test_no_private_key_content_is_tracked():
    leaked = []
    for rel in _git_ls_files():
        path = os.path.join(ROOT, rel.replace('/', os.sep))
        if not os.path.isfile(path) or os.path.getsize(path) > 512 * 1024:
            continue
        try:
            with io.open(path, encoding='utf-8', errors='ignore') as fh:
                text = fh.read()
        except OSError:
            continue
        if any(h in text for h in PRIVATE_KEY_HEADERS):
            leaked.append(rel)
    assert not leaked, '这些文件里有私钥正文，绝对不能入库：%s' % leaked


def test_gitignore_covers_signing_material():
    """新克隆的仓库不能因为谁手滑 `git add -A` 就把签名材料带进去。"""
    with io.open(os.path.join(ROOT, '.gitignore'), encoding='utf-8') as fh:
        lines = {line.strip() for line in fh}
    for pattern in ('signing/', '*.pfx', '*.cer'):
        assert pattern in lines, '.gitignore 缺少 %s' % pattern


def test_teamcity_password_param_is_masked():
    """口令必须是 Password 类型参数，不能是普通 text 参数。"""
    with io.open(os.path.join(ROOT, '.teamcity', 'settings.kts'), encoding='utf-8') as fh:
        text = fh.read()
    assert 'password("env.STOCKCHART_SIGN_PFX_PASSWORD"' in text
    assert 'text("env.STOCKCHART_SIGN_PFX_PASSWORD"' not in text
    # 口令不能硬编码在 DSL 里：声明处的第二个参数必须是空串
    m = re.search(r'password\("env\.STOCKCHART_SIGN_PFX_PASSWORD"\s*,\s*"([^"]*)"', text)
    assert m and m.group(1) == '', 'DSL 里不要把 PFX 口令写成字面量'
