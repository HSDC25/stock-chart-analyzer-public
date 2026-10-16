#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按顺序安全发布桌面版更新产物：包体先传并逐个校验，清单最后传。

顺序是硬约束：`update.json` 是客户端唯一的触发点，先传清单会让客户端拿到指向缺失文件
（或 sha 对不上）的 URL，直接拒收且没有回退。

用法：
    python scripts/publish_update.py --notes "..."        # 先生成本地 update_dist/
    python scripts/republish_update.py                    # 传包体 + 校验 + 传清单
    python scripts/republish_update.py --dry-run          # 只解析与核对本地文件
    python scripts/republish_update.py --exe-version 2026.10.04
        # 把清单里所有 version 改写成该值（见下）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / 'update_dist' / 'update.json'
DEFAULT_KEY = Path.home() / '.ssh' / 'HSDC.pem'
DEFAULT_HOST = os.environ.get('STOCKCHART_PUBLISH_HOST', '')
DEFAULT_REMOTE_DIR = '/var/www/stockdata/updates'
# 清单里的文件名要拼进 ssh 命令，只放行普通文件名字符
SAFE_NAME = re.compile(r'^[\w][\w.\-]*$')


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--manifest', default=str(MANIFEST), help='本地 update.json 路径')
    p.add_argument('--host', default=DEFAULT_HOST,
                   help='SSH 目标，如 user@host（也可用环境变量 STOCKCHART_PUBLISH_HOST）')
    p.add_argument('--remote-dir', default=DEFAULT_REMOTE_DIR)
    p.add_argument('--key', default=str(DEFAULT_KEY))
    p.add_argument('--exe-version', default='',
                   help='改写清单里每个变体的 version。装机基数里若还有换体脚本坏掉的包，'
                        '必须把它压在装机版本上，否则那些机器每次启动都白下一个完整包')
    p.add_argument('--skip-upload', action='store_true', help='只做校验，不上传')
    p.add_argument('--dry-run', action='store_true', help='连服务器都不连，只核对本地产物')
    return p.parse_args(argv)


def resolve(name: str) -> Path:
    """清单里只有文件名，字节可能在三处之一：dist（带日期 exe）或 update_dist（zip）。"""
    for candidate in (REPO / 'update_dist' / name,
                      REPO / 'win11' / 'dist' / name,
                      REPO / 'win7' / 'dist' / name):
        if candidate.is_file():
            return candidate
    raise SystemExit(f'[FAIL] 找不到 {name}：先跑构建脚本与 publish_update.py')


def scp(key: Path, files: list, target: str) -> int:
    cmd = ['scp', '-q', '-i', str(key)] + [str(f) for f in files] + [target]
    return subprocess.run(cmd, check=False).returncode  # nosec B603 - 参数均为本地路径与固定目标


def server_shas(key: Path, host: str, remote_dir: str, names: list) -> dict:
    quoted = ' '.join(names)
    out = subprocess.check_output(  # nosec B603 - names 来自清单文件名，已在本机解析过
        ['ssh', '-i', str(key), host, f'cd {remote_dir} && sha256sum {quoted}'],
        text=True, errors='ignore')
    sums = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            sums[parts[1]] = parts[0]
    return sums


def https_ok(entry_url: str, expect_size: int) -> bool:
    req = urllib.request.Request(entry_url, method='HEAD',
                                 headers={'User-Agent': 'StockChart-Publish'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # nosec B310 - 清单强制 HTTPS
            length = r.headers.get('Content-Length')
            return r.status == 200 and length is not None and int(length) == expect_size
    except (urllib.error.URLError, ValueError):
        return False


def main(argv=None) -> int:
    args = parse_args(argv)
    remote_dir = args.remote_dir.rstrip('/')
    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    variants = manifest.get('variants') or {}
    if not variants:
        print('[FAIL] 清单是空的')
        return 1

    if args.exe_version:
        for entry in variants.values():
            entry['version'] = args.exe_version
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
                                 encoding='utf-8')
        print(f'[ok] 清单里 {len(variants)} 个变体的 version 已改写为 {args.exe_version}')

    # 清单里每条 URL 对应的本地产物，先确认本机字节就和 sha 一致
    payload = {}
    expected = {}
    for variant, entry in sorted(variants.items()):
        for kind in ('exe', 'ui'):
            url = entry.get('%s_url' % kind) or ''
            name = url.rsplit('/', 1)[-1]
            if not name:
                print(f'[FAIL] {variant} 缺少 {kind}_url')
                return 1
            if not SAFE_NAME.match(name):
                print(f'[FAIL] {variant} 的 {kind}_url 文件名不合法：{name}')
                return 1
            path = resolve(name)
            expected[name] = entry['%s_sha256' % kind]
            local_sha = sha256_file(path)
            if local_sha != expected[name]:
                print(f'[FAIL] {variant} 的 {kind} 与清单 sha 不一致：'
                      f'本地 {local_sha[:16]}… 清单 {expected[name][:16]}…')
                print('       重新生成清单（publish_update.py）后再发布，别只改一半')
                return 1
            payload[name] = path
    print(f'[ok] 本地产物 {len(payload)} 个，sha 全部与清单一致')

    if args.dry_run:
        print('[dry-run] 不连服务器')
        return 0

    key = Path(args.key)
    names = sorted(payload)
    if not args.skip_upload:
        print(f'上传 {len(names)} 个包体到 {args.host}:{remote_dir}/ …')
        if scp(key, [payload[n] for n in names], f'{args.host}:{remote_dir}/') != 0:
            print('[FAIL] 包体上传失败，清单保持原样未动')
            return 1

    print('核对服务器上的 sha256 …')
    remote = server_shas(key, args.host, remote_dir, names)
    bad = [n for n in names if remote.get(n) != expected[n]]
    if bad:
        for n in bad:
            print(f'[FAIL] 服务器上的 {n} sha 与清单不符: {remote.get(n)}')
        return 1

    print('逐个 URL 验 HTTPS 200 与长度 …')
    for variant, entry in sorted(variants.items()):
        for kind in ('exe', 'ui'):
            if not https_ok(entry['%s_url' % kind], entry['%s_size' % kind]):
                print(f'[FAIL] {variant} 的 {kind} 拉不到：{entry["%s_url" % kind]}')
                return 1
    print(f'[ok] {len(names)} 个包体在服务器上 sha/长度/HTTPS 全通过')

    print('最后上传清单 update.json …')
    if scp(key, [manifest_path], f'{args.host}:{remote_dir}/') != 0:
        print('[FAIL] 清单上传失败——此时包体是新的、清单仍是旧的，客户端只会用到旧清单')
        return 1

    first = next(iter(variants.values()))
    manifest_url = first['exe_url'].rsplit('/', 1)[0].replace('/updates', '') + '/update.json'
    req = urllib.request.Request(manifest_url,
                                 headers={'User-Agent': 'StockChart-Publish',
                                          'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(req, timeout=30) as r:  # nosec B310 - 固定官方地址
        live = r.read()
    local = manifest_path.read_bytes()
    if live != local:
        print('[FAIL] 线上清单和本地不一致（缓存或上传未完成），先别收工')
        return 1
    print('[OK] 线上清单已与本地逐字节一致')
    for variant, entry in sorted(variants.items()):
        print(f'  {variant:<28} version={entry["version"]} ui_version={entry["ui_version"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
