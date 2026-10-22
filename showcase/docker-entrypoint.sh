#!/bin/sh
# 容器入口。Docker Desktop 的“Run”对话框不能填命令，所以不带参数时：
#   - 设置了环境变量 VIDCUT_CMD：运行它（例如 render、auto --rebuild、make status）
#   - 否则（start）：工作目录还没有剪辑时，先对 /videos 全自动出预览视频，然后启动网页编辑器
#
# 也可以在 docker run 的镜像名后面直接写命令：
#   start             同上（默认）
#   web               只启动网页编辑器（容器内 0.0.0.0:8766）
#   make <目标> ...    运行 Makefile 里的目标（工作目录固定为 /work）
#   help              显示用法
#   其他              交给 vidcut.py，例如 auto /videos --name "日本之旅"、render --draft
set -e
cd /app

if [ $# -eq 0 ] || { [ $# -eq 1 ] && [ "$1" = "start" ] && [ -n "$VIDCUT_CMD" ]; }; then
  if [ -n "$VIDCUT_CMD" ]; then
    # 按 shell 规则拆分，支持引号，例如 VIDCUT_CMD='auto /videos --name "日本 之旅"'
    eval "set -- $VIDCUT_CMD"
  else
    set -- start
  fi
fi

has_videos() {
  find /videos -type f \( -iname '*.mp4' -o -iname '*.mov' -o -iname '*.m4v' -o -iname '*.mkv' \
       -o -iname '*.avi' -o -iname '*.mts' \) 2>/dev/null | head -1 | grep -q .
}

case "$1" in
  start)
    if [ ! -f /work/project.json ]; then
      if has_videos; then
        echo "== 第一次运行：对 /videos 全自动处理，完成后启动网页编辑器 =="
        python vidcut.py -w /work auto /videos ${VIDCUT_NAME:+--name "$VIDCUT_NAME"} || true
      else
        echo "/videos 里没有视频：启动网页编辑器后在开始页填写素材目录"
        echo "（Docker Desktop：Volumes 里把视频目录的 Container path 填 /videos，工作目录填 /work）"
      fi
    fi
    echo "== 网页编辑器：浏览器打开 http://localhost:<映射的主机端口>（容器端口 8766） =="
    exec python web.py -w /work --host 0.0.0.0 --port 8766 --asr none
    ;;
  web)
    shift
    exec python web.py -w /work --host 0.0.0.0 --port 8766 --asr none "$@"
    ;;
  make)
    shift
    exec make RUN=python WORK=/work "$@"
    ;;
  help|--help|-h)
    cat <<'EOF'
vidcut Docker 镜像

  挂载：素材目录 → /videos（只读即可），工作目录 → /work，模型缓存卷 → /cache；端口 8766

  Docker Desktop（不能填命令）：
    直接 Run：第一次自动处理 /videos 并出预览视频，然后启动网页编辑器 http://localhost:8766
    环境变量 VIDCUT_NAME   片名，例如 日本之旅
    环境变量 VIDCUT_CMD    改为运行某个命令，例如 render、auto --rebuild、make status

  命令行：
    docker run --rm -p 8766:8766 -v <素材目录>:/videos:ro -v <工作目录>:/work -v vidcut-cache:/cache vidcut
    docker run --rm -v <素材目录>:/videos:ro -v <工作目录>:/work -v vidcut-cache:/cache vidcut auto /videos --name "日本之旅"
    docker run --rm -v <素材目录>:/videos:ro -v <工作目录>:/work -v vidcut-cache:/cache vidcut render

  命令：start（默认）/ web / auto / update / render / transcript / make <目标> / help …

  其他环境变量：ANTHROPIC_API_KEY（可选，用 Claude 做摘要和翻译）
               VIDCUT_WHISPER_MODEL（语音识别模型，默认 small；更准：medium、large-v3-turbo）
EOF
    ;;
  *)
    exec python vidcut.py -w /work "$@"
    ;;
esac
