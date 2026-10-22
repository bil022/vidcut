#!/bin/sh
# 脚本更新不需要它（容器启动时会自动获取最新脚本）；只有依赖变了才需要重新构建镜像。
# 从 GitHub 重新构建 vidcut 镜像：有新提交才重新构建（不需要本地 git 仓库）。
#   ./update.sh            检查并更新
#   ./update.sh --force    无论如何都重新构建
# 定时运行（每天 3 点）：crontab -e 加一行
#   0 3 * * * /path/to/update.sh >> /tmp/vidcut-update.log 2>&1
set -e
REPO="${VIDCUT_REPO:-bil022/vidcut}"
BRANCH="${VIDCUT_BRANCH:-main}"
IMAGE="${VIDCUT_IMAGE:-vidcut}"

latest=$(curl -fsSL "https://api.github.com/repos/$REPO/commits/$BRANCH" | sed -n 's/^  "sha": "\([0-9a-f]*\)".*/\1/p' | head -1)
[ -n "$latest" ] || { echo "无法从 GitHub 获取最新版本"; exit 1; }
current=$(docker image inspect -f '{{ index .Config.Labels "vidcut.rev" }}' "$IMAGE" 2>/dev/null || true)

if [ "$latest" = "$current" ] && [ "$1" != "--force" ]; then
  echo "$(date '+%F %T') 已是最新（${latest%"${latest#???????}"}）"
  exit 0
fi
echo "$(date '+%F %T') 更新 ${current:-（未安装）} → $latest"
# 下载这个提交的源码包，解压到临时目录再构建（旧版 Docker 不支持直接从 git 子目录构建）
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$latest" | tar -xz -C "$tmp"
docker build --build-arg VIDCUT_REV="$latest" -t "$IMAGE" "$tmp"/*/showcase
echo "完成。正在运行的容器要删除后重新创建才会用新版本（工作目录和缓存卷里的数据不受影响）。"
