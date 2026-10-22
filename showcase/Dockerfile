# vidcut：在 Windows / macOS / Linux 上用同一个镜像运行（amd64 和 arm64 都可以构建）。
#
# 构建：  docker build -t vidcut .
#         docker build -t vidcut --build-arg WITH_TRANSLATE=0 .   # 不装离线翻译，镜像小很多
# 运行：  见 DOCKER.md，或直接 docker run --rm vidcut help
FROM python:3.12-slim-bookworm

# ffmpeg（Debian 版带 zscale，HDR 视频可以正确转成 SDR）、中文字体、make
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk make ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Python 依赖：Claude SDK、Pillow（画标题和字幕）、faster-whisper（CPU 语音识别）
RUN pip install --no-cache-dir anthropic pillow faster-whisper

# 离线翻译（Argos Translate）依赖 PyTorch；先从 CPU 专用源装 torch，避免下载几 GB 的 GPU 版本
ARG WITH_TRANSLATE=1
RUN if [ "$WITH_TRANSLATE" = "1" ]; then \
      pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
   && pip install --no-cache-dir argostranslate; \
    fi

WORKDIR /app
COPY vidcut.py web.py Makefile make_samples.sh docker-entrypoint.sh ./
COPY web ./web
RUN chmod +x docker-entrypoint.sh make_samples.sh

# 模型和下载缓存（Whisper、离线翻译模型）都放在 /cache，挂载成卷后只需下载一次
# VIDCUT_REEXEC=1：依赖已经装好，不要再尝试用 uv 临时安装
ENV HOME=/cache \
    HF_HOME=/cache/huggingface \
    VIDCUT_REEXEC=1 \
    VIDCUT_WHISPER_MODEL=small \
    VIDCUT_DEFAULT_FOLDER=/videos \
    PYTHONUNBUFFERED=1

# /videos：素材目录（建议只读挂载）  /work：工作目录（项目、设置、输出视频）  /cache：模型缓存
VOLUME ["/work", "/cache"]
EXPOSE 8766
# 构建时的 git 提交号；update.ps1 / update.sh 用它判断镜像是否已是最新（放在最后，不影响前面层的缓存）
ARG VIDCUT_REV=dev
LABEL vidcut.rev=$VIDCUT_REV
ENV VIDCUT_REV=$VIDCUT_REV
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["start"]
