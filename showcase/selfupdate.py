#!/usr/bin/env python3
"""容器启动时从 GitHub 获取最新脚本，不用重新构建镜像。只用标准库。

下载最新提交里的脚本到 /cache/app/<提交号>/，检查通过后让 /cache/app/current 指向它；
docker-entrypoint.sh 发现 current 存在就改用那里的脚本运行。

不会切换的情况（继续用当前版本）：
  - 网络不通、GitHub 访问失败
  - 新版本的 Python 脚本有语法错误
  - 新版本改了依赖（Dockerfile 里安装软件的部分变了）：这时需要重新构建镜像（update.ps1 / update.sh）
本地改代码调试时，用 VIDCUT_UPDATE=0 关掉自动更新，否则会被 GitHub 上的版本替换。

用法：python selfupdate.py [--force]
环境变量：VIDCUT_REPO（默认 bil022/vidcut）、VIDCUT_BRANCH（默认 main）、VIDCUT_CODE_DIR（默认 /cache/app）
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

REPO = os.environ.get("VIDCUT_REPO", "bil022/vidcut")
BRANCH = os.environ.get("VIDCUT_BRANCH", "main")
ROOT = Path(os.environ.get("VIDCUT_CODE_DIR", "/cache/app"))
IMAGE_APP = Path(__file__).resolve().parent
FILES = ["vidcut.py", "web.py", "web/index.html", "Makefile", "make_samples.sh",
         "docker-entrypoint.sh", "selfupdate.py", "Dockerfile"]
HEADERS = {"User-Agent": "vidcut-selfupdate"}


def say(msg: str) -> None:
    print(f"[更新] {msg}", flush=True)


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=20) as r:
        return r.read()


def deps_key(dockerfile: str) -> str:
    """Dockerfile 里复制代码（WORKDIR /app）之前的部分就是安装依赖；它变了就必须重新构建镜像。"""
    lines = []
    for line in dockerfile.splitlines():
        if line.strip().startswith("WORKDIR /app"):
            break
        if line.strip() and not line.strip().startswith("#"):
            lines.append(line.strip())
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def current_rev() -> str:
    rev_file = ROOT / "current" / "REV"
    return rev_file.read_text().strip() if rev_file.exists() else os.environ.get("VIDCUT_REV", "dev")


def main() -> int:
    force = "--force" in sys.argv
    try:
        latest = json.loads(fetch(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"))["sha"]
    except Exception as e:  # noqa: BLE001 —— 没网时照常用当前版本
        say(f"无法连接 GitHub（{type(e).__name__}），继续使用当前版本")
        return 0
    cur = current_rev()
    if latest == cur and not force:
        say(f"脚本已是最新（{latest[:7]}）")
        return 0

    say(f"下载新版本 {cur[:7]} → {latest[:7]} …")
    try:
        data = fetch(f"https://codeload.github.com/{REPO}/tar.gz/{latest}")
    except Exception as e:  # noqa: BLE001
        say(f"下载失败（{type(e).__name__}），继续使用当前版本")
        return 0
    staging = ROOT / f"{latest}.tmp"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts  # <仓库名-提交号>/showcase/<文件>
            if len(parts) >= 3 and parts[1] == "showcase" and "/".join(parts[2:]) in FILES and member.isfile():
                dest = staging.joinpath(*parts[2:])
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(tar.extractfile(member).read())
    missing = [f for f in FILES if not (staging / f).exists() and f != "selfupdate.py"]
    if missing:
        say(f"新版本缺少文件 {', '.join(missing)}，继续使用当前版本")
        shutil.rmtree(staging, ignore_errors=True)
        return 0

    image_dockerfile = IMAGE_APP / "Dockerfile"
    if image_dockerfile.exists() and deps_key(image_dockerfile.read_text()) != deps_key((staging / "Dockerfile").read_text()):
        say("新版本改了依赖，需要重新构建镜像（运行 update.ps1 / update.sh）；这次继续使用当前版本")
        shutil.rmtree(staging, ignore_errors=True)
        return 0
    try:
        for f in ("vidcut.py", "web.py", "selfupdate.py"):
            if (staging / f).exists():
                compile((staging / f).read_text(encoding="utf-8"), f, "exec")  # 只检查语法，不写文件
    except SyntaxError as e:
        say(f"新版本 {e.filename} 第 {e.lineno} 行有语法错误，继续使用当前版本")
        shutil.rmtree(staging, ignore_errors=True)
        return 0

    (staging / "REV").write_text(latest + "\n")
    final = ROOT / latest
    shutil.rmtree(final, ignore_errors=True)
    staging.rename(final)
    link = ROOT / "current.tmp"
    link.unlink(missing_ok=True)
    link.symlink_to(final.name)
    link.replace(ROOT / "current")  # 原子切换
    for old in ROOT.iterdir():  # 清理旧版本
        if old.is_dir() and not old.is_symlink() and old.name != latest:
            shutil.rmtree(old, ignore_errors=True)
    say(f"已更新到 {latest[:7]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
