#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成并（可选）上传桌面版的自动更新产物。

流程：读取构建脚本留下的 outputs/<variant>.json 侧车 → 计算 exe 的 sha256 →
把该变体的 templates/static/共享 js 打成 UI 覆盖层 zip（几百 KB）→ 汇总成
``update.json`` → 需要时 scp 到 data.hsdc.cloud。

用法：
    python scripts/publish_update.py                      # 只生成本地 update_dist/
    python scripts/publish_update.py --notes "修复排行筛选"
    python scripts/publish_update.py --scp user@<your-server>:/var/www/stockdata/updates/
    python scripts/publish_update.py --select StockChart_Win11_Dark
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update_lib  # noqa: E402

REPO = update_lib.REPO
DEFAULT_BASE_URL = 'https://data.hsdc.cloud/updates'


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--base-url', default=DEFAULT_BASE_URL,
                   help='更新文件在服务器上的公网前缀（必须 https）')
    p.add_argument('--out', default=str(REPO / 'update_dist'),
                   help='本地产物目录，默认 update_dist/')
    p.add_argument('--notes', default='', help='写入 update.json 的更新说明，显示在版本徽章提示里')
    p.add_argument('--select', nargs='*', metavar='VARIANT', help='只发布指定变体')
    p.add_argument('--scp', metavar='TARGET',
                   help='scp 目标，例如 user@<your-server>:/var/www/stockdata/updates/')
    p.add_argument('--link-exes', action='store_true',
                   help='把 exe 复制进产物目录（默认只引用 outputs/ 里的原文件，避免多占 200MB）')
    p.add_argument('--ui-version', metavar='VERSION',
                   help='界面包单独定版（默认跟构建号同一天）。同一天里再发一次界面必须抬一位，'
                        '比如 2026.10.05.2，否则当天已装过覆盖层的客户端永远不会拿这一份')
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if not args.base_url.startswith('https://') and '127.0.0.1' not in args.base_url:
        print('[FAIL] 更新源必须走 HTTPS：客户端会拒绝明文 HTTP 的包')
        return 2

    out_dir = Path(args.out)
    if args.ui_version and not re.match(r'^\d+(\.\d+){2,3}$', args.ui_version):
        print(f'[FAIL] --ui-version 形如 2026.10.05 或 2026.10.05.2，收到的是 {args.ui_version!r}')
        return 2
    sidecars = update_lib.iter_sidecars()
    if args.select:
        wanted = set(args.select)
        sidecars = [s for s in sidecars if s.stem in wanted]
    if not sidecars:
        print('[FAIL] 没有找到构建侧车 outputs/<variant>.json，请先运行构建脚本')
        return 1

    print(f'发现 {len(sidecars)} 个变体产物，输出目录 {out_dir}')
    result = update_lib.build_manifest(args.base_url, out_dir, args.notes, sidecars,
                                       ui_version=args.ui_version)
    manifest, per_variant = result['manifest'], result['files']
    if not manifest['variants']:
        print('[FAIL] 清单为空：所有侧车都指向了不存在的产物')
        return 1

    files = [out_dir / 'update.json']
    for variant, paths in per_variant.items():
        entry = manifest['variants'][variant]
        upload_exe = paths['exe']
        if args.link_exes:
            linked = out_dir / paths['exe'].name
            if linked != paths['exe']:
                shutil.copy2(str(paths['exe']), str(linked))
            upload_exe = linked
        files.append(upload_exe)
        files.append(paths['ui'])
        (out_dir / f'{variant}_ui.zip.sha256').write_text(
            entry['ui_sha256'] + '  ' + f'{variant}_ui.zip\n', encoding='ascii')

    print(f"\nupdate.json:\n{json.dumps(manifest, ensure_ascii=False, indent=2)[:1200]}")

    if args.scp:
        target = args.scp.rstrip('/') + '/'
        cmd = ['scp'] + [str(f) for f in files] + [target]
        print(f'\n上传 {len(files)} 个文件到 {target} …')
        subprocess.run(cmd, check=True)  # nosec B603 - scp 路径均来自本脚本
        print('[OK] 上传完成。update.json 与 zip/exe 同目录即可被客户端发现')
    else:
        print('\n未指定 --scp，产物留在本地。上传后更新即刻对客户端可见：')
        print(f'  scp {" ".join(str(f) for f in files)} user@<your-server>:/var/www/stockdata/updates/')
    return 0


if __name__ == '__main__':
    sys.exit(main())
