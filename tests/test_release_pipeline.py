"""一键发版流水线的纯逻辑测试：上传计划、线上验收地址、git 钩子。

这几处一旦写歪，最坏的结果是"以为发了新版，客户端拿到的还是旧的"，
所以不依赖网络也不依赖证书，只钉住可判定的部分。
"""
import importlib.util
import io
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, *parts):
    path = os.path.join(ROOT, *parts)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


republish = _load('republish_update', 'scripts', 'republish_update.py')
verify = _load('verify_published', 'scripts', 'ci', 'verify_published.py')


# ── 只传变化的包体 ─────────────────────────────────────────────────────────

def test_plan_uploads_skips_files_with_same_sha():
    expected = {'a.exe': '1', 'b.exe': '2', 'u.zip': '3'}
    remote = {'a.exe': '1', 'b.exe': 'x', 'u.zip': '3'}
    todo, skipped = republish.plan_uploads(expected, remote)
    assert todo == ['b.exe']
    assert skipped == ['a.exe', 'u.zip']


def test_plan_uploads_treats_missing_remote_as_todo():
    todo, skipped = republish.plan_uploads({'a.exe': '1'}, {})
    assert todo == ['a.exe'] and skipped == []


def test_plan_uploads_force_sends_everything():
    expected = {'a.exe': '1', 'b.exe': '2'}
    todo, skipped = republish.plan_uploads(expected, dict(expected), force=True)
    assert todo == ['a.exe', 'b.exe'] and skipped == []


def test_plan_uploads_output_is_sorted_for_stable_logs():
    todo, _ = republish.plan_uploads({'z.exe': '1', 'a.exe': '2', 'm.exe': '3'}, {})
    assert todo == ['a.exe', 'm.exe', 'z.exe']


# ── 线上验收 ───────────────────────────────────────────────────────────────

def test_manifest_url_is_derived_from_updates_prefix():
    assert (verify.manifest_url_for('https://data.hsdc.cloud/updates')
            == 'https://data.hsdc.cloud/update.json')
    assert (verify.manifest_url_for('https://data.hsdc.cloud/updates/')
            == 'https://data.hsdc.cloud/update.json')
    assert (verify.manifest_url_for('https://example.test')
            == 'https://example.test/update.json')


def test_pick_exes_takes_head_and_tail():
    names = ['v%d.exe' % i for i in range(7)]
    picked = verify.pick_exes(names, 2)
    assert picked == ['v0.exe', 'v6.exe']


def test_pick_exes_returns_all_when_fewer_than_limit():
    assert verify.pick_exes(['b.exe', 'a.exe'], 2) == ['a.exe', 'b.exe']


def test_pick_exes_zero_means_no_download():
    assert verify.pick_exes(['a.exe'], 0) == []


# ── git 钩子 ───────────────────────────────────────────────────────────────

HOOKS = os.path.join(ROOT, '.githooks')


def _hooks():
    if not os.path.isdir(HOOKS):
        pytest.skip('没有 .githooks 目录')
    return sorted(f for f in os.listdir(HOOKS) if os.path.isfile(os.path.join(HOOKS, f)))


def test_release_hook_exists():
    assert 'post-commit' in _hooks(), '发版钩子没了，[release] 提交就不会自动跑'


@pytest.mark.parametrize('hook', _hooks())
def test_hooks_use_lf_and_no_bom(hook):
    """钩子由 sh 执行：CRLF 让 shebang 变成 /bin/sh^M，BOM 让 shebang 不被识别。"""
    raw = io.open(os.path.join(HOOKS, hook), 'rb').read()
    assert b'\r' not in raw, '%s 里有 CR，git 会报 bad interpreter' % hook
    assert not raw.startswith(b'\xef\xbb\xbf'), '%s 带 BOM，sh 认不出 shebang' % hook


@pytest.mark.parametrize('hook', _hooks())
def test_hooks_start_with_shebang(hook):
    first = io.open(os.path.join(HOOKS, hook), encoding='utf-8').readline()
    assert first.strip() in ('#!/bin/sh', '#!/usr/bin/env sh', '#!/bin/bash')


def test_release_hook_only_fires_on_marker():
    text = io.open(os.path.join(HOOKS, 'post-commit'), encoding='utf-8').read()
    assert '[release]' in text
    # 钩子必须永远成功退出，否则会反过来让提交失败
    assert 'exit 0' in text


def test_release_script_has_all_entry_points():
    text = io.open(os.path.join(ROOT, 'scripts', 'ci', 'release.ps1'),
                   encoding='utf-8').read()
    for switch in ('$PublishOnly', '$UiOnly', '$DryRun', '$SkipTests'):
        assert switch in text, 'release.ps1 缺参数 %s' % switch
    assert 'release.lock' in text, 'release.ps1 没加互斥锁，两次发版会互相覆盖产物'
    assert 'verify_published.py' in text, 'release.ps1 没接线上验收'
