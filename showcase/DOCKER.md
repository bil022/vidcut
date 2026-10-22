# 用 Docker 运行（Windows / macOS / Linux）

镜像里装好了 ffmpeg（带 HDR 转换）、中文字体、语音识别（faster-whisper）和离线翻译（Argos Translate），
不需要在本机安装 Python、ffmpeg 或 uv。

## 1. 安装与构建

1. 安装 [Docker Desktop](https://www.docker.com/products/docker-desktop/)（Windows 需启用 WSL 2，安装程序会提示）
2. 在 `showcase` 目录构建镜像（第一次约 10–20 分钟，镜像约 2–3GB）：

```bash
docker build -t vidcut .
```

不需要离线翻译时可以构建小一点的镜像（翻译只用 Claude）：

```bash
docker build -t vidcut --build-arg WITH_TRANSLATE=0 .
```

## 2. 运行

三个目录挂载进容器：

| 容器里 | 放什么 |
|---|---|
| `/videos` | 你的素材目录（只读挂载，原文件不会被改动） |
| `/work` | 工作目录：`settings.json`、`project.json`、输出视频 `out/` |
| `/cache` | 模型缓存（语音识别、翻译模型），用命名卷 `vidcut-cache`，只需下载一次 |

### Windows（PowerShell）

```powershell
docker run --rm -v "C:\Users\me\Videos\Japan:/videos:ro" -v "C:\Users\me\vidcut\work_japan:/work" -v vidcut-cache:/cache vidcut auto /videos --name "日本之旅"
```

```powershell
docker run --rm -p 8766:8766 -v "C:\Users\me\Videos\Japan:/videos:ro" -v "C:\Users\me\vidcut\work_japan:/work" -v vidcut-cache:/cache vidcut web
```

### macOS / Linux

```bash
docker run --rm -v ~/Movies/Japan:/videos:ro -v "$PWD/work_japan":/work -v vidcut-cache:/cache vidcut auto /videos --name "日本之旅"
```

```bash
docker run --rm -p 8766:8766 -v ~/Movies/Japan:/videos:ro -v "$PWD/work_japan":/work -v vidcut-cache:/cache vidcut web
```

预览视频在工作目录的 `out/日本之旅_preview.mp4`；网页编辑器打开 http://localhost:8766 。

### 常用命令

在 `vidcut` 后面换成下面任意一个（挂载参数相同）：

| 命令 | 作用 |
|---|---|
| `auto /videos --name "片名"` | 全自动出预览视频（加 `--final` 同时出 1080p） |
| `auto --rebuild` | 改了 `settings.json` 后重新生成剪辑 |
| `render --draft` / `render` | 改了 `project.json` 后重出预览 / 成片 |
| `update` | 加入新素材 |
| `web` | 网页编辑器 |
| `make status`、`make help` | 同 Makefile |
| `help` | 显示用法 |

## 3. 用 docker compose（少打字）

```bash
cp .env.example .env      # Windows：copy .env.example .env
```

编辑 `.env` 里的 `VIDEOS`（素材目录）和 `WORK`（工作目录），然后：

```bash
docker compose build
docker compose run --rm vidcut auto /videos --name "日本之旅"
docker compose up web
```

## 4. 说明

- **Claude**：在 `.env` 里填 `ANTHROPIC_API_KEY`，或 `docker run -e ANTHROPIC_API_KEY=…`，摘要和翻译会用 Claude；不填就用规则和离线翻译
- **语音识别模型**：默认 `small`（CPU 上较快）；更准用 `-e VIDCUT_WHISPER_MODEL=large-v3-turbo`（第一次下载约 1.6GB，CPU 上明显更慢）
- **速度**：容器里用 CPU 识别语音；在 Apple Silicon Mac 上直接运行（不用 Docker）可以用 GPU，快很多
- **工作目录不要在 Docker 和本机之间混用**：容器里记录的素材路径是 `/videos/...`，本机记录的是本机路径；各用各的工作目录
- **网页里“扫描新素材”** 填的是容器里的路径（例如 `/videos/新的子目录`），不是 Windows 路径
- **Windows 换行**：仓库里的 `.gitattributes` 保证 `.sh` 和 `Makefile` 用 LF 换行，否则容器里无法运行
