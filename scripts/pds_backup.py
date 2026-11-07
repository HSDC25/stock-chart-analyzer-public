#!/usr/bin/env python3
"""把项目源码 + 完整 Git 历史打包备份到阿里云 PDS 企业网盘（个人空间）。

用法:
    python scripts/pds_backup.py                 # 备份当前 HEAD（同一 HEAD 已备份则跳过）
    python scripts/pds_backup.py --force         # 忽略去重，强制再备一份
    python scripts/pds_backup.py --verify-download  # 上传后回读两个归档并比对 sha256
    python scripts/pds_backup.py --install-hook  # 安装 git post-commit 钩子，提交后自动备份
    python scripts/pds_backup.py --uninstall-hook

鉴权沿用 aliyun CLI 的 api_key 配置（~/.aliyun），脚本不含任何密钥。
首次需先执行:
    aliyun pds config --authentication-type api_key --api-key <uk-...> --domain-id bj41851
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

DOMAIN_ID = "bj41851"
DRIVE_ID = "101"  # 个人空间「吴焱东」
USER_AGENT = "AlibabaCloud-Agent-Skills/alibabacloud-pds-intelligent-workspace"


def log(msg: str) -> None:
    print(f"[pds-backup] {msg}", flush=True)


def repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return Path(out)


def find_aliyun() -> str:
    env = os.environ.get("ALIYUN_CLI")
    if env and Path(env).exists():
        return env
    found = shutil.which("aliyun")
    if found:
        return found
    home = Path.home() / "aliyun-cli" / ("aliyun.exe" if os.name == "nt" else "aliyun")
    if home.exists():
        return str(home)
    raise SystemExit("[pds-backup] 找不到 aliyun CLI，请安装或设置 ALIYUN_CLI 环境变量")


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def pds(aliyun: str, args: list[str], query: str | None = None) -> dict | list:
    cmd = [aliyun, "pds", *args, "--user-agent", USER_AGENT]
    if query:
        cmd += ["--cli-query", query]
    r = run(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"aliyun pds {' '.join(args[:2])} 失败:\n{r.stdout}\n{r.stderr}")
    text = r.stdout.strip()
    # 上传命令会先打印进度行，再输出 JSON；取最后一个 JSON 块
    start = text.find("{")
    arr = text.find("[")
    if arr != -1 and (start == -1 or arr < start):
        start = arr
    if start == -1:
        return {}
    return json.loads(text[start:])


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_files(root: Path) -> list[str]:
    raw = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True
    ).stdout
    tracked = [p.decode("utf-8") for p in raw.split(b"\0") if p]

    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "-z"], capture_output=True, check=True
    ).stdout
    extra = []
    for entry in status.split(b"\0"):
        if entry.startswith(b"?? "):
            rel = entry[3:].decode("utf-8")
            if rel.startswith("docs/") and rel.lower().endswith(".md"):
                extra.append(rel)
    return list(dict.fromkeys(tracked + extra))


def build_artifacts(root: Path, staging: Path, head: str) -> dict[str, Path]:
    staging.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    files = collect_files(root)

    src_zip = staging / f"stockchart-src-{stamp}.zip"
    with zipfile.ZipFile(src_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in files:
            p = root / rel.replace("/", os.sep)
            if p.is_file():
                z.write(p, "stockchart/" + rel)
    log(f"源码包: {src_zip.name} ({len(files)} 个文件, {src_zip.stat().st_size} B)")

    bundle = staging / f"stockchart-repo-fullhistory-{stamp}.bundle"
    r = run(["git", "-C", str(root), "bundle", "create", str(bundle), "--all"])
    if r.returncode != 0 or not bundle.exists():
        raise RuntimeError(f"git bundle 失败:\n{r.stdout}\n{r.stderr}")
    log(f"历史包: {bundle.name} ({bundle.stat().st_size} B)")

    manifest = staging / "SHA256SUMS.txt"
    lines = [f"{sha256(p)}  {p.name}" for p in (src_zip, bundle)]
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"src": src_zip, "bundle": bundle, "manifest": manifest}


def state_path(root: Path) -> Path:
    return root / ".git" / "pds-backup-state.json"


def load_state(root: Path) -> dict:
    p = state_path(root)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
    return {}


def backup(root: Path, aliyun: str, force: bool, verify_download: bool) -> None:
    head = run(["git", "-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
    state = load_state(root)
    if not force and state.get("head") == head:
        log(f"HEAD {head[:7]} 已备份过（{state.get('folder')}），跳过。用 --force 强制重备。")
        return

    # 预检鉴权
    pds(aliyun, ["get-user"], "{user_id: user_id}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder_name = f"stockchart-backup-{stamp}-{head[:7]}"
    local_app = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    staging = Path(local_app) / "stockchart-pds-backup" / stamp
    arts = build_artifacts(root, staging, head)

    created = pds(
        aliyun,
        ["create-file", "--drive-id", DRIVE_ID, "--parent-file-id", "root",
         "--name", folder_name, "--type", "folder", "--check-name-mode", "auto_rename"],
        "{file_id: file_id, name: name}",
    )
    folder_id = created["file_id"]
    log(f"云端目录: /{created.get('name') or folder_name} (drive {DRIVE_ID})")

    file_ids = {}
    for key, path in arts.items():
        res = pds(
            aliyun,
            ["upload-file", "--drive-id", DRIVE_ID, "--parent-file-id", folder_id,
             "--local-path", str(path), "--name", path.name, "--check-name-mode", "refuse"],
            "{file_id: file_id, name: name, size: size}",
        )
        if int(res.get("size", -1)) != path.stat().st_size:
            raise RuntimeError(f"{path.name} 上传后体积不一致: 云端 {res.get('size')} vs 本地 {path.stat().st_size}")
        file_ids[key] = res["file_id"]
        log(f"已上传 {path.name} ({res['size']} B) ✓")

    if verify_download:
        for key in ("src", "bundle"):
            path = arts[key]
            tmp = staging / f"verify-{path.name}"
            pds(aliyun, ["download-to-local", "--drive-id", DRIVE_ID,
                         "--file-id", file_ids[key], "--save-to", str(tmp)])
            ok = sha256(tmp) == sha256(path)
            tmp.unlink(missing_ok=True)
            if not ok:
                raise RuntimeError(f"{path.name} 回读 sha256 不一致")
            log(f"回读校验 {path.name} sha256 一致 ✓")

    state_path(root).write_text(json.dumps({
        "head": head, "folder": folder_name, "folder_id": folder_id,
        "file_ids": file_ids, "at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"完成。云端路径 /{folder_name}/，本地暂存 {staging}")


def hook_path(root: Path) -> Path:
    return root / ".git" / "hooks" / "post-commit"


def install_hook(root: Path) -> None:
    py = Path(sys.executable).as_posix()
    script = (root / "scripts" / "pds_backup.py").as_posix()
    logfile = (root / ".git" / "pds-backup.log").as_posix()
    content = (
        "#!/bin/sh\n"
        "# 由 scripts/pds_backup.py --install-hook 生成；提交后自动备份到 PDS。\n"
        f'"{py}" "{script}" >> "{logfile}" 2>&1\n'
    )
    hp = hook_path(root)
    if hp.exists():
        raise SystemExit(f"[pds-backup] 已存在 {hp}，未覆盖。请先手动处理。")
    hp.write_text(content, encoding="utf-8")
    hp.chmod(0o755)
    log(f"已安装 post-commit 钩子: {hp}（日志 .git/pds-backup.log）")


def uninstall_hook(root: Path) -> None:
    hp = hook_path(root)
    if hp.exists() and "pds_backup.py" in hp.read_text(encoding="utf-8"):
        hp.unlink()
        log(f"已移除钩子: {hp}")
    else:
        log("未发现本脚本安装的钩子，未改动。")


def main() -> None:
    ap = argparse.ArgumentParser(description="备份项目到阿里云 PDS 企业网盘")
    ap.add_argument("--force", action="store_true", help="忽略 HEAD 去重，强制备份")
    ap.add_argument("--verify-download", action="store_true", help="上传后回读归档比对 sha256")
    ap.add_argument("--install-hook", action="store_true", help="安装 git post-commit 自动备份钩子")
    ap.add_argument("--uninstall-hook", action="store_true", help="移除自动备份钩子")
    args = ap.parse_args()

    root = repo_root()
    if args.install_hook:
        install_hook(root)
        return
    if args.uninstall_hook:
        uninstall_hook(root)
        return

    aliyun = find_aliyun()
    try:
        backup(root, aliyun, args.force, args.verify_download)
    except Exception as e:  # 钩子场景下失败不应阻断，仅记录
        log(f"失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()