#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["anthropic", "pillow"]
# ///
"""vidcut —— AI 家庭/旅行视频初剪的命令行演示。

流水线（每一步结果都写进工作目录，可单独重跑）：
  scan       扫描素材目录 → media.json（内容哈希 ID、时长、帧率、旋转、HDR、拍摄时间、GPS）
  proxy      生成 720p 代理文件和缩略图（统一帧率、SDR、补静音音轨）
  analyze    在代理上检测静音、黑屏、场景切换、模糊度，抽关键帧
  transcribe 语音转录，自动识别中文/英文等（mlx-whisper / faster-whisper，未安装则跳过）
  transcript 把所有语音整理成文字稿 transcript.md（transcribe 结束时也会自动生成）
  translate  把语音翻译成另一种语言（中文→英文，英文→中文）；Claude 优先，不可用时用离线的 Argos Translate
  summarize  用 Claude 看关键帧 + 转录，生成摘要和初剪建议（无 API 时用规则兜底）
  roughcut   生成 project.json（按时间/地点分组排序），可手动编辑
  render     分段渲染 + 转场 + 字幕 + 响度标准化 → MP4 和 SRT；--draft 用代理快速出预览视频
  auto       一条命令全自动：scan → … → roughcut → 预览视频（--final 同时出成片）；参数在 settings.json
  all        依次执行 scan → roughcut
  update     加入新素材：重新扫描已知目录（及新目录），只处理新增视频，不改 project.json
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".mts"}
MODEL = os.environ.get("VIDCUT_MODEL", "claude-opus-5-5")
SCHEMA_VERSION = 2


# ───────────────────────── 通用工具 ─────────────────────────

def log(msg: str) -> None:
    print(msg, flush=True)


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode != 0:
        tail = "\n".join(p.stderr.strip().splitlines()[-15:])
        raise RuntimeError(f"命令失败: {' '.join(cmd[:6])} …\n{tail}")
    return p


def ffmpeg(*args: str) -> subprocess.CompletedProcess:
    return run(["ffmpeg", "-hide_banner", "-nostdin", "-y", *args])


def load_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)


_ffmpeg_major: int | None = None


def ffmpeg_major() -> int:
    global _ffmpeg_major
    if _ffmpeg_major is None:
        m = re.search(r"ffmpeg version n?(\d+)", run(["ffmpeg", "-version"]).stdout)
        _ffmpeg_major = int(m.group(1)) if m else 99  # 开发版本号不是数字时按新版本处理
    return _ffmpeg_major


_filters_cache: str | None = None


def has_filter(name: str) -> bool:
    global _filters_cache
    if _filters_cache is None:
        _filters_cache = run(["ffmpeg", "-hide_banner", "-filters"]).stdout
    return re.search(rf"\s{re.escape(name)}\s", _filters_cache) is not None


class Workspace:
    def __init__(self, root: Path):
        self.root = root
        self.media_file = root / "media.json"
        self.project_file = root / "project.json"
        self.out = root / "out"

    def derived(self, media_id: str) -> Path:
        d = self.root / "derived" / media_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def media(self) -> dict:
        data = load_json(self.media_file)
        if not data:
            sys.exit("还没有 media.json，请先运行: vidcut.py scan <素材目录>")
        return data["media"]


# ───────────────────────── settings.json：自动化参数 ─────────────────────────

HERE = Path(__file__).resolve().parent
TRANSITIONS = {"cut", "fade", "fadeblack", "fadewhite", "dissolve", "wipeleft", "wiperight", "slideleft",
               "slideright", "smoothleft", "smoothright", "circleopen", "circleclose", "radial", "zoomin", "distance"}

DEFAULT_SETTINGS = {
    "_说明": {
        "name": "片名，用在片头标题和输出文件名上",
        "output": "成片分辨率/帧率；preview_resolution 是预览视频的分辨率；portrait_mode 竖屏处理：blur_fill 模糊背景 / pad 黑边",
        "long_clips": "超过 threshold_s 秒的视频只保留开头 head_s 秒和结尾 tail_s 秒（在 AI 建议的入出点范围内取）；threshold_s 设为 0 关闭",
        "transitions": "片段之间的转场：type 可选 fade / fadeblack / dissolve / wipeleft / slideleft / smoothleft / circleopen / cut（无转场）等；"
                       "group_type 用在换天/换地点处；part_type 用在同一视频的开头和结尾之间",
        "titles": "opening 片头标题卡，groups 每天/每个地点的标题卡，captions 把 AI 标题叠在每段画面上，group_km 同组地点半径",
        "audio": "auto_volume 按响度自动调每段音量：有人说话的段落调到 speech_lufs，环境声调到 ambient_lufs（最多提升 ambient_max_boost_db），"
                 "单段最多调整 max_gain_db；final_lufs 整片最终响度",
        "subtitles": "burn_in 把语音字幕烧进视频；translate 自动翻译（中文↔英文）；show 显示 both 原文+译文 / original 只原文 / translation 只译文",
        "speech": "language 语音语言（auto 自动识别）；whisper_model 语音识别模型（null 用 models/ 下已下载的或默认模型）",
    },
    "name": "My Trip",
    "output": {"resolution": [1920, 1080], "fps": 30, "preview_resolution": [960, 540], "portrait_mode": "blur_fill"},
    "long_clips": {"threshold_s": 15, "head_s": 5, "tail_s": 5},
    "transitions": {"type": "fade", "duration_s": 0.6, "group_type": "fadeblack", "group_duration_s": 0.8,
                    "part_type": "fade", "part_duration_s": 0.5},
    "titles": {"opening": True, "groups": True, "captions": False, "group_km": 30},
    "audio": {"auto_volume": True, "speech_lufs": -16, "ambient_lufs": -23, "ambient_max_boost_db": 6,
              "max_gain_db": 12, "final_lufs": -16},
    "subtitles": {"burn_in": True, "translate": True, "show": "both"},
    "speech": {"language": "auto", "whisper_model": None},
}


def merge_defaults(cur: dict, default: dict) -> dict:
    """用户的 settings.json 缺了新版本加的字段时，用默认值补上（不覆盖用户的值）。"""
    out = dict(cur)
    for k, v in default.items():
        if k not in out:
            out[k] = v
        elif isinstance(v, dict) and isinstance(out[k], dict) and k != "_说明":
            out[k] = merge_defaults(out[k], v)
    return out


def load_settings(ws: Workspace, name: str | None = None) -> dict:
    path = ws.root / "settings.json"
    cur = load_json(path, {})
    s = merge_defaults(cur, DEFAULT_SETTINGS)
    s["_说明"] = DEFAULT_SETTINGS["_说明"]  # 说明文字始终用最新版本
    if name:
        s["name"] = name
    if s != cur:
        save_json(path, s)
        log(f"设置文件 → {path}（可直接编辑后重新运行）")
    tr = s["transitions"]
    for k in ("type", "group_type", "part_type"):
        if tr[k] not in TRANSITIONS:
            sys.exit(f"settings.json: transitions.{tr[k]} 不是可用的转场，可选：{', '.join(sorted(TRANSITIONS))}")
    return s


def apple_silicon() -> bool:
    import platform
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def default_whisper_model() -> str | None:
    if os.environ.get("VIDCUT_WHISPER_MODEL"):  # Docker 镜像里用它指定 faster-whisper 模型
        return os.environ["VIDCUT_WHISPER_MODEL"]
    local = sorted((HERE / "models").glob("*whisper*")) if apple_silicon() else []
    return str(local[0]) if local else None


def reexec_with(package: str, argv: list[str], timeout: float | None = None) -> bool:
    """当前环境缺少某个可选依赖时，用 uv 临时带上它重新运行这一步。"""
    if os.environ.get("VIDCUT_REEXEC") or not shutil.which("uv"):
        return False
    log(f"  （自动切换到带 {package} 的环境运行这一步）")
    env = {**os.environ, "VIDCUT_REEXEC": "1"}
    try:
        return subprocess.run(["uv", "run", "--with", package, str(Path(__file__).resolve()), *argv],
                              env=env, timeout=timeout).returncode == 0
    except subprocess.TimeoutExpired:
        log(f"  超过 {timeout:.0f} 秒仍未完成，跳过这一步（下次运行会继续）")
        return False


def measure_lufs(src: Path, start: float, dur: float) -> float | None:
    """一段音频的综合响度（LUFS）；静音或太短时返回 None。"""
    # 用 loudnorm 的测量结果（各版本 ffmpeg 都支持；ebur128 在 ffmpeg 5.1 上会出错却仍输出 0 LUFS）
    p = run(["ffmpeg", "-hide_banner", "-nostdin", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
             "-vn", "-af", "loudnorm=print_format=json", "-f", "null", "-"], check=False)
    if p.returncode != 0 or "{" not in p.stderr:
        return None
    try:
        value = float(json.loads(p.stderr[p.stderr.rindex("{"):p.stderr.rindex("}") + 1])["input_i"])
    except (ValueError, KeyError):
        return None
    return value if -60 < value < 0 else None


def media_duration(path: Path) -> float:
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration",
               "-of", "default=nw=1:nk=1", str(path)]).stdout.strip()
    return float(out)


# ───────────────────────── scan ─────────────────────────

def quick_hash(path: Path) -> str:
    """大小 + 头尾各 1MB 的哈希：几十 GB 素材也能秒级去重。"""
    size = path.stat().st_size
    h = hashlib.sha256(str(size).encode())
    with path.open("rb") as f:
        h.update(f.read(1 << 20))
        if size > 2 << 20:
            f.seek(-(1 << 20), os.SEEK_END)
            h.update(f.read(1 << 20))
    return "m_" + h.hexdigest()[:16]


def parse_rate(s: str | None) -> float:
    if not s or s in ("0/0", "0"):
        return 0.0
    num, _, den = s.partition("/")
    return float(num) / float(den or 1)


def parse_iso6709(s: str | None):
    m = re.match(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)", s or "")
    return {"lat": float(m.group(1)), "lon": float(m.group(2))} if m else None


def parse_time(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def probe(path: Path) -> dict:
    info = json.loads(run(["ffprobe", "-v", "error", "-print_format", "json",
                           "-show_format", "-show_streams", str(path)]).stdout)
    fmt = info.get("format", {})
    tags = {k.lower(): v for k, v in fmt.get("tags", {}).items()}
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if v is None:
        raise ValueError("没有视频流")

    rotation = 0
    for sd in v.get("side_data_list", []):
        if "rotation" in sd:
            rotation = int(sd["rotation"])
    rotation = rotation or int(v.get("tags", {}).get("rotate", 0))
    w, h = v["width"], v["height"]
    if abs(rotation) % 180 == 90:
        w, h = h, w

    avg, real = parse_rate(v.get("avg_frame_rate")), parse_rate(v.get("r_frame_rate"))
    ctime = (tags.get("com.apple.quicktime.creationdate") or tags.get("creation_time"))
    t = parse_time(ctime) or dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
    loc = tags.get("com.apple.quicktime.location.iso6709") or tags.get("location")
    return {
        "path": str(path.resolve()),
        "duration_ms": int(float(fmt.get("duration", v.get("duration", 0))) * 1000),
        "width": w, "height": h,
        "portrait": h > w,
        "rotation": rotation,
        "fps": round(avg or real, 3),
        "vfr": bool(avg and real and abs(avg - real) > 0.05),
        "vcodec": v.get("codec_name"),
        "hdr": v.get("color_transfer") in ("arib-std-b67", "smpte2084"),
        "has_audio": a is not None,
        "created": t.isoformat(),
        "created_source": "metadata" if ctime else "file_mtime",
        "gps": parse_iso6709(loc),
    }


def cmd_scan(ws: Workspace, args) -> None:
    folder = Path(args.folder).expanduser()
    files = sorted(p for p in folder.rglob("*")
                   if p.suffix.lower() in VIDEO_EXTS and not p.name.startswith("."))
    if not files:
        sys.exit(f"{folder} 下没有找到视频文件")
    old = load_json(ws.media_file) or {}
    existing = old.get("media", {})
    # 同一次扫描里内容相同 → 重复；以前扫描过的内容出现在新位置 → 视为文件搬家，更新路径
    media, dupes, seen = dict(existing), 0, {}
    for p in files:
        mid = quick_hash(p)
        if mid in seen:
            dupes += 1
            log(f"  重复: {p.name} 与 {seen[mid]} 内容相同，跳过")
            continue
        seen[mid] = p.name
        try:
            media[mid] = {"id": mid, "name": p.name, **probe(p)}
        except Exception as e:  # noqa: BLE001 —— 坏文件不应中断整批
            log(f"  无法读取 {p.name}: {e}")
            continue
        m = media[mid]
        flags = [f for f, on in [("竖屏", m["portrait"]), ("HDR", m["hdr"]),
                                 ("可变帧率", m["vfr"]), ("无音轨", not m["has_audio"])] if on]
        log(f"  {mid}  {p.name:<16} {m['duration_ms']/1000:6.1f}s  {m['width']}x{m['height']} "
            f"{m['fps']:g}fps  {m['created'][:16]}  {' '.join(flags)}")
    ws.root.mkdir(parents=True, exist_ok=True)
    sources = sorted(set(old.get("sources", [])) | {str(folder.resolve())})
    save_json(ws.media_file, {"schema_version": SCHEMA_VERSION, "sources": sources, "media": media})
    log(f"扫描完成：{len(media)} 个媒体（跳过重复 {dupes} 个）→ {ws.media_file}")


# ───────────────────────── proxy ─────────────────────────

def hdr_to_sdr_chain() -> str | None:
    if has_filter("zscale") and has_filter("tonemap"):
        return ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                "tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv")
    return None


def cmd_proxy(ws: Workspace, args) -> None:
    for mid, m in ws.media().items():
        d = ws.derived(mid)
        proxy, thumb = d / "proxy.mp4", d / "thumb.jpg"
        if proxy.exists() and not args.force:
            continue
        vf = []
        if m["hdr"]:
            chain = hdr_to_sdr_chain()
            if chain:
                vf.append(chain)
            else:
                log(f"  警告: {m['name']} 是 HDR，但本机 ffmpeg 缺少 zscale，颜色可能发灰")
        # ffmpeg 解码时会自动应用旋转；长边缩到 1280，统一 30fps
        vf += ["scale='if(gt(iw,ih),1280,-2)':'if(gt(iw,ih),-2,1280)'", "fps=30", "format=yuv420p"]
        cmd = ["-i", m["path"]]
        if not m["has_audio"]:
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        cmd += ["-map", "0:v:0", "-map", "0:a:0" if m["has_audio"] else "1:a",
                "-vf", ",".join(vf), "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
                "-g", "30", "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
                "-shortest", "-movflags", "+faststart", str(proxy)]
        log(f"  代理: {m['name']}")
        ffmpeg(*cmd)
        ffmpeg("-ss", f"{m['duration_ms'] / 3000:.2f}", "-i", str(proxy),
               "-frames:v", "1", "-vf", "scale=320:-2", str(thumb))
    log("代理文件已生成")


# ───────────────────────── analyze ─────────────────────────

def merge_intervals(iv: list[list[float]]) -> list[list[float]]:
    out: list[list[float]] = []
    for s, e in sorted(iv):
        if out and s <= out[-1][1] + 0.05:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def analyze_one(m: dict, proxy: Path, d: Path) -> dict:
    dur = m["duration_ms"] / 1000
    meta = d / "frames_meta.txt"
    vf = (f"fps=5,blackdetect=d=0.4:pix_th=0.10,scdet=t=12,"
          f"blurdetect=block_width=32:block_height=32,metadata=print:file={meta}")
    af = "silencedetect=n=-35dB:d=0.8,volumedetect"
    p = run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(proxy),
             "-vf", vf, "-af", af, "-f", "null", "-"])
    err = p.stderr

    silence, start = [], None
    for line in err.splitlines():
        if (s := re.search(r"silence_start: ([\d.]+)", line)):
            start = float(s.group(1))
        elif (e := re.search(r"silence_end: ([\d.]+)", line)) and start is not None:
            silence.append([start, float(e.group(1))])
            start = None
    if start is not None:
        silence.append([start, dur])
    black = [[float(a), float(b)] for a, b in
             re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", err)]
    mean_vol = re.search(r"mean_volume: (-?[\d.]+) dB", err)

    cuts, blur, t = [], [], 0.0
    for line in meta.read_text().splitlines() if meta.exists() else []:
        if (fm := re.search(r"pts_time:([\d.]+)", line)):
            t = float(fm.group(1))
        elif line.startswith("lavfi.scd.time="):
            cuts.append(round(t, 2))
        elif line.startswith("lavfi.blur="):
            blur.append((t, float(line.split("=")[1])))
    meta.unlink(missing_ok=True)

    # 视频的“模糊”是相对的：比本片中位数明显更糊的连续区段
    blurry = []
    if blur:
        vals = sorted(b for _, b in blur)
        thr = max(vals[len(vals) // 2] * 1.8, 6.0)
        blurry = merge_intervals([[t, t + 0.2] for t, b in blur if b > thr])
        blurry = [iv for iv in blurry if iv[1] - iv[0] >= 1.0]

    # 关键帧：每个场景取中点，跳过黑屏，最多 4 张
    bounds = [0.0, *[c for c in cuts if 0.5 < c < dur - 0.5], dur]
    mids = [(a + b) / 2 for a, b in zip(bounds, bounds[1:])]
    mids = [x for x in mids if not any(s <= x <= e for s, e in black)] or [dur / 2]
    if len(mids) > 4:
        mids = [mids[round(i * (len(mids) - 1) / 3)] for i in range(4)]
    if len(mids) == 1 and dur > 6:
        mids = [dur * 0.25, dur * 0.5, dur * 0.75]
        mids = [x for x in mids if not any(s <= x <= e for s, e in black)] or mids
    for old in d.glob("kf_*.jpg"):
        old.unlink()
    keyframes = []
    for i, x in enumerate(mids):
        kf = d / f"kf_{i}.jpg"
        ffmpeg("-ss", f"{x:.2f}", "-i", str(proxy), "-frames:v", "1",
               "-vf", "scale=512:-2", "-q:v", "4", str(kf))
        keyframes.append({"t": round(x, 2), "file": kf.name})

    return {
        "silence": [[round(a, 2), round(b, 2)] for a, b in silence],
        "black": [[round(a, 2), round(b, 2)] for a, b in black],
        "scene_cuts": cuts,
        "blurry": [[round(a, 2), round(b, 2)] for a, b in blurry],
        "mean_volume_db": float(mean_vol.group(1)) if mean_vol else None,
        "keyframes": keyframes,
    }


def cmd_analyze(ws: Workspace, args) -> None:
    for mid, m in ws.media().items():
        d = ws.derived(mid)
        out, proxy = d / "analysis.json", d / "proxy.mp4"
        if not proxy.exists():
            sys.exit("缺少代理文件，请先运行 proxy")
        if out.exists() and not args.force:
            continue
        a = analyze_one(m, proxy, d)
        save_json(out, a)
        log(f"  {m['name']:<16} 静音{len(a['silence'])}段 黑屏{len(a['black'])}段 "
            f"场景切换{len(a['scene_cuts'])} 模糊{len(a['blurry'])}段 "
            f"平均音量 {a['mean_volume_db']} dB  关键帧{len(a['keyframes'])}")
    log("分析完成")


# ───────────────────────── transcribe ─────────────────────────

# 中文提示词让 Whisper 输出简体和标点；语言自动检测时也加上，Whisper 会先按音频判断语言再解码
ZH_PROMPT = "以下是普通话的句子。"


def load_asr(model: str | None):
    """返回 (名称, asr)；asr(wav, language) → (语言, [(开始, 结束, 文字)])，language=None 为自动检测。"""
    try:
        import mlx_whisper  # Apple Silicon

        repo = model or "mlx-community/whisper-large-v3-turbo"

        def asr(wav: Path, language: str | None):
            r = mlx_whisper.transcribe(str(wav), path_or_hf_repo=repo, language=language, word_timestamps=True,
                                       initial_prompt=ZH_PROMPT if language in (None, "zh") else None)
            return r.get("language") or language, [(s["start"], s["end"], s["text"].strip()) for s in r["segments"]]
        return f"mlx-whisper ({repo})", asr
    except ImportError:
        pass
    try:
        from faster_whisper import WhisperModel

        size = model or "large-v3-turbo"
        fw = WhisperModel(size, device="auto", compute_type="int8")

        def asr(wav: Path, language: str | None):
            # 自己读 ffmpeg 生成的 16kHz 单声道 WAV 交给模型，不经过 faster-whisper 的 PyAV 解码
            # （新版 PyAV 改了接口，会让 faster-whisper 读文件时报错）
            import wave
            import numpy as np
            with wave.open(str(wav)) as w:
                audio = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
            segs, info = fw.transcribe(audio, language=language, vad_filter=True, word_timestamps=True,
                                       initial_prompt=ZH_PROMPT if language in (None, "zh") else None)
            return info.language, [(s.start, s.end, s.text.strip()) for s in segs]
        return f"faster-whisper ({size})", asr
    except ImportError:
        return None, None


def cmd_transcribe(ws: Workspace, args) -> None:
    model = args.model or default_whisper_model()
    name, asr = load_asr(model)
    if asr is None:
        argv = (["-w", str(ws.root.resolve()), "transcribe"] + (["--force"] if args.force else [])
                + (["--language", args.language] if args.language else []) + (["--model", model] if model else []))
        if reexec_with("mlx-whisper" if apple_silicon() else "faster-whisper", argv):
            return
        log("未安装语音识别库，跳过转录。启用方法（二选一）：\n"
            "  uv run --with mlx-whisper vidcut.py transcribe      # Apple Silicon\n"
            "  uv run --with faster-whisper vidcut.py transcribe   # 其他平台")
        return
    log(f"使用 {name}")
    for mid, m in ws.media().items():
        d = ws.derived(mid)
        out = d / "transcript.json"
        if out.exists() and not args.force:
            continue
        a = load_json(d / "analysis.json", {})
        dur = m["duration_ms"] / 1000
        silent = sum(e - s for s, e in a.get("silence", []))
        if not m["has_audio"] or silent > dur * 0.95:
            save_json(out, {"engine": name, "language": None, "segments": []})
            log(f"  {m['name']:<16} 无有效语音")
            continue
        # 按静音把音频切成几个说话区段，逐段识别：长时间静音后 Whisper 容易漏掉后面的话，
        # 而且每段可以各自判断语言（前面说中文、后面说英文的视频很常见）
        keep, lang_dur = [], {}
        wav = d / "region16k.wav"
        for r0, r1 in speech_regions(a.get("silence", []), dur):
            ffmpeg("-ss", f"{r0:.3f}", "-t", f"{r1 - r0:.3f}", "-i", str(d / "proxy.mp4"),
                   "-vn", "-ac", "1", "-ar", "16000", str(wav))
            lang, segs = asr(wav, args.language)
            for s, e, text in segs:
                s, e = r0 + s, min(r0 + e, r1)
                # Whisper 在静音段容易“编”出文字：丢掉大部分落在静音里的句子
                overlap = sum(max(0, min(e, b) - max(s, a_)) for a_, b in a.get("silence", []))
                if text and e > s and overlap < (e - s) * 0.7:
                    keep.append({"start": round(s, 2), "end": round(e, 2), "text": text, "lang": lang})
                    lang_dur[lang] = lang_dur.get(lang, 0) + e - s
        wav.unlink(missing_ok=True)
        main = max(lang_dur, key=lang_dur.get) if keep else None
        save_json(out, {"engine": name, "language": main, "segments": keep})
        langs = "/".join(sorted(lang_dur, key=lang_dur.get, reverse=True)) or "-"
        log(f"  {m['name']:<16} [{langs}] " + (" ".join(x["text"] for x in keep)[:70] or "（无）"))
    log("转录完成")
    write_transcript(ws)


def speech_regions(silence: list[list[float]], dur: float) -> list[tuple[float, float]]:
    """静音区段的补集，就是可能有人说话的区段；间隔很短的合并，两头各留一点余量。"""
    regions, t = [], 0.0
    for s, e in sorted(silence):
        if s > t:
            regions.append([t, s])
        t = max(t, e)
    if t < dur:
        regions.append([t, dur])
    merged: list[list[float]] = []
    for s, e in regions:
        if merged and s - merged[-1][1] < 0.8:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(max(0.0, s - 0.25), min(dur, e + 0.25)) for s, e in merged if e - s >= 0.3] or [(0.0, dur)]


LANG_NAMES = {"zh": "中文", "en": "英文", "ja": "日语", "ko": "韩语", "yue": "粤语", "fr": "法语", "de": "德语", "es": "西班牙语"}


def mmss(sec: float) -> str:
    return f"{int(sec // 60)}:{sec % 60:04.1f}"


def write_transcript(ws: Workspace) -> Path:
    """把所有视频里的语音按拍摄时间整理成一份文字稿。"""
    media = sorted(ws.media().values(), key=lambda m: m["created"])
    lines, counts = [], {}
    for m in media:
        tr = load_json(ws.derived(m["id"]) / "transcript.json")
        if not tr or not tr["segments"]:
            continue
        code = tr.get("language")
        if not code:  # 旧版本没记录语言：有汉字就算中文
            code = "zh" if re.search(r"[\u4e00-\u9fff]", "".join(x["text"] for x in tr["segments"])) else "en"
        lang = LANG_NAMES.get(code, code)
        counts[lang] = counts.get(lang, 0) + 1
        lines += ["", f"## {m['name']} · {local_time(m):%Y-%m-%d %H:%M} · {lang}", ""]
        for x in tr["segments"]:
            lines.append(f"[{mmss(x['start'])}–{mmss(x['end'])}] {x['text']}  ")
            if x.get("translation"):
                lines.append(f"　　→ {x['translation']}  ")
    stats = "，".join(f"{k} {v} 段" for k, v in counts.items())
    head = ["# 语音文字稿", "", f"共 {sum(counts.values())} 段视频有语音" + (f"（{stats}）" if stats else "") +
            "。时间是相对每段原视频的开头；按拍摄时间排序。"]
    out = ws.root / "transcript.md"
    out.write_text("\n".join(head + lines) + "\n")
    log(f"文字稿 → {out}")
    return out


def cmd_transcript(ws: Workspace, args) -> None:
    out = write_transcript(ws)
    print(out.read_text())


# ───────────────────────── translate：中文↔英文字幕翻译 ─────────────────────────

TRANSLATE_SYSTEM = """你是字幕翻译。把每一行字幕翻译成{target}：口语、简洁、适合做字幕；地名和专有名词用通行译名。
原文来自语音识别，可能有同音错字，请结合上下文按正确的意思翻译。translations 与输入的 lines 一一对应、数量相同。"""
TRANSLATE_SCHEMA = {"type": "object", "properties": {"translations": {"type": "array", "items": {"type": "string"}}},
                    "required": ["translations"], "additionalProperties": False}


def target_lang(lang: str | None) -> str:
    return "en" if lang in ("zh", "yue") else "zh"


def has_claude_credentials() -> bool:
    """环境变量或 `ant auth login` 的登录信息；都没有就不调用 Claude（避免每段都报一次错）。"""
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or (Path.home() / ".config" / "anthropic").exists())


def claude_client(offline: bool):
    if offline:
        return None
    if not has_claude_credentials():
        log("未设置 ANTHROPIC_API_KEY，不使用 Claude")
        return None
    try:
        import anthropic
        return anthropic.Anthropic()
    except Exception as e:  # noqa: BLE001
        log(f"无法初始化 Claude 客户端（{e}）")
        return None


def is_account_error(e: Exception) -> bool:
    """账号/请求层面的错误（余额、权限等），后面的请求也会同样失败。"""
    import anthropic
    return (isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.BadRequestError))
            or "authentication" in str(e).lower())


def claude_translate(client, lines: list[str], target: str) -> list[str]:
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=TRANSLATE_SYSTEM.format(target="英文" if target == "en" else "简体中文"),
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": TRANSLATE_SCHEMA}},
        messages=[{"role": "user", "content": json.dumps({"lines": lines}, ensure_ascii=False)}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("模型拒绝了该请求")
    out = json.loads(next(b.text for b in resp.content if b.type == "text"))["translations"]
    if len(out) != len(lines):
        raise RuntimeError(f"译文行数 {len(out)} 与原文 {len(lines)} 不一致")
    return out


def download_resumable(urls: list[str], name: str, attempts: int = 8) -> Path:
    """断点续传下载到 ~/.cache/vidcut/；网络中断时从已下载的位置继续。"""
    import urllib.request
    dest = Path.home() / ".cache" / "vidcut" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    if dest.exists():
        return dest
    last = None
    for attempt in range(attempts):
        for url in urls:
            have = part.stat().st_size if part.exists() else 0
            # 下载服务器会拒绝 Python 默认的 User-Agent（403）
            headers = {"User-Agent": "Mozilla/5.0 (vidcut)", **({"Range": f"bytes={have}-"} if have else {})}
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    if have and r.status != 206:  # 服务器不支持续传，从头下载
                        have = 0
                        part.unlink(missing_ok=True)
                    total = have + int(r.headers.get("Content-Length") or 0)
                    with part.open("ab") as f:
                        while chunk := r.read(1 << 20):
                            f.write(chunk)
                if total and part.stat().st_size >= total:
                    part.replace(dest)
                    return dest
            except Exception as e:  # noqa: BLE001
                last = e
                log(f"  下载中断（{type(e).__name__}），已下载 {part.stat().st_size >> 20 if part.exists() else 0}MB，继续…")
    raise RuntimeError(f"下载失败：{last}")


_argos_ready: set = set()
_argos_failed: dict = {}


def argos_translate(lines: list[str], src: str, target: str) -> list[str]:
    """离线翻译（Argos Translate）；第一次用某个方向时自动下载模型（网络不稳时重试 3 次）。"""
    import argostranslate.package as pkg
    import argostranslate.translate as tr
    src = "zh" if src in ("zh", "yue") else src
    if (src, target) in _argos_failed:  # 这一批里已经失败过，不再重复下载
        raise RuntimeError(_argos_failed[src, target])
    if (src, target) not in _argos_ready:
        if (src, target) not in {(p.from_code, p.to_code) for p in pkg.get_installed_packages()}:
            log(f"  下载离线翻译模型 {src}→{target}（只需一次）…")
            try:
                pkg.update_package_index()
                cand = next((p for p in pkg.get_available_packages() if (p.from_code, p.to_code) == (src, target)), None)
                if cand is None:
                    raise RuntimeError(f"没有 {src}→{target} 的离线翻译模型")
                pkg.install_from_path(download_resumable(cand.links, f"translate-{src}_{target}.argosmodel"))
            except Exception as e:  # noqa: BLE001
                _argos_failed[src, target] = f"{src}→{target} 模型下载失败：{e}"
                raise RuntimeError(_argos_failed[src, target]) from e
        _argos_ready.add((src, target))
    return [tr.translate(x, src, target) for x in lines]


def cmd_translate(ws: Workspace, args) -> None:
    if not load_settings(ws)["subtitles"]["translate"]:
        log("settings.json 里 subtitles.translate 为 false，跳过翻译")
        return
    # 每句按自己的语言决定翻译方向（同一段视频里可能中英都有）
    jobs = []
    for mid, m in ws.media().items():
        path = ws.derived(mid) / "transcript.json"
        tr = load_json(path)
        if tr and tr["segments"]:
            groups: dict[tuple[str, str], list[int]] = {}
            for i, sg in enumerate(tr["segments"]):
                if args.force or not sg.get("translation"):
                    src = sg.get("lang") or tr.get("language") or "en"
                    groups.setdefault((src, target_lang(src)), []).append(i)
            jobs += [(m, path, tr, idx, src, target) for (src, target), idx in groups.items()]
    if not jobs:
        log("没有需要翻译的句子")
        sync_project_subtitles(ws)
        return

    def apply(m, path, tr, idx, target, texts, source):
        for i, t in zip(idx, texts):
            tr["segments"][i].update(translation=t.strip(), translation_lang=target, translation_source=source)
        save_json(path, tr)
        log(f"  {m['name']:<16} → {target}：{tr['segments'][idx[0]]['translation'][:50]}")

    client = claude_client(args.offline)
    remaining = []
    for m, path, tr, idx, src, target in jobs:
        lines = [tr["segments"][i]["text"] for i in idx]
        if client is not None:
            try:
                apply(m, path, tr, idx, target, claude_translate(client, lines, target), MODEL)
                continue
            except Exception as e:  # noqa: BLE001 —— 失败就交给离线翻译
                log(f"  Claude 翻译失败（{type(e).__name__}: {getattr(e, 'message', e)}），改用离线翻译")
                if is_account_error(e):
                    client = None
        remaining.append((m, path, tr, idx, src, target))

    if remaining:
        try:
            import argostranslate  # noqa: F401
        except ImportError:
            # 清掉旧译文再交给带 argostranslate 的子进程，它只处理没有译文的句子
            for _, path, tr, idx, _, _ in remaining:
                for i in idx:
                    tr["segments"][i]["translation"] = ""
                save_json(path, tr)
            # 第一次要安装离线翻译和下载模型，最多等 30 分钟；网络问题不应卡住整个流程
            if not reexec_with("argostranslate", ["-w", str(ws.root.resolve()), "translate", "--offline"], timeout=1800):
                log("无法离线翻译：请安装 uv，或运行 uv run --with argostranslate vidcut.py translate")
            sync_project_subtitles(ws)
            return
        for m, path, tr, idx, src, target in remaining:
            lines = [tr["segments"][i]["text"] for i in idx]
            try:
                apply(m, path, tr, idx, target, argos_translate(lines, src, target), "argos")
            except Exception as e:  # noqa: BLE001
                log(f"  {m['name']}: 离线翻译失败（{e}）")
    write_transcript(ws)
    sync_project_subtitles(ws)
    log("翻译完成")


def sync_project_subtitles(ws: Workspace) -> None:
    """把新翻译填进 project.json 里还没有译文的字幕（不覆盖手动修改过的译文）。"""
    project = load_json(ws.project_file)
    if not project:
        return
    changed = 0
    for it in project["tracks"][0]["items"]:
        if it.get("type") != "clip" or not it.get("subtitles"):
            continue
        segs = load_json(ws.derived(it["media_id"]) / "transcript.json", {}).get("segments", [])
        for sub in it["subtitles"]:
            if sub.get("translation"):
                continue
            seg = next((s for s in segs if round(s["start"] * 1000) == sub["start_ms"] and s.get("translation")), None)
            if seg:
                sub["translation"] = seg["translation"]
                changed += 1
    if changed:
        save_json(ws.project_file, project)
        log(f"  project.json 里补上了 {changed} 句译文")



# ───────────────────────── summarize（AI 初剪建议） ─────────────────────────

def heuristic_suggestion(m: dict, a: dict, segs: list[dict]) -> dict:
    dur = m["duration_ms"] / 1000
    t_in, t_out, reasons = 0.0, dur, []
    for s, e in a.get("black", []):
        if s < 0.3 and e > t_in:
            t_in = e
            reasons.append(f"开头 {e:.1f} 秒黑屏")
        if e > dur - 0.3 and s < t_out:
            t_out = s
            reasons.append(f"结尾 {dur - s:.1f} 秒黑屏")
    if segs:  # 有人说话的片段，修掉首尾长静音
        for s, e in a.get("silence", []):
            if s < 0.3 and e - 0.3 > t_in and e < segs[0]["start"] + 0.1:
                t_in = max(t_in, e - 0.3)
                reasons.append(f"开头 {e:.1f} 秒无声")
            if e > dur - 0.3 and s + 0.5 < t_out and s > segs[-1]["end"] - 0.1:
                t_out = min(t_out, s + 0.5)
                reasons.append(f"结尾 {dur - s:.1f} 秒无声")
    black_total = sum(e - s for s, e in a.get("black", []))
    keep = (t_out - t_in) >= 2.0 and black_total < dur * 0.8
    if not keep:
        reasons = ["几乎全程黑屏"] if black_total >= dur * 0.8 else reasons
        reasons.append("有效内容不足 2 秒，疑似误触")
        t_in, t_out = 0.0, dur  # 丢弃的片段保留完整区间，方便用户改主意时重新加入
    return {"keep": keep, "in_ms": int(t_in * 1000), "out_ms": int(t_out * 1000),
            "reason": "；".join(reasons) or "无需裁剪"}


SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "不超过 12 个字的片段标题"},
        "summary": {"type": "string", "description": "一两句中文摘要，结合画面与语音"},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "keep": {"type": "boolean"},
        "in_ms": {"type": "integer"},
        "out_ms": {"type": "integer"},
        "reason": {"type": "string", "description": "保留/丢弃与裁剪理由，简短中文"},
        "place": {"type": "string", "description": "拍摄地点的简短中文名（城市或景点），无法判断时为空字符串"},
    },
    "required": ["title", "summary", "keywords", "keep", "in_ms", "out_ms", "reason", "place"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """你是家庭与旅行视频的剪辑助理。你会拿到一段原始视频的元数据、几张关键帧、带时间戳的语音转录，以及自动检测到的静音/黑屏/模糊区段和一个基于规则的裁剪建议。

