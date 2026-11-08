#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布后验收：以普通客户端身份，从公网把「真正会拿到的字节」再核一遍。

``republish_update.py`` 核对的是服务器磁盘上的 sha256，那不等于公网取到的内容：
CDN 缓存、静态站配置、权限都可能让外部拿到旧文件或 403。所以传完再走一遍 HTTPS：

1. 拉 ``update.json``（带 cache-buster），必须与本地 ``update_dist/update.json`` 逐字节一致
2. 清单里每个 URL 都 HEAD 200 且 Content-Length 与清单里的 size 一致
3. UI 包全量下载算 sha256；EXE 默认抽查首尾两个，``--all`` 全下

用法：
    python scripts/ci/verify_published.py
    python scripts/ci/verify_published.py --all
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / 'update_dist' / 'update.json'
DEFAULT_BASE_URL = 'https://data.hsdc.cloud/updates'
UA = {'User-Agent': 'StockChart-Publish'}


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--manifest', default=str(MANIFEST), help='本地 update.json 路径')
    p.add_argument('--base-url', default=DEFAULT_BASE_URL,
                   help='更新文件在服务器上的公网前缀（清单地址由它推导）')
    p.add_argument('--manifest-url', default='',
                   help='显式指定线上清单地址，默认由 --base-url 推导为 <host>/update.json')
    p.add_argument('--exe-sample', type=int, default=2,
                   help='抽查多少个 exe 下载并核对 sha（默认 2，取首尾）；0 表示不抽查')
    p.add_argument('--all', action='store_true', help='下载并核对全部 exe（慢，约 1GB）')
    p.add_argument('--timeout', type=int, default=180, help='单次请求超时秒数')
    return p.parse_args(argv)


def manifest_url_for(base_url: str) -> str:
    """``https://host/updates`` → ``https://host/update.json``。"""
    base = base_url.rstrip('/')
    root = base[:-len('/updates')] if base.endswith('/updates') else base
    return root + '/update.json'


def pick_exes(names: list, limit: int) -> list:
    """挑出要下载核对的 exe：默认首尾各一个，数量不足时全要。"""
    names = sorted(names)
    if limit <= 0:
        return []
    if len(names) <= limit:
        return names
    head = names[:limit // 2]
    tail = names[-(limit - limit // 2):]
    return sorted(set(head + tail))


def _open(url: str, timeout: int, method: str = 'GET'):
    req = urllib.request.Request(url, method=method, headers=UA)
    return urllib.request.urlopen(req, timeout=timeout)  # nosec B310 - 只访问清单里的 https 地址


def fetch_bytes(url: str, timeout: int) -> bytes:
    with _open(url, timeout) as r:
        return r.read()


def head_length(url: str, timeout: int):
    """返回 (status, content-length 或 None)；不支持 HEAD 的服务器再退回 GET 取头。"""
    try:
        with _open(url, timeout, 'HEAD') as r:
            length = r.headers.get('Content-Length')
            return r.status, (int(length) if length else None)
    except urllib.error.HTTPError as exc:
        if exc.code not in (403, 405, 501):
            return exc.code, None
    with _open(url, timeout) as r:
        length = r.headers.get('Content-Length')
        return r.status, (int(length) if length else None)


def sha256_url(url: str, timeout: int) -> tuple:
    """边下边算 sha256，不落盘。返回 (sha256, 字节数)。"""
    h = hashlib.sha256()
    size = 0
    with _open(url, timeout) as r:
        for chunk in iter(lambda: r.read(1 << 20), b''):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def main(argv=None) -> int:
    args = parse_args(argv)
    manifest_path = Path(args.manifest)
    local = manifest_path.read_bytes()
    manifest = json.loads(local)
    variants = manifest.get('variants') or {}
    if not variants:
        print('[FAIL] 本地清单是空的，先跑 publish_update.py')
        return 1

    url = args.manifest_url or manifest_url_for(args.base_url)
    # 加时间戳绕开 CDN 对 update.json 的缓存：验收要看的是源站，不是边缘副本
    cache_buster = ('&' if '?' in url else '?') + 't=%d' % int(time.time())
    print(f'拉线上清单 {url} …')
    try:
        live = fetch_bytes(url + cache_buster, args.timeout)
    except (urllib.error.URLError, OSError) as exc:
        print(f'[FAIL] 拉不到线上清单：{exc}')
        return 1
    if live != local:
        print(f'[FAIL] 线上清单与本机不一致（线上 {len(live)}B / 本机 {len(local)}B）')
        print('       要么清单还没传上去，要么被 CDN 缓存了旧版本，先别收工')
        return 1
    print(f'[ok] 线上清单与本机逐字节一致（{len(local)}B）')

    failures = 0
    for variant, entry in sorted(variants.items()):
        for kind in ('exe', 'ui'):
            entry_url = entry.get('%s_url' % kind) or ''
            size = entry.get('%s_size' % kind) or 0
            try:
                status, length = head_length(entry_url, args.timeout)
            except (urllib.error.URLError, OSError) as exc:
                print(f'[FAIL] {variant} 的 {kind} 拉不到：{exc}')
                failures += 1
                continue
            if status != 200:
                print(f'[FAIL] {variant} 的 {kind} HTTP {status}：{entry_url}')
                failures += 1
            elif length is not None and length != size:
                print(f'[FAIL] {variant} 的 {kind} 长度 {length} != 清单 {size}：{entry_url}')
                failures += 1
            else:
                print(f'[ok] {variant:<28} {kind} HTTP 200 {size / 1048576:.1f}MB')

    exes_to_hash = sorted(variants) if args.all else pick_exes(list(variants), args.exe_sample)
    downloads = [(v, 'ui') for v in sorted(variants)]
    downloads += [(v, 'exe') for v in exes_to_hash]
    total = 0
    for variant, kind in downloads:
        entry = variants[variant]
        entry_url = entry['%s_url' % kind]
        expect = entry['%s_sha256' % kind]
        try:
            digest, size = sha256_url(entry_url, args.timeout)
        except (urllib.error.URLError, OSError) as exc:
            print(f'[FAIL] {variant} 的 {kind} 下载失败：{exc}')
            failures += 1
            continue
        total += size
        if digest != expect:
            print(f'[FAIL] {variant} 的 {kind} 内容 sha 不符：'
                  f'线上 {digest[:16]}… 清单 {expect[:16]}…')
            failures += 1
        else:
            print(f'[ok] {variant:<28} {kind} sha 一致（下载 {size / 1048576:.1f}MB）')

    print(f'\n[汇总] 核对 {len(downloads)} 个文件，实际下载 {total / 1048576:.0f}MB，失败 {failures} 项')
    if failures:
        print('[FAIL] 线上验收没通过')
        return 1
    print('[OK] 线上验收通过：客户端现在拿到的就是这一版')
    return 0


if __name__ == '__main__':
    sys.exit(main())
