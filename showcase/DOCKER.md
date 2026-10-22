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

### Docker Desktop（图形界面，不用输入命令）

在 Images 里找到 `vidcut`，点 **Run**，展开 **Optional settings**：

| 字段 | 填写 |
|---|---|
| Container name | 随意，例如 `vidcut-japan` |
| Host port | `8766`（对应容器端口 8766） |
| Volumes 第 1 行 | Host path：你的视频目录（如 `C:\Users\me\Videos\Japan`）；Container path：`/videos` |
| Volumes 第 2 行 | Host path：一个空的工作目录（如 `C:\Users\me\vidcut\work_japan`）；Container path：`/work` |
| Volumes 第 3 行（可选） | Host path：一个缓存目录（如 `C:\Users\me\vidcut\cache`）；Container path：`/cache`，模型只下载一次 |
| Environment variables（可选） | `VIDCUT_NAME` = 片名；`ANTHROPIC_API_KEY` = 你的 key |

点 Run 后：

1. **第一次**：容器自动处理所有视频并生成预览视频（在 Logs 标签里能看到进度，第一次要下载模型，较慢）
2. 处理完会自动启动网页编辑器，Logs 里出现“网页编辑器”字样后，浏览器打开 http://localhost:8766
3. 之后在网页里调整、点“出预览视频”/“出 1080p 成片”；输出文件在工作目录的 `out\` 里
4. 以后再启动这个容器（Containers 里点 ▶），会直接进入网页编辑器，不会重新处理
5. 改设置、重新生成、出 1080p、下载都在网页里：右侧“自动设置”点“保存并重新生成”，“输出文件”里点“下载”
6. 没挂载 `/videos` 也能启动：网页会显示开始页，在里面填素材目录

想让容器运行别的命令，加环境变量 `VIDCUT_CMD`，例如：

| VIDCUT_CMD | 作用 |
|---|---|
| `render` | 渲染 1080p 成片后退出 |
| `auto --rebuild` | 改了 settings.json 后重新生成剪辑和预览 |
| `update` | 处理新加入素材目录的视频 |
| `web` | 只启动网页编辑器 |

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
| （不写命令） | 默认 `start`：第一次先全自动处理，然后启动网页编辑器 |

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

## 5. 更新到最新版本

GitHub 上有新提交时重新构建镜像。`update.ps1`（Windows）/ `update.sh`（macOS、Linux）会检查最新提交，
已是最新就什么都不做；否则下载那个版本的源码并构建（不需要安装 git）。

Windows（PowerShell）：

```powershell
powershell -ExecutionPolicy Bypass -File update.ps1
```

没有克隆仓库也可以，先下载这个脚本：

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/bil022/vidcut/main/showcase/update.ps1 -OutFile update.ps1
```

每天凌晨 3 点自动检查（在 `update.ps1` 所在目录运行一次）：

```powershell
schtasks /Create /SC DAILY /ST 03:00 /TN vidcut-update /TR "powershell -ExecutionPolicy Bypass -File `"$PWD\update.ps1`""
```

macOS / Linux：

```bash
./update.sh
```

每天 3 点自动检查：`crontab -e` 加一行 `0 3 * * * /path/to/showcase/update.sh >> /tmp/vidcut-update.log 2>&1`

说明：

- 更新的是镜像。**正在运行的容器还是旧版本**：在 Docker Desktop 里删掉容器，用同样的设置重新 Run（工作目录和缓存卷里的数据都还在）
- 当前镜像是哪个版本：`docker image inspect -f "{{ index .Config.Labels \"vidcut.rev\" }}" vidcut`
- Windows 上的检查记录写在 `update.ps1` 旁边的 `update.log`