请输出：
- title / summary / keywords：描述这段视频拍了什么。画面内容以关键帧为准，语音内容以转录为准；不要编造关键帧和转录里都没有的细节。如果画面是测试图案或无法辨认，就如实说明。
- keep / in_ms / out_ms / reason：初剪建议。目标是去掉误触、镜头对着口袋或地面、首尾无意义的部分，同时保留有人说话和有画面内容的部分。规则建议通常是合理的起点，有充分理由时再调整。in_ms 和 out_ms 必须在 0 到视频时长之间，且 out_ms 大于 in_ms。
- place：根据 GPS 坐标和画面判断拍摄地点，用简短的中文地名（如“京都”“浅草寺”），会用于分组标题；没有把握就留空。"""


def claude_summary(client, m: dict, a: dict, segs: list[dict], heur: dict, d: Path) -> dict:
    content = []
    for kf in a["keyframes"]:
        data = base64.standard_b64encode((d / kf["file"]).read_bytes()).decode()
        content.append({"type": "text", "text": f"关键帧 @ {kf['t']} 秒："})
        content.append({"type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": data}})
    info = {
        "文件名": m["name"], "时长_ms": m["duration_ms"], "拍摄时间": m["created"],
        "GPS": m["gps"], "竖屏": m["portrait"], "有音轨": m["has_audio"],
        "平均音量_dB": a.get("mean_volume_db"),
        "静音区段_秒": a.get("silence"), "黑屏区段_秒": a.get("black"),
        "模糊区段_秒": a.get("blurry"), "场景切换_秒": a.get("scene_cuts"),
        "转录": [f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segs] or "（无语音）",
        "规则建议": heur,
    }
    content.append({"type": "text", "text": json.dumps(info, ensure_ascii=False, indent=1)})

    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium",
                       "format": {"type": "json_schema", "schema": SUMMARY_SCHEMA}},
        messages=[{"role": "user", "content": content}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("模型拒绝了该请求")
    text = next(b.text for b in resp.content if b.type == "text")
    out = json.loads(text)
    dur = m["duration_ms"]
    out["in_ms"] = max(0, min(int(out["in_ms"]), dur))
    out["out_ms"] = max(0, min(int(out["out_ms"]), dur))
    if out["out_ms"] - out["in_ms"] < 500:
        out["in_ms"], out["out_ms"] = heur["in_ms"], heur["out_ms"]
    return {**out, "source": resp.model}


def cmd_summarize(ws: Workspace, args) -> None:
    client = claude_client(args.offline)
    for mid, m in ws.media().items():
        d = ws.derived(mid)
        out = d / "summary.json"
        if out.exists() and not args.force:
            continue
        a = load_json(d / "analysis.json")
        if a is None:
            sys.exit("缺少分析结果，请先运行 analyze")
        segs = load_json(d / "transcript.json", {"segments": []})["segments"]
        heur = heuristic_suggestion(m, a, segs)
        result = None
        if client is not None:
            import anthropic
            try:
                result = claude_summary(client, m, a, segs, heur, d)
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                    anthropic.BadRequestError) as e:
                # 账号/请求层面的错误，后面的片段也会同样失败：停用 Claude，整批改用规则
                msg = getattr(e, "message", str(e))
                log(f"  Claude 不可用（{type(e).__name__}: {msg}），本批其余片段改用规则建议")
                client = None
            except Exception as e:  # noqa: BLE001 —— 单段失败不影响整批，回退到规则
                log(f"  {m['name']}: Claude 调用失败（{type(e).__name__}: {e}），使用规则建议")
        if result is None:
            speech = "".join(s["text"] for s in segs)
            result = {
                "title": Path(m["name"]).stem,
                "summary": speech[:80] or f"无语音片段，{m['duration_ms']/1000:.0f} 秒，"
                                          f"{len(a['scene_cuts']) + 1} 个镜头",
                "keywords": [], "place": "", **heur, "source": "heuristic",
            }
        save_json(out, result)
        flag = "保留" if result["keep"] else "丢弃"
        log(f"  {m['name']:<16} [{flag}] {result['in_ms']/1000:.1f}–{result['out_ms']/1000:.1f}s  "
            f"{result['title']}：{result['summary']}\n{'':20}理由：{result['reason']}")
    log("摘要与初剪建议完成")


# ───────────────────────── roughcut ─────────────────────────

def local_time(m: dict) -> dt.datetime:
    t = dt.datetime.fromisoformat(m["created"])
    if m["gps"]:
        # 用经度粗略估算当地时区，避免跨时区旅行时按 UTC 分错天
        return (t + dt.timedelta(hours=round(m["gps"]["lon"] / 15))).replace(tzinfo=None)
    return t.astimezone().replace(tzinfo=None)  # 没有 GPS 就用本机时区


def local_date(m: dict) -> dt.date:
    return local_time(m).date()


def distance_km(a: dict | None, b: dict | None) -> float:
    if not a or not b:
        return 0.0
    from math import asin, cos, radians, sin, sqrt
    la1, lo1, la2, lo2 = map(radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = sin((la2 - la1) / 2) ** 2 + cos(la1) * cos(la2) * sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * asin(sqrt(h))


def assign_groups(ordered: list[dict], radius_km: float) -> dict[str, str]:
    """同一天、且离本组第一段不超过 radius_km 的视频归为一组。"""
    groups, first = {}, None
    for m in ordered:
        if (first is None or local_date(m) != local_date(first)
                or distance_km(first["gps"], m["gps"]) > radius_km):
            first = m
        g = first["gps"]
        place = f"{g['lat']:.2f},{g['lon']:.2f}" if g else "未知地点"
        groups[m["id"]] = f"{local_date(first)} · {place}"
    return groups


def group_titles(kept: list[dict], groups: dict[str, str], summaries: dict) -> dict[str, dict]:
    """给每组起标题：多天行程用“第 N 天”，单天用日期；有地名时加上出现最多的地名。"""
    from collections import Counter
    days = sorted({local_date(m) for m in kept})
    meta: dict[str, dict] = {}
    for m in kept:
        g = meta.setdefault(groups[m["id"]], {"date": local_date(m), "places": []})
        if (place := summaries[m["id"]].get("place")):
            g["places"].append(place)
    for g in meta.values():
        d = g["date"]
        day = f"第 {days.index(d) + 1} 天" if len(days) > 1 else f"{d.month} 月 {d.day} 日"
        place = Counter(g["places"]).most_common(1)[0][0] if g["places"] else ""
        g["title"] = f"{day} · {place}" if place else day
        g["subtitle"] = f"{d:%Y.%m.%d}" if len(days) > 1 else ""
    return meta


def split_parts(in_ms: int, out_ms: int, lc: dict) -> list[tuple[int, int, str]]:
    """长视频只取开头和结尾；短视频整段保留。"""
    th, head, tail = lc["threshold_s"] * 1000, round(lc["head_s"] * 1000), round(lc["tail_s"] * 1000)
    if th <= 0 or out_ms - in_ms <= th or head + tail >= out_ms - in_ms:
        return [(in_ms, out_ms, "")]
    return [(in_ms, in_ms + head, "开头"), (out_ms - tail, out_ms, "结尾")]


def auto_volume(src: Path, a_ms: int, b_ms: int, has_speech: bool, au: dict) -> tuple[float, float | None]:
    """按这一段的响度算增益：说话的段落对齐到 speech_lufs，环境声对齐到 ambient_lufs（少量提升）。"""
    lufs = measure_lufs(src, a_ms / 1000, (b_ms - a_ms) / 1000)
    if lufs is None:  # 静音或几乎没声音：不放大底噪
        return 0.0, None
    target = au["speech_lufs"] if has_speech else au["ambient_lufs"]
    hi = au["max_gain_db"] if has_speech else min(au["max_gain_db"], au["ambient_max_boost_db"])
    gain = max(-au["max_gain_db"], min(hi, target - lufs))
    return round(gain * 2) / 2, round(lufs, 1)


def clip_subtitles(segs: list[dict], a_ms: int, b_ms: int) -> list[dict]:
    """与 [a_ms, b_ms) 重叠的语音句子；时间是原视频里的毫秒，和 in_ms/out_ms 一致。"""
    return [{"start_ms": round(s["start"] * 1000), "end_ms": round(s["end"] * 1000),
             "text": s["text"], "translation": s.get("translation", "")}
            for s in segs if s["end"] * 1000 > a_ms and s["start"] * 1000 < b_ms]


def transition(kind: str, seconds: float) -> dict | None:
    return None if kind == "cut" else {"type": kind, "duration_ms": round(seconds * 1000)}


def build_project(ws: Workspace, st: dict) -> dict:
    media = ws.media()
    ordered = sorted(media.values(), key=lambda m: m["created"])
    groups = assign_groups(ordered, st["titles"]["group_km"])
    summaries = {}
    for m in ordered:
        if (s := load_json(ws.derived(m["id"]) / "summary.json")) is None:
            sys.exit("缺少摘要，请先运行 summarize")
        summaries[m["id"]] = s
    kept = [m for m in ordered if summaries[m["id"]]["keep"]]
    excluded = [{"media_id": m["id"], "name": m["name"], "reason": summaries[m["id"]]["reason"]}
                for m in ordered if not summaries[m["id"]]["keep"]]
    meta = group_titles(kept, groups, summaries)
    trs, ti, au = st["transitions"], st["titles"], st["audio"]
    normal_tr = transition(trs["type"], trs["duration_s"])
    group_tr = transition(trs["group_type"], trs["group_duration_s"])
    part_tr = transition(trs["part_type"], trs["part_duration_s"])

    items: list[dict] = []
    if ti["opening"] and kept:
        d0, d1 = local_date(kept[0]), local_date(kept[-1])
        items.append({"id": "t_open", "type": "title", "role": "opening", "text": st["name"],
                      "subtitle": f"{d0:%Y.%m.%d}" + (f" – {d1:%m.%d}" if d1 != d0 else ""),
                      "duration_ms": 3000, "background": "blur_next", "fade_in_ms": 0, "fade_out_ms": 0})
    last_g = last_day = last_title = None
    for m in kept:
        s, g = summaries[m["id"]], groups[m["id"]]
        gm = meta[g]
        new_group = g != last_g
        into = normal_tr
        if new_group:
            into = group_tr
            if ti["groups"] and (gm["date"] != last_day or gm["title"] != last_title):
                items.append({"id": f"t{len(items)}", "type": "title", "text": gm["title"],
                              "subtitle": gm["subtitle"], "duration_ms": 2000, "background": "blur_next",
                              "fade_in_ms": 0, "fade_out_ms": 0, "transition": group_tr})
                into = normal_tr
            elif last_g is not None and group_tr is None:  # 不用转场时，分组之间留一点黑场
                items.append({"id": f"g{len(items)}", "type": "gap", "duration_ms": 1000, "background": "black"})
        segs = load_json(ws.derived(m["id"]) / "transcript.json", {}).get("segments", [])
        proxy = ws.derived(m["id"]) / "proxy.mp4"
        parts = split_parts(s["in_ms"], s["out_ms"], st["long_clips"])
        for k, (a, b, label) in enumerate(parts):
            subs = clip_subtitles(segs, a, b)
            clip = {"id": f"c{len(items)}", "type": "clip", "media_id": m["id"], "name": m["name"],
                    "in_ms": a, "out_ms": b, "volume_db": 0, "fade_in_ms": 0, "fade_out_ms": 0}
            if label:
                clip["part"] = label
            tr = part_tr if k > 0 else into
            if items and tr:
                clip["transition"] = tr
            if au["auto_volume"]:
                gain, lufs = auto_volume(proxy, a, b, bool(subs), au)
                clip["volume_db"] = gain
                clip["auto"] = {"volume_db": gain, "measured_lufs": lufs, "speech": bool(subs)}
            if subs:
                clip["subtitles"] = subs
            clip["group"] = gm["title"]
            clip["ai"] = {"title": s["title"], "summary": s["summary"], "keywords": s["keywords"],
                          "reason": s["reason"], "place": s.get("place", ""), "source": s["source"]}
            # 规则兜底时的“标题”只是文件名，不适合放到画面上
            if ti["captions"] and k == 0 and s["source"] != "heuristic" and s["title"] and b - a > 1500:
                clip["caption"] = {"text": s["title"], "position": "bottom", "start_ms": 300, "end_ms": min(b - a, 3800)}
            items.append(clip)
        last_g, last_day, last_title = g, gm["date"], gm["title"]
    if items:  # 整片从黑场淡入、淡出到黑场
        items[0]["fade_in_ms"] = min(600, timeline_ms(items[:1]) // 3)
        items[-1]["fade_out_ms"] = min(800, timeline_ms(items[-1:]) // 3)
    out = st["output"]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_from": "settings.json",
        "project": {"name": st["name"], "resolution": out["resolution"], "fps": out["fps"],
                    "preview_resolution": out["preview_resolution"], "portrait_mode": out["portrait_mode"],
                    "loudness_lufs": au["final_lufs"],
                    "subtitles": {"burn_in": st["subtitles"]["burn_in"], "show": st["subtitles"]["show"]}},
        "tracks": [
            {"id": "main", "kind": "video", "items": items},
            {"id": "music", "kind": "audio", "items": []},
            {"id": "overlay", "kind": "overlay", "items": []},
        ],
        "excluded": excluded,
    }


def cmd_roughcut(ws: Workspace, args) -> None:
    if ws.project_file.exists() and not args.force:
        sys.exit(f"{ws.project_file} 已存在（可能有手动修改）。加 --force 覆盖。")
    st = load_settings(ws, args.name)
    # 命令行参数只对这一次生效，不写回 settings.json
    if args.group_km is not None:
        st["titles"]["group_km"] = args.group_km
    if args.titles is False:
        st["titles"]["opening"] = st["titles"]["groups"] = False
    if args.captions:
        st["titles"]["captions"] = True
    project = build_project(ws, st)
    save_json(ws.project_file, project)
    items, excluded = project["tracks"][0]["items"], project["excluded"]

    lines = [f"# {st['name']} · 初剪", "", "| # | 分组 | 片段 | 区间 | 音量 | 标题 | 摘要 |", "|---|---|---|---|---|---|---|"]
    log(f"\n初剪：{st['name']}")
    for it in items:
        tr = it.get("transition")
        arrow = f"   ⤳ {tr['type']} {tr['duration_ms']/1000:.1f}s" if tr else ""
        if arrow:
            log(arrow)
        if it["type"] == "gap":
            log(f"   ── 间隔 {it['duration_ms']/1000:.1f}s ──")
        elif it["type"] == "title":
            log(f"   ▣ 标题卡 {it['duration_ms']/1000:.1f}s：{it['text']}  {it['subtitle']}")
            lines.append(f"| {it['id']} | | 标题卡 | {it['duration_ms']/1000:.1f}s | | {it['text']} | {it['subtitle']} |")
        else:
            part = f"（{it['part']}）" if it.get("part") else ""
            vol = f"{it['volume_db']:+.1f}dB"
            subs = it.get("subtitles", [])
            log(f"  {it['id']:>3}  {it['group']}  {it['name']}{part:<6} {it['in_ms']/1000:5.1f}–{it['out_ms']/1000:5.1f}s  "
                f"音量 {vol:>7}  {it['ai']['title']}" + (f"  字幕 {len(subs)} 句" if subs else ""))
            lines.append(f"| {it['id']} | {it['group']} | {it['name']}{part} | "
                         f"{it['in_ms']/1000:.1f}–{it['out_ms']/1000:.1f}s | {vol} | {it['ai']['title']} | "
                         f"{it['ai']['summary']} |")
            for sub in subs:
                lines.append(f"| | | | | | 　{sub['text']} | {sub.get('translation', '')} |")
    for e in excluded:
        log(f"   ✕   {e['name']:<14} 已排除：{e['reason']}")
    lines += ["", "## 已排除", *[f"- {e['name']}：{e['reason']}" for e in excluded]]
    (ws.root / "roughcut.md").write_text("\n".join(lines) + "\n")
    log(f"项目 → {ws.project_file}（可直接编辑后再 render）")


def timeline_ms(items: list[dict]) -> int:
    """不计转场重叠的总时长。"""
    return sum(it["out_ms"] - it["in_ms"] if it["type"] == "clip" else it["duration_ms"] for it in items)


# ───────────────────────── render ─────────────────────────

def seg_key(*parts) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


FONT_CANDIDATES = [
    os.environ.get("VIDCUT_FONT"),
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
]
_fonts: dict = {}
TEXT_STYLE = 2  # 文字排版规则（换行、字号、位置）改了就加 1，让缓存的文字图片和片段重新生成


def load_font(size: int, bold: bool):
    """找一个能显示中文的字体；.ttc 里优先选简体中文的粗体/常规字重。"""
    from PIL import ImageFont
    if (size, bold) in _fonts:
        return _fonts[size, bold]
    want = ("Semibold", "Bold", "W6", "Medium") if bold else ("Regular", "W3", "Light")
    for path in filter(None, FONT_CANDIDATES):
        if not Path(path).exists():
            continue
        best = None
        for i in range(32):
            try:
                f = ImageFont.truetype(path, size, index=i)
            except OSError:
                break
            fam, style = f.getname()
            score = (any(k in fam for k in ("SC", "GB", "CJK")), style in want[:2], style in want)
            if best is None or score > best[0]:
                best = (score, i)
        if best:
            _fonts[size, bold] = ImageFont.truetype(path, size, index=best[1])
            return _fonts[size, bold]
    raise RuntimeError("找不到中文字体，请设置环境变量 VIDCUT_FONT 指向一个 .ttf/.ttc 文件")


def wrap_text(draw, text: str, font, max_w: float, max_lines: int = 3) -> list[str]:
    """按宽度换行：中文可以在任意字之间断开，英文只在单词之间断开。"""
    lines, cur = [], ""
    for tok in re.findall(r"\n|[A-Za-z0-9'’\-]+[.,!?;:]*|\s+|.", text.strip()):
        if tok == "\n":
            lines.append(cur.rstrip())
            cur = ""
        elif cur.strip() and draw.textlength(cur + tok, font=font) > max_w:
            lines.append(cur.rstrip())
            cur = tok.lstrip()
        else:
            cur += tok
    lines.append(cur.rstrip())
    return [ln for ln in lines if ln][:max_lines]


def text_png(kind: str, text: str, subtitle: str, position: str, W: int, H: int, out: Path,
             raise_bottom: bool = False) -> Path:
    """把文字画成全画幅透明 PNG，渲染时叠加到画面上（不依赖 ffmpeg 的 drawtext）。
    kind: title 标题卡 / caption 画面文字 / subtitle 语音字幕（text 原文，subtitle 译文）。"""
    if out.exists():
        return out
    from PIL import Image, ImageDraw, ImageFilter
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if kind == "title":
        f1, f2 = load_font(round(H * 0.085), True), load_font(round(H * 0.042), False)
        main = wrap_text(draw, text, f1, W * 0.8)
        sub = wrap_text(draw, subtitle, f2, W * 0.8, 2)
        lh1, lh2 = f1.size * 1.3, f2.size * 1.4
        spacing = H * 0.025 if main and sub else 0  # 主副标题之间留白
        y = H / 2 - (lh1 * len(main) + spacing + lh2 * len(sub)) / 2
        pos = []
        for ln in main:
            pos.append((ln, f1, y))
            y += lh1
        y += spacing
        for ln in sub:
            pos.append((ln, f2, y))
            y += lh2
        shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        for ln, f, yy in pos:
            sd.text((W / 2, yy + H * 0.004), ln, font=f, fill=(0, 0, 0, 200), anchor="mt")
        img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(H * 0.008)))
        draw = ImageDraw.Draw(img)
        for ln, f, yy in pos:
            fill = (255, 255, 255, 255) if f is f1 else (235, 235, 235, 255)
            draw.text((W / 2, yy), ln, font=f, fill=fill, anchor="mt")
    elif kind == "subtitle":
        # 底部两层：原文（白）+ 译文（浅黄），黑色描边保证任何画面上都看得清
        f1, f2 = load_font(round(H * 0.046), True), load_font(round(H * 0.037), False)
        l1 = wrap_text(draw, text, f1, W * 0.86, 2) if text else []
        l2 = wrap_text(draw, subtitle, f2, W * 0.86, 2) if subtitle else []
        lh1, lh2 = f1.size * 1.3, f2.size * 1.35
        y = H * 0.955 - (lh1 * len(l1) + lh2 * len(l2))
        for lines, f, lh, fill in ((l1, f1, lh1, (255, 255, 255, 255)), (l2, f2, lh2, (255, 228, 140, 255))):
            for ln in lines:
                draw.text((W / 2, y), ln, font=f, fill=fill, anchor="mt",
                          stroke_width=max(2, round(f.size * 0.09)), stroke_fill=(0, 0, 0, 235))
                y += lh
    else:
        f = load_font(round(H * 0.048), True)
        lines = wrap_text(draw, text, f, W * 0.8, 2)
        lh, pad = f.size * 1.35, f.size * 0.45
        box_w = max(draw.textlength(ln, font=f) for ln in lines) + pad * 2
        box_h = lh * len(lines) + pad * 1.2
        # 下方的画面文字在有语音字幕时上移，避免互相遮挡
        cy = {"top": H * 0.13, "center": H * 0.5}.get(position, H * (0.70 if raise_bottom else 0.85))
        x0, y0 = W / 2 - box_w / 2, cy - box_h / 2
        draw.rounded_rectangle((x0, y0, x0 + box_w, y0 + box_h), radius=f.size * 0.3, fill=(0, 0, 0, 130))
        for i, ln in enumerate(lines):
            draw.text((W / 2, y0 + pad * 0.6 + i * lh), ln, font=f, fill=(255, 255, 255, 255), anchor="mt")
    img.save(out)
    return out


def render_clip(it: dict, m: dict, src: Path, W: int, H: int, F: int, mode: str,
                hdr_chain: str | None, preset: str, crf: str, out: Path,
                caption_png: Path | None = None, subs: list[tuple[Path, float, float]] = ()) -> None:
    length = (it["out_ms"] - it["in_ms"]) / 1000
    fi, fo = it.get("fade_in_ms", 0) / 1000, it.get("fade_out_ms", 0) / 1000
    cmd = ["-ss", f"{it['in_ms']/1000:.3f}", "-t", f"{length:.3f}", "-i", str(src)]
    n_inputs = 1
    if not m["has_audio"]:
        cmd += ["-f", "lavfi", "-t", f"{length:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        n_inputs += 1
    still = ["-loop", "1", "-framerate", str(F), "-t", f"{length:.3f}", "-i"]
    if caption_png:
        cmd += [*still, str(caption_png)]
        cap_idx = n_inputs
        n_inputs += 1
    sub_idx = []
    for png, _, _ in subs:
        cmd += [*still, str(png)]
        sub_idx.append(n_inputs)
        n_inputs += 1
    pre = f"{hdr_chain}," if hdr_chain else ""
    same_aspect = abs(m["width"] / m["height"] - W / H) < 0.01
    if mode == "blur_fill" and not same_aspect:
        v = (f"[0:v]{pre}fps={F},split[a][b];"
             f"[a]scale={W//4}:{H//4}:force_original_aspect_ratio=increase,crop={W//4}:{H//4},"
             f"gblur=sigma=8,scale={W}:{H}[bg];"
             f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
             f"[bg][fg]overlay=(W-w)/2:(H-h)/2")
    else:
        v = (f"[0:v]{pre}fps={F},scale={W}:{H}:force_original_aspect_ratio=decrease,"
             f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2")
    v += ",setsar=1,format=yuv420p"
    if caption_png:
        c = it["caption"]
        s, e = c["start_ms"] / 1000, min(c["end_ms"] / 1000, length)
        fd = min(0.3, (e - s) / 3)
        v += (f"[base];[{cap_idx}:v]format=rgba,fade=t=in:st={s:.3f}:d={fd:.3f}:alpha=1,"
              f"fade=t=out:st={e - fd:.3f}:d={fd:.3f}:alpha=1[cap];"
              f"[base][cap]overlay=0:0:enable='between(t,{s:.3f},{e:.3f})',format=yuv420p")
    for j, ((_, s, e), k) in enumerate(zip(subs, sub_idx)):
        v += f"[sb{j}];[sb{j}][{k}:v]overlay=0:0:enable='between(t,{s:.3f},{e:.3f})'"
    if subs:
        v += ",format=yuv420p"
    if fi:
        v += f",fade=t=in:st=0:d={fi}"
    if fo:
        v += f",fade=t=out:st={max(0, length - fo):.3f}:d={fo}"
    a_in = "[0:a]" if m["has_audio"] else "[1:a]"
    a = f"{a_in}aresample=48000,aformat=channel_layouts=stereo,volume={it.get('volume_db', 0)}dB"
    if fi:
        a += f",afade=t=in:st=0:d={fi}"
    if fo:
        a += f",afade=t=out:st={max(0, length - fo):.3f}:d={fo}"
    cmd += ["-filter_complex", f"{v}[v];{a}[a]", "-map", "[v]", "-map", "[a]",
            "-t", f"{length:.3f}", "-r", str(F), "-c:v", "libx264", "-preset", preset,
            "-crf", crf, "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(out)]
    ffmpeg(*cmd)


def render_title(it: dict, bg: tuple[Path, float] | None, png: Path, W: int, H: int, F: int,
                 preset: str, crf: str, out: Path) -> None:
    d = it["duration_ms"] / 1000
    fi, fo = it.get("fade_in_ms", 0) / 1000, it.get("fade_out_ms", 0) / 1000
    frame = out.with_suffix(".bg.jpg")
    if bg:
        # 背景：下一段画面的一帧，放大铺满后模糊、压暗，让文字更清楚
        ffmpeg("-ss", f"{bg[1]:.3f}", "-i", str(bg[0]), "-frames:v", "1", "-q:v", "3", str(frame))
        cmd = ["-loop", "1", "-framerate", str(F), "-t", f"{d:.3f}", "-i", str(frame)]
        vbg = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
               f"gblur=sigma={max(4, W // 60)},eq=brightness=-0.15[bg]")
    else:
        cmd = ["-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={F}:d={d:.3f}"]
        vbg = "[0:v]null[bg]"
    cmd += ["-loop", "1", "-framerate", str(F), "-t", f"{d:.3f}", "-i", str(png),
            "-f", "lavfi", "-t", f"{d:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    v = f"{vbg};[bg][1:v]overlay=0:0,setsar=1,format=yuv420p"
    if fi:
        v += f",fade=t=in:st=0:d={fi}"
    if fo:
        v += f",fade=t=out:st={max(0, d - fo):.3f}:d={fo}"
    cmd += ["-filter_complex", f"{v}[v]", "-map", "[v]", "-map", "2:a", "-t", f"{d:.3f}",
            "-r", str(F), "-c:v", "libx264", "-preset", preset, "-crf", crf,
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(out)]
    ffmpeg(*cmd)
    frame.unlink(missing_ok=True)


def render_gap(it: dict, W: int, H: int, F: int, preset: str, out: Path) -> None:
    d = it["duration_ms"] / 1000
    ffmpeg("-f", "lavfi", "-i", f"color=c={it.get('background', 'black')}:s={W}x{H}:r={F}:d={d}",
           "-f", "lavfi", "-t", f"{d}", "-i", "anullsrc=r=48000:cl=stereo",
           "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", preset,
           "-c:a", "aac", "-b:a", "192k", "-t", f"{d}", str(out))


def srt_time(sec: float) -> str:
    ms = int(round(max(0.0, sec) * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def join_segments(segs: list[dict], out: Path, F: int, preset: str, crf: str) -> None:
    """按顺序拼接；有转场的地方用 xfade（画面）+ acrossfade（声音）重叠过渡。"""
    inputs, fc = [], []
    for i, sg in enumerate(segs):
        d = sg["dur"]
        inputs += ["-i", str(sg["path"])]
        # 统一时间基，音频按画面时长补齐/截断，多段拼接后不会音画漂移
        fc.append(f"[{i}:v]fps={F},settb=AVTB,setpts=PTS-STARTPTS,format=yuv420p[v{i}]")
        fc.append(f"[{i}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                  f"apad=whole_dur={d:.3f},atrim=0:{d:.3f},asetpts=PTS-STARTPTS[a{i}]")
    v, a, acc = "[v0]", "[a0]", segs[0]["dur"]
    for i in range(1, len(segs)):
        sg = segs[i]
        if sg["td"] > 0:
            td = sg["td"]
            fc.append(f"{v}[v{i}]xfade=transition={sg['transition']['type']}:duration={td:.3f}:offset={acc - td:.3f}[xv{i}]")
            fc.append(f"{a}[a{i}]acrossfade=d={td:.3f}:c1=tri:c2=tri[xa{i}]")
            acc += sg["dur"] - td
        else:
            fc.append(f"{v}{a}[v{i}][a{i}]concat=n=2:v=1:a=1[xv{i}][xa{i}]")
            acc += sg["dur"]
        v, a = f"[xv{i}]", f"[xa{i}]"
    script = out.with_suffix(".filter.txt")
    script.write_text(";\n".join(fc))
    # 从文件读滤镜图：ffmpeg 7+ 用 -/filter_complex，旧版本（如 Debian 的 5.1）用 -filter_complex_script
    opt = "-/filter_complex" if ffmpeg_major() >= 7 else "-filter_complex_script"
    ffmpeg(*inputs, opt, str(script), "-map", v, "-map", a, "-r", str(F),
           "-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(out))
    script.unlink(missing_ok=True)


def clip_subs_for_render(ws: Workspace, it: dict) -> list[dict]:
    """片段的字幕：优先用 project.json 里的（可能手改过），旧项目没有就从转录里取。"""
    if "subtitles" in it:
        return it["subtitles"]
    segs = load_json(ws.derived(it["media_id"]) / "transcript.json", {}).get("segments", [])
    return clip_subtitles(segs, it["in_ms"], it["out_ms"])


def cmd_render(ws: Workspace, args) -> None:
    project = load_json(ws.project_file)
    if project is None:
        sys.exit("缺少 project.json，请先运行 roughcut")
    if project.get("schema_version") != SCHEMA_VERSION:
        sys.exit(f"project.json 的 schema_version 不是 {SCHEMA_VERSION}")
    media = ws.media()
    cfg = project["project"]
    F = cfg["fps"]
    if args.draft:
        W, H = cfg.get("preview_resolution", [960, 540])
        preset, crf = "veryfast", "26"
    else:
        W, H = cfg["resolution"]
        preset, crf = "medium", "18"
    sub_cfg = cfg.get("subtitles", {"burn_in": False, "show": "both"})
    show = sub_cfg.get("show", "both")
    hdr_chain = hdr_to_sdr_chain()
    seg_dir = ws.root / "derived" / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)

    def source(m: dict) -> Path:
        src = ws.derived(m["id"]) / "proxy.mp4" if args.draft else Path(m["path"])
        if not src.exists():
            sys.exit(f"找不到素材: {src}")
        return src

    def sub_lines(sub: dict) -> tuple[str, str]:
        orig = sub.get("text", "").strip() if show in ("both", "original") else ""
        trans = sub.get("translation", "").strip() if show in ("both", "translation") else ""
        return orig, trans

    items = project["tracks"][0]["items"]
    segs: list[dict] = []
    for idx, it in enumerate(items):
        srt_rel: list[tuple[float, float, str]] = []
        if it["type"] == "gap":
            out = seg_dir / f"gap_{seg_key(it, W, H, F)}.mp4"
            if not out.exists():
                render_gap(it, W, H, F, preset, out)
        elif it["type"] == "title":
            bg = None
            if it.get("background") == "blur_next":
                nxt = next((x for x in items[idx + 1:] if x["type"] == "clip"), None)
                if nxt:
                    t = min(nxt["in_ms"] + 500, nxt["out_ms"] - 100) / 1000
                    bg = (source(media[nxt["media_id"]]), t)
            fields = {k: it.get(k) for k in ("text", "subtitle", "duration_ms", "fade_in_ms", "fade_out_ms")}
            key = seg_key(fields, bg and [str(bg[0]), bg[1]], W, H, F, args.draft, TEXT_STYLE)
            out = seg_dir / f"title_{key}.mp4"
            if out.exists():
                log(f"  {it['id']:>5} 标题卡           缓存命中")
            else:
                log(f"  {it['id']:>5} 标题卡           渲染「{it['text']}」")
                png = text_png("title", it["text"], it.get("subtitle", ""), "center", W, H,
                               seg_dir / f"text_{seg_key('title', it['text'], it.get('subtitle', ''), W, H, TEXT_STYLE)}.png")
                render_title(it, bg, png, W, H, F, preset, crf, out)
        elif it["type"] == "clip":
            m = media[it["media_id"]]
            src = source(m)
            if it["out_ms"] <= it["in_ms"]:
                sys.exit(f"{it['id']}: out_ms 必须大于 in_ms")
            length = (it["out_ms"] - it["in_ms"]) / 1000
            # 语音字幕：换算成相对这一段开头的时间
            burn = []
            for sub in clip_subs_for_render(ws, it):
                s = max(sub["start_ms"], it["in_ms"]) / 1000 - it["in_ms"] / 1000
                e = min(sub["end_ms"], it["out_ms"]) / 1000 - it["in_ms"] / 1000
                orig, trans = sub_lines(sub)
                if e - s < 0.2 or not (orig or trans):
                    continue
                srt_rel.append((s, e, "\n".join(x for x in (orig, trans) if x)))
                if sub_cfg.get("burn_in"):
                    burn.append((orig, trans, round(s, 3), round(min(e, length), 3)))
            cap = it.get("caption") if (it.get("caption") or {}).get("text", "").strip() else None
            render_fields = {k: it.get(k) for k in
                             ("media_id", "in_ms", "out_ms", "volume_db", "fade_in_ms", "fade_out_ms")}
            key = seg_key(render_fields, cap, burn, W, H, F, cfg["portrait_mode"], args.draft, str(src), TEXT_STYLE)
            out = seg_dir / f"clip_{key}.mp4"
            label = f"{m['name']}" + (f"（{it['part']}）" if it.get("part") else "")
            if out.exists():
                log(f"  {it['id']:>5} {label:<16} 缓存命中")
            else:
                log(f"  {it['id']:>5} {label:<16} 渲染 {it['in_ms']/1000:.1f}–{it['out_ms']/1000:.1f}s"
                    + (f"  音量 {it.get('volume_db', 0):+g}dB" if it.get("volume_db") else "")
                    + (f"  字幕 {len(burn)} 句" if burn else "") + (f"  文字「{cap['text']}」" if cap else ""))
                chain = hdr_chain if (m["hdr"] and not args.draft) else None
                png = None
                if cap:
                    pos = cap.get("position", "bottom")
                    png = text_png("caption", cap["text"], "", pos, W, H,
                                   seg_dir / f"text_{seg_key('caption', cap['text'], pos, bool(burn), W, H, TEXT_STYLE)}.png",
                                   raise_bottom=bool(burn))
                sub_pngs = [(text_png("subtitle", o, t, "bottom", W, H,
                                      seg_dir / f"sub_{seg_key('subtitle', o, t, W, H, TEXT_STYLE)}.png"), s, e)
                            for o, t, s, e in burn]
                render_clip(it, m, src, W, H, F, cfg["portrait_mode"], chain, preset, crf, out, png, sub_pngs)
        else:
            sys.exit(f"{it.get('id')}: 未知类型 {it['type']}")
        segs.append({"path": out, "dur": media_duration(out), "transition": it.get("transition") if idx else None,
                     "srt": srt_rel})

    # 转场时长不超过相邻两段的一半；算出每段在成片里的起点
    t = 0.0
    for i, sg in enumerate(segs):
        tr, td = sg["transition"], 0.0
        if i and tr and tr.get("type", "cut") != "cut":
            td = min(tr.get("duration_ms", 600) / 1000, segs[i - 1]["dur"] / 2, sg["dur"] / 2)
            td = td if td >= 0.05 else 0.0
        sg["td"] = td
        sg["start"] = t - td
        t = sg["start"] + sg["dur"]
    total = t

    ws.out.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w\-]+", "_", cfg["name"]).strip("_") or "output"
    stem += "_preview" if args.draft else ""
    joined = seg_dir / f"{stem}_joined.mp4"
    if any(sg["td"] for sg in segs):
        log(f"  拼接并生成 {sum(1 for sg in segs if sg['td'])} 处转场…")
        join_segments(segs, joined, F, preset, crf)
    else:
        log("  拼接…")
        concat_list = seg_dir / "concat.txt"
        concat_list.write_text("".join(f"file '{sg['path'].resolve()}'\n" for sg in segs))
        ffmpeg("-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(joined))

    final = ws.out / f"{stem}.mp4"
    target = cfg.get("loudness_lufs", -16)
    log(f"  响度标准化到 {target} LUFS（两遍 loudnorm）…")
    ln = f"loudnorm=I={target}:TP=-1.5:LRA=11"
    p1 = run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(joined), "-vn",
              "-af", ln + ":print_format=json", "-f", "null", "-"])
    meas = json.loads(p1.stderr[p1.stderr.rindex("{"):p1.stderr.rindex("}") + 1])
    ln2 = (f"{ln}:measured_I={meas['input_i']}:measured_TP={meas['input_tp']}:"
           f"measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}:"
           f"offset={meas['target_offset']}:linear=true")
    ffmpeg("-i", str(joined), "-c:v", "copy", "-af", ln2, "-ar", "48000",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(final))
    joined.unlink(missing_ok=True)

    srt = []
    for sg in segs:
        for s, e, text in sg["srt"]:
            srt.append(f"{len(srt) + 1}\n{srt_time(sg['start'] + s)} --> {srt_time(sg['start'] + e)}\n{text}\n")
    if srt:
        (ws.out / f"{stem}.srt").write_text("\n".join(srt))
    log(f"完成：{final}  （{total:.1f}s，{W}x{H}）" + (f"  字幕：{stem}.srt" if srt else ""))


# ───────────────────────── update / auto / all ─────────────────────────

def scan_sources(ws: Workspace, args, extra: str | None) -> set[str]:
    """重新扫描以前扫过的目录（以及可选的新目录）；返回扫描前已有的媒体 ID。"""
    data = load_json(ws.media_file) or {}
    # 旧版本的 media.json 没记录扫描目录，就用已有视频所在的目录
    folders = list(data.get("sources") or
                   sorted({str(Path(m["path"]).parent) for m in data.get("media", {}).values()}))
    if extra:
        folders.append(str(Path(extra).expanduser().resolve()))
    if not folders:
        sys.exit("还没有扫描过任何目录，请指定素材目录")
    for f in dict.fromkeys(folders):
        if not Path(f).is_dir():
            log(f"  跳过不存在的目录 {f}")
            continue
        args.folder = f
        try:
            cmd_scan(ws, args)
        except SystemExit as e:  # 某个目录暂时没有视频，不影响其他目录
            log(f"  {e}")
    return set(data.get("media", {}))


def analysis_steps(ws: Workspace, args) -> None:
    for step in (cmd_proxy, cmd_analyze, cmd_transcribe, cmd_translate, cmd_summarize):
        log(f"\n▶ {step.__name__.removeprefix('cmd_')}")
        step(ws, args)


def cmd_update(ws: Workspace, args) -> None:
    """加入新素材：重新扫描以前扫过的目录（以及可选的新目录），只处理新增视频。不改 project.json。"""
    before = scan_sources(ws, args, args.folder)
    analysis_steps(ws, args)
    new = [m["name"] for mid, m in ws.media().items() if mid not in before]
    log(f"\n新增 {len(new)} 个素材" + (f"：{'、'.join(new)}" if new else ""))
    if new:
        log("新素材不会自动放进时间线：在网页编辑器的“素材库”里点“加入”，或手动编辑 project.json")


def cmd_auto(ws: Workspace, args) -> None:
    """一条命令：素材目录 → 分析、转录、翻译、AI 初剪 → 预览视频（可选同时出 1080p 成片）。"""
    ws.root.mkdir(parents=True, exist_ok=True)
    st = load_settings(ws, args.name)
    lang = st["speech"]["language"]
    args.language = None if lang in (None, "", "auto") else lang
    args.model = args.model or st["speech"]["whisper_model"]
    args.force = False
    if args.folder or ws.media_file.exists():
        before = scan_sources(ws, args, args.folder)
    else:
        sys.exit("请指定素材目录：vidcut.py auto <目录>")
    analysis_steps(ws, args)
    new = [m["name"] for mid, m in ws.media().items() if mid not in before]

    log("\n▶ roughcut")
    if not ws.project_file.exists() or args.rebuild:
        args.force, args.group_km, args.titles, args.captions = True, None, None, None
        cmd_roughcut(ws, args)
        args.force = False
    else:
        log(f"保留已有的 {ws.project_file}（可能有手动修改；加 --rebuild 按 settings.json 重新生成）")
        sync_project_subtitles(ws)
        if new:
            log(f"新素材 {'、'.join(new)} 没有加入时间线：用 --rebuild 重新生成，或在网页编辑器里加入")

    log("\n▶ render 预览")
    args.draft = True
    cmd_render(ws, args)
    if args.final:
        log("\n▶ render 成片")
        args.draft = False
        cmd_render(ws, args)
    stem = re.sub(r"[^\w\-]+", "_", st["name"]).strip("_") or "output"
    log("\n全部完成：")
    log(f"  预览视频  {ws.out / (stem + '_preview.mp4')}")
    if args.final:
        log(f"  成片      {ws.out / (stem + '.mp4')}")
    log(f"  剪辑项目  {ws.project_file}   ← 改顺序、裁剪、音量、字幕、转场，再运行 render")
    log(f"  自动设置  {ws.root / 'settings.json'}   ← 改参数后运行 auto --rebuild")
    log(f"  语音文字稿 {ws.root / 'transcript.md'}")


def cmd_all(ws: Workspace, args) -> None:
    for step in (cmd_scan, cmd_proxy, cmd_analyze, cmd_transcribe, cmd_translate, cmd_summarize, cmd_roughcut):
        log(f"\n▶ {step.__name__.removeprefix('cmd_')}")
        step(ws, args)


def main() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            sys.exit(f"需要先安装 {tool}")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-w", "--work", default="work", help="工作目录（默认 ./work）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_)
        p.set_defaults(fn=fn)
        return p

    p = add("scan", cmd_scan, "扫描素材目录")
    p.add_argument("folder")
    for name, fn, h in [("proxy", cmd_proxy, "生成代理文件"), ("analyze", cmd_analyze, "检测静音/黑屏/场景")]:
        add(name, fn, h).add_argument("--force", action="store_true")
    p = add("transcribe", cmd_transcribe, "语音转录")
    p.add_argument("--force", action="store_true")
    p.add_argument("--language", default="auto", help="语言代码（zh、en…），默认 auto 自动检测")
    p.add_argument("--model", help="Whisper 模型名或 HF 仓库")
    add("transcript", cmd_transcript, "把所有语音整理成文字稿 transcript.md")
    p = add("translate", cmd_translate, "翻译语音（中文↔英文）")
    p.add_argument("--force", action="store_true")
    p.add_argument("--offline", action="store_true", help="不调用 Claude，只用离线翻译")
    p = add("summarize", cmd_summarize, "AI 摘要与初剪建议")
    p.add_argument("--force", action="store_true")
    p.add_argument("--offline", action="store_true", help="不调用 Claude，只用规则")

    def roughcut_args(p):
        p.add_argument("--name", help="片名（会写进 settings.json）")
        p.add_argument("--group-km", type=float, help="同组地点半径（公里），只对这一次生效")
        p.add_argument("--no-titles", dest="titles", action="store_false", default=None, help="这一次不加标题卡")
        p.add_argument("--captions", action="store_true", default=None, help="这一次把 AI 标题叠加到每段开头")

    p = add("roughcut", cmd_roughcut, "按 settings.json 生成 project.json")
    p.add_argument("--force", action="store_true")
    roughcut_args(p)
    p = add("render", cmd_render, "渲染成片")
    p.add_argument("--draft", action="store_true", help="用代理文件快速出预览视频（preview_resolution）")
    p = add("auto", cmd_auto, "全自动：素材目录 → 预览视频")
    p.add_argument("folder", nargs="?", help="素材目录；不填则重新扫描以前扫过的目录")
    p.add_argument("--name", help="片名（会写进 settings.json）")
    p.add_argument("--model", help="Whisper 模型")
    p.add_argument("--offline", action="store_true", help="不调用 Claude（摘要和翻译都用离线方式）")
    p.add_argument("--rebuild", action="store_true", help="按 settings.json 重新生成 project.json（会覆盖手动修改）")
    p.add_argument("--final", action="store_true", help="同时渲染 1080p 成片")
    p = add("all", cmd_all, "scan → roughcut 一条龙")
    p.add_argument("folder")
    p.add_argument("--force", action="store_true")
    p.add_argument("--language", default="auto", help="语言代码（zh、en…），默认 auto 自动检测")
    p.add_argument("--model")
    p.add_argument("--offline", action="store_true")
    roughcut_args(p)
    p = add("update", cmd_update, "加入新素材（不改 project.json）")
    p.add_argument("folder", nargs="?", help="新的素材目录；不填则重新扫描以前扫过的目录")
    p.add_argument("--language", default="auto", help="语言代码（zh、en…），默认 auto 自动检测")
    p.add_argument("--model")
    p.add_argument("--offline", action="store_true")

    args = ap.parse_args()
    if getattr(args, "language", None) in ("", "auto"):
        args.language = None
    args.force = getattr(args, "force", False)
    try:
        args.fn(Workspace(Path(args.work)), args)
    except RuntimeError as e:
        sys.exit(f"错误: {e}")


if __name__ == "__main__":
    main()
