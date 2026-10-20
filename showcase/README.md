# vidcut：AI 视频初剪命令行演示

方案 v2 的最小可运行版本：没有网页、没有数据库、没有队列，只有一个 Python 脚本加 FFmpeg。
每一步的结果都存成 JSON 文件，后面做 Web UI 时可以直接复用这些数据格式。

## 需要

- `ffmpeg` / `ffprobe`（`brew install ffmpeg`）
- [`uv`](https://docs.astral.sh/uv/)（自动安装 Python 依赖）
- 可选：Claude API key（`ANTHROPIC_API_KEY`），用于看图写摘要、给初剪建议；没有就用规则兜底
- 可选：语音识别 `mlx-whisper`（Apple Silicon）或 `faster-whisper`（其他平台），由 `uv run --with` 临时装上

## 快速开始

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
| `transcribe` | Whisper 转录；丢弃落在静音里的“幻觉”句子 | `transcript.json` |
| `summarize` | Claude 看关键帧 + 转录 + 检测结果 → 标题、摘要、保留/丢弃、入出点、理由 | `summary.json` |
| `roughcut` | 按拍摄时间排序，按“同一天 + 30 公里内”分组，组间留黑场，组首尾淡入淡出 | `work/project.json`、`work/roughcut.md` |
| `render` | 每段单独渲染并缓存 → 拼接 → 两遍 loudnorm（-16 LUFS）；转录映射成字幕 | `work/out/*.mp4`、`*.srt` |

每一步都会跳过已经做完的部分，加 `--force` 重做。`all` 等于依次跑 scan → roughcut。

## 用命令行“剪辑”

`roughcut` 之后直接编辑 `work/project.json`，然后重新 `render --draft` 看效果：

- 调顺序：移动 `tracks[0].items` 里的条目
- 改裁剪：改 `in_ms` / `out_ms`（毫秒，相对原视频）
- 调音量：`volume_db`
- 改间隔：增删 `{"type": "gap", "duration_ms": 1000}`
- 找回被丢弃的片段：从 `excluded` 里拿 `media_id`，照着别的 clip 写一条
- 竖屏处理：`project.portrait_mode` 为 `blur_fill`（模糊背景）或其他值（黑边）

只有改动过的片段会重新渲染，其他直接用缓存。

## 演示了方案里的哪些点

- 原素材只读、内容哈希 ID、按拍摄时间和 GPS 分组（含按经度估算当地日期）
- 统一格式：混合帧率（25/30）、竖屏、无音轨视频、HDR 转 SDR（需要 ffmpeg 带 zscale）
- AI 初剪：误触片段（全程黑屏、不足 2 秒）自动排除，开头黑屏和首尾静音自动裁掉，每条建议都写明理由
- 视觉 + 语音一起做摘要；Claude 不可用时自动退回规则，整批不会因此失败
- 多轨道、整数毫秒的 schema v2；分段渲染缓存；响度标准化；字幕

## 还没做

- Web UI 和实时预览（现在用 `render --draft` 代替）
- 转场、背景音乐、标题、照片
- OTIO 导出、任务队列、断点续传
- 模糊检测只是粗略的相对阈值，抖动检测还没做
