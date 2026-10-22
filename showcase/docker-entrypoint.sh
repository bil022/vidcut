#!/bin/sh
# 容器入口：
#   web               启动网页编辑器（容器内监听 0.0.0.0:8766）
#   make <目标> ...    运行 Makefile 里的目标（工作目录固定为 /work）
#   help              显示用法
#   其他              交给 vidcut.py，例如 auto /videos --name "日本之旅"、render --draft
set -e
cd /app

case "$1" in
  web)
    shift
    exec python web.py -w /work --host 0.0.0.0 --port 8766 --asr none "$@"
    ;;
  make)
    shift
    exec make RUN=python WORK=/work "$@"
    ;;
  help|--help|-h|"")
    cat <<'EOF'
vidcut Docker 镜像

  素材目录挂载到 /videos，工作目录挂载到 /work，模型缓存挂载到 /cache：

  docker run --rm -v <素材目录>:/videos:ro -v <工作目录>:/work -v vidcut-cache:/cache vidcut auto /videos --name "日本之旅"
  docker run --rm -p 8766:8766 -v <素材目录>:/videos:ro -v <工作目录>:/work -v vidcut-cache:/cache vidcut web
  docker run --rm -v <素材目录>:/videos:ro -v <工作目录>:/work -v vidcut-cache:/cache vidcut render --draft
  docker run --rm -v <工作目录>:/work vidcut make status

  命令：auto / update / scan / proxy / analyze / transcribe / translate / summarize / roughcut / render / transcript
        web（网页编辑器，浏览器打开 http://localhost:8766）
        make <目标>（同 Makefile，例如 make status、make preview）

  环境变量：ANTHROPIC_API_KEY（可选，用 Claude 做摘要和翻译）
            VIDCUT_WHISPER_MODEL（语音识别模型，默认 small；更准的有 medium、large-v3-turbo）
EOF
    ;;
  *)
    exec python vidcut.py -w /work "$@"
    ;;
esac
