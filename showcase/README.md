# vidcut：AI 视频初剪演示

方案 v2 的最小可运行版本：一个 Python 命令行脚本加 FFmpeg，外加一个本地网页编辑器。没有数据库、没有队列，
每一步的结果都存成 JSON 文件。

## 需要

- `ffmpeg` / `ffprobe`（`brew install ffmpeg`）
- [`uv`](https://docs.astral.sh/uv/)（自动安装 Python 依赖）
- 可选：Claude API key（`ANTHROPIC_API_KEY`），用于看图写摘要、给初剪建议；没有就用规则兜底
- 可选：语音识别 `mlx-whisper`（Apple Silicon）或 `faster-whisper`（其他平台），由 `uv run --with` 临时装上
- 标题文字用 Pillow 绘制（`uv` 会自动安装），需要系统里有中文字体：macOS 自带苹方；
  Linux 装 Noto Sans CJK；也可以用环境变量 `VIDCUT_FONT` 指定字体文件

## Windows / Linux：用 Docker

不想在本机装 Python、ffmpeg、uv，或者在 Windows 上运行，见 [DOCKER.md](DOCKER.md)：

```bash
docker build -t vidcut .
docker run --rm -v <素材目录>:/videos:ro -v <工作目录>:/work -v vidcut-cache:/cache vidcut auto /videos --name "日本之旅"
```

## 用 make

`make`（或 `make help`）列出所有命令。常用：

```bash
make auto SRC=~/Movies/日本 NAME="日本之旅" WORK=work_japan   # 一条命令出预览视频
make web WORK=work_japan                                     # 网页编辑器 http://127.0.0.1:8766
make preview WORK=work_japan                                 # 改完 project.json 重出预览
make final WORK=work_japan                                   # 同时出 1080p 成片
make status WORK=work_japan                                  # 看各步骤进度
```

分步运行：`make scan SRC=…` → `proxy` → `analyze` → `transcribe` → `translate` → `summarize` → `roughcut` → `preview` / `render`。
变量 `WORK`（工作目录，默认 work）、`SRC`、`NAME`、`PORT`（默认 8766）、`MODEL`（Whisper 模型）都可以覆盖。

## 全自动：一条命令出预览视频

```bash
uv run vidcut.py auto ~/Movies/某次旅行 --name "日本之旅"
```

自动完成：扫描 → 代理 → 分析 → 语音转录（自动识别中文/英文）→ 翻译（中文↔英文）→ AI 摘要与初剪
→ 渲染预览视频 `work/out/日本之旅_preview.mp4`（加 `--final` 同时出 1080p 成片）。

- **长视频**只保留开头 5 秒和结尾 5 秒，中间用转场衔接
- 片段之间是**平滑转场**（淡化；换天/换地点时经过黑场）
- **自动音量**：逐段测响度，有人说话的调到 -16 LUFS，环境声调到 -23 LUFS，整片再统一标准化
- **双语字幕**：原文 + 译文烧进画面，同时输出 `.srt`
- 片头标题卡、“第 N 天 · 地点”标题卡

生成两个 JSON 方便修改：

| 文件 | 改什么 | 改完运行 |
|---|---|---|
| `work/settings.json` | 自动化参数：长视频阈值和保留秒数、转场类型和时长、音量目标、字幕显示方式、是否翻译、标题卡等（文件里 `_说明` 有每项解释） | `vidcut.py auto --rebuild`（重新生成剪辑，覆盖手动修改） |
| `work/project.json` | 具体剪辑：顺序、入出点、音量、转场、标题、每句字幕的原文和译文 | `vidcut.py render --draft`（只重渲改过的段落） |

再次运行 `vidcut.py auto`（不带目录）会重新扫描原目录、处理新增素材，并保留已有的 `project.json`。

翻译优先用 Claude；Claude 不可用时自动安装并使用离线的 Argos Translate（第一次要下载模型，约 100MB）。
语音识别和离线翻译需要的库都会通过 `uv` 自动临时安装，不需要手动 `--with`。

## 快速开始（分步）

```bash
./make_samples.sh samples                 # 生成 6 段模拟旅行视频（含中文语音）
uv run --with mlx-whisper vidcut.py all samples --name "Japan Trip"
uv run vidcut.py render --draft           # 低清草稿，几秒出片 → work/out/Japan_Trip_draft.mp4
uv run vidcut.py render                   # 1080p 成片 + SRT 字幕
```

换成自己的素材：把 `samples` 换成你的视频目录即可，原文件只读，不会被修改。
素材搬了位置，重新 `scan` 新目录即可：内容哈希不变，代理和分析结果都会复用。

### Whisper 模型

默认用 `whisper-large-v3-turbo`（约 1.6GB，中文效果好），第一次运行时会从 Hugging Face 自动下载。
网络慢时可以先手动下载到本地目录，再用 `--model` 指定：

```bash
mkdir -p models/whisper-small-mlx
curl -L -C - -o models/whisper-small-mlx/config.json https://huggingface.co/mlx-community/whisper-small-mlx/resolve/main/config.json
curl -L -C - -o models/whisper-small-mlx/weights.npz https://huggingface.co/mlx-community/whisper-small-mlx/resolve/main/weights.npz
uv run --with mlx-whisper vidcut.py transcribe --model models/whisper-small-mlx
```

## 流水线

| 步骤 | 做什么 | 产物 |
|---|---|---|
| `scan <目录>` | 内容哈希做 ID（自动去重），读时长/帧率/旋转/HDR/拍摄时间/GPS | `work/media.json` |
| `proxy` | 720p、30fps、SDR、带音轨的代理文件 + 缩略图 | `work/derived/<id>/proxy.mp4` |
| `analyze` | 静音、黑屏、场景切换、模糊检测，按场景抽关键帧 | `analysis.json`、`kf_*.jpg` |
| `transcribe` | Whisper 转录，自动识别中文/英文（中英夹杂也行）；丢弃落在静音里的“幻觉”句子 | `transcript.json`、`work/transcript.md` |
| `transcript` | 把所有视频的语音按拍摄时间整理成文字稿（含译文） | `work/transcript.md` |
| `translate` | 中文语音译成英文、英文译成中文；Claude 优先，否则离线 Argos Translate | `transcript.json` 里的 `translation` |
| `summarize` | Claude 看关键帧 + 转录 + 检测结果 → 标题、摘要、地点、保留/丢弃、入出点、理由 | `summary.json` |
| `roughcut` | 按 `settings.json` 生成剪辑：时间排序、分组、标题卡、长视频取头尾、转场、自动音量、字幕 | `work/project.json`、`work/roughcut.md` |
| `render` | 每段单独渲染并缓存 → 转场拼接 → 两遍 loudnorm；标题卡、画面文字、双语字幕；`--draft` 出预览视频 | `work/out/*.mp4`、`*.srt` |
| `auto [目录]` | 以上全部，最后出预览视频；`--final` 同时出成片，`--rebuild` 按 settings.json 重新生成剪辑 | 同上 |
| `update [目录]` | 加入新素材：重新扫描已知目录（或新目录），只处理新增视频，**不改** `project.json` | 同上 |

每一步都会跳过已经做完的部分，加 `--force` 重做。`all` 等于依次跑 scan → roughcut。

- 转录语言默认自动检测；只有一种语言时可以指定，例如 `transcribe --language en`
- `roughcut --no-titles` 不加标题卡；`roughcut --captions` 把 AI 标题作为文字叠在每段开头

## 用命令行“剪辑”

`roughcut` 之后直接编辑 `work/project.json`，然后重新 `render --draft` 看效果：

- 调顺序：移动 `tracks[0].items` 里的条目
- 改裁剪：改 `in_ms` / `out_ms`（毫秒，相对原视频）
- 调音量：`volume_db`
- 改间隔：增删 `{"type": "gap", "duration_ms": 1000}`
- 转场：任意一项加 `"transition": {"type": "fade", "duration_ms": 600}`，表示从上一项过渡到这一项（`cut` 或删掉表示直接切）
- 字幕：clip 里的 `"subtitles": [{"start_ms": 3600, "end_ms": 8600, "text": "原文", "translation": "译文"}]`，
  时间是原视频里的毫秒；`project.subtitles` 控制 `burn_in`（是否烧进画面）和 `show`（both / original / translation）
- 标题卡：`{"type": "title", "text": "第 1 天 · 东京", "subtitle": "", "duration_ms": 2000, "background": "blur_next"}`
  （`background` 为 `blur_next` 时用下一段画面模糊做背景，`black` 为黑底）
- 画面文字：在 clip 里加 `"caption": {"text": "浅草寺", "position": "bottom", "start_ms": 0, "end_ms": 3000}`
  （`position` 可选 `bottom` / `center` / `top`，时间相对这一段的开头）
- 找回被丢弃的片段：从 `excluded` 里拿 `media_id`，照着别的 clip 写一条
- 竖屏处理：`project.portrait_mode` 为 `blur_fill`（模糊背景）或其他值（黑边）

只有改动过的片段会重新渲染，其他直接用缓存。

## 网页编辑器

不想手改 JSON，可以用网页编辑同一个 `project.json`（服务只用 Python 标准库；出片和扫描时通过 `uv` 调用 `vidcut.py`）：

```bash
python3 web.py -w work
```

浏览器打开 http://127.0.0.1:8765 ，可以：

- **顺序**：拖动 ⋮⋮ 或用 ↑↓；“按拍摄时间排序”一键恢复时间顺序（片头固定在最前，标题卡和间隔跟着后面那段走）
- **裁剪**：改入点、出点、音量、淡入淡出；点击卡片在右侧预览，用“设为入点 / 设为出点”精确裁剪
- **增删换**：✕ 把片段移回素材库；“素材库”列出所有没用到的视频，点“加入”按拍摄时间插回；
  “替换”换成另一段素材，保留位置、音量和淡入淡出
- **新素材**：“扫描新素材”在后台跑 `vidcut.py update`（转录、摘要都会做），完成后出现在素材库
- **标题**：“+ 标题卡”插入整屏标题；每段可加“画面文字”（位置、起止时间可调，默认填入 AI 标题）
- **语音**：每段卡片逐句列出语音原文（中文就显示中文，英文就显示英文），入出点范围外的句子标为已裁掉；
  点一句跳到那个位置；预览时播放器按时间显示当前这句
- **预览**：“预览整条”在浏览器里实时播放，包括裁剪、音量、淡入淡出、间隔、标题卡和画面文字
- **保存**：撤销/重做（Cmd/Ctrl+Z）、保存（Cmd/Ctrl+S）；保存前会校验，旧版本自动备份到 `work/history/`
- **出片**：“出草稿 / 出 1080p 成片”在后台调用 `vidcut.py render`，完成后可直接播放和下载

加入新素材时的语音识别：Apple Silicon 上默认用 mlx-whisper，并优先使用 `models/` 下已下载的模型；
可用 `--asr none` 关闭，或 `--asr-model` 指定模型。

服务默认只监听本机（127.0.0.1）。网页和命令行改的是同一个文件，不要两边同时改。

### PHP 版

`index.php` 是同一个编辑器的 PHP 实现（PHP 8.1+，页面、接口、功能都与 `web.py` 相同），二选一即可：

```bash
PHP_CLI_SERVER_WORKERS=4 php -S 127.0.0.1:8765 index.php
VIDCUT_WORK=work_real PHP_CLI_SERVER_WORKERS=4 php -S 127.0.0.1:8765 index.php   # 其他工作目录
```

- `PHP_CLI_SERVER_WORKERS=4` 让 PHP 自带服务器同时处理多个请求（播放视频时还能保存、轮询进度），不要省略
- 也可以放到 Apache 下：`.htaccess` 会把所有请求交给 `index.php`，工作目录里的文件不会被直接访问；
  用 `SetEnv VIDCUT_WORK ...` 指定工作目录。需要开启 mod_rewrite，且网站进程对工作目录有写权限
- 其他环境变量：`VIDCUT_ASR`（auto / mlx-whisper / faster-whisper / none）、`VIDCUT_ASR_MODEL`、`VIDCUT_UV`（uv 的路径）
- PHP 没有常驻线程，渲染和扫描新素材作为独立后台进程运行，状态和日志写在 `<工作目录>/.job/`

## 演示了方案里的哪些点

- 原素材只读、内容哈希 ID、按拍摄时间和 GPS 分组（含按经度估算当地日期）
- 统一格式：混合帧率（25/30）、竖屏、无音轨视频、HDR 转 SDR（需要 ffmpeg 带 zscale）
- AI 初剪：误触片段（全程黑屏、不足 2 秒）自动排除，开头黑屏和首尾静音自动裁掉，每条建议都写明理由
- 视觉 + 语音一起做摘要；Claude 不可用时自动退回规则，整批不会因此失败
- 多轨道、整数毫秒的 schema v2；分段渲染缓存；响度标准化；字幕；标题卡和画面文字

## 还没做

- 转场、背景音乐、照片
- OTIO 导出、任务队列、断点续传
- 模糊检测只是粗略的相对阈值，抖动检测还没做
