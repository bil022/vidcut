#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["anthropic"]
# ///
"""vidcut —— AI 家庭/旅行视频初剪的命令行演示。

流水线（每一步结果都写进工作目录，可单独重跑）：
  scan       扫描素材目录 → media.json（内容哈希 ID、时长、帧率、旋转、HDR、拍摄时间、GPS）
  proxy      生成 720p 代理文件和缩略图（统一帧率、SDR、补静音音轨）
  analyze    在代理上检测静音、黑屏、场景切换、模糊度，抽关键帧
  transcribe 语音转录（mlx-whisper / faster-whisper，未安装则跳过）
  summarize  用 Claude 看关键帧 + 转录，生成摘要和初剪建议（无 API 时用规则兜底）
  roughcut   生成 project.json（按时间/地点分组排序），可手动编辑
  render     分段渲染 + 拼接 + 响度标准化 → MP4 和 SRT；--draft 用代理快速出低清草稿
  all        依次执行 scan → roughcut
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
    existing = (load_json(ws.media_file) or {}).get("media", {})
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
    save_json(ws.media_file, {"schema_version": SCHEMA_VERSION, "media": media})
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

def load_asr(model: str | None):
    try:
        import mlx_whisper  # Apple Silicon

        repo = model or "mlx-community/whisper-large-v3-turbo"

        def asr(wav: Path, language: str | None):
            r = mlx_whisper.transcribe(str(wav), path_or_hf_repo=repo, language=language, word_timestamps=True,
                                       initial_prompt="以下是普通话的句子。" if language == "zh" else None)
            return [(s["start"], s["end"], s["text"].strip()) for s in r["segments"]]
        return f"mlx-whisper ({repo})", asr
    except ImportError:
        pass
    try:
        from faster_whisper import WhisperModel

        size = model or "large-v3-turbo"
        fw = WhisperModel(size, device="auto", compute_type="int8")

        def asr(wav: Path, language: str | None):
            segs, _ = fw.transcribe(str(wav), language=language, vad_filter=True, word_timestamps=True,
                                    initial_prompt="以下是普通话的句子。" if language == "zh" else None)
            return [(s.start, s.end, s.text.strip()) for s in segs]
        return f"faster-whisper ({size})", asr
    except ImportError:
        return None, None


def cmd_transcribe(ws: Workspace, args) -> None:
    name, asr = load_asr(args.model)
    if asr is None:
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
            save_json(out, {"engine": name, "segments": []})
            log(f"  {m['name']:<16} 无有效语音")
            continue
        wav = d / "audio16k.wav"
        ffmpeg("-i", str(d / "proxy.mp4"), "-vn", "-ac", "1", "-ar", "16000", str(wav))
        segs = asr(wav, args.language)
        wav.unlink(missing_ok=True)
        # Whisper 在静音段容易“编”出文字：丢掉大部分落在静音里的句子
        keep = []
        for s, e, text in segs:
            overlap = sum(max(0, min(e, b) - max(s, a_)) for a_, b in a.get("silence", []))
            if text and overlap < (e - s) * 0.7:
                keep.append({"start": round(s, 2), "end": round(e, 2), "text": text})
        save_json(out, {"engine": name, "segments": keep})
        log(f"  {m['name']:<16} " + (" ".join(x["text"] for x in keep)[:60] or "（无）"))
    log("转录完成")


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
    },
    "required": ["title", "summary", "keywords", "keep", "in_ms", "out_ms", "reason"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """你是家庭与旅行视频的剪辑助理。你会拿到一段原始视频的元数据、几张关键帧、带时间戳的语音转录，以及自动检测到的静音/黑屏/模糊区段和一个基于规则的裁剪建议。

请输出：
- title / summary / keywords：描述这段视频拍了什么。画面内容以关键帧为准，语音内容以转录为准；不要编造关键帧和转录里都没有的细节。如果画面是测试图案或无法辨认，就如实说明。
- keep / in_ms / out_ms / reason：初剪建议。目标是去掉误触、镜头对着口袋或地面、首尾无意义的部分，同时保留有人说话和有画面内容的部分。规则建议通常是合理的起点，有充分理由时再调整。in_ms 和 out_ms 必须在 0 到视频时长之间，且 out_ms 大于 in_ms。"""


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
    client = None
    if not args.offline:
        try:
            import anthropic
            client = anthropic.Anthropic()
        except Exception as e:  # noqa: BLE001
            log(f"无法初始化 Claude 客户端（{e}），改用规则摘要")
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
                "keywords": [], **heur, "source": "heuristic",
            }
        save_json(out, result)
        flag = "保留" if result["keep"] else "丢弃"
        log(f"  {m['name']:<16} [{flag}] {result['in_ms']/1000:.1f}–{result['out_ms']/1000:.1f}s  "
            f"{result['title']}：{result['summary']}\n{'':20}理由：{result['reason']}")
    log("摘要与初剪建议完成")


# ───────────────────────── roughcut ─────────────────────────

def local_date(m: dict) -> dt.date:
    t = dt.datetime.fromisoformat(m["created"])
    if m["gps"]:
        # 用经度粗略估算当地时区，避免跨时区旅行时按 UTC 分错天
        t = t + dt.timedelta(hours=round(m["gps"]["lon"] / 15))
    return t.date()


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


def cmd_roughcut(ws: Workspace, args) -> None:
    if ws.project_file.exists() and not args.force:
        sys.exit(f"{ws.project_file} 已存在（可能有手动修改）。加 --force 覆盖。")
    media = ws.media()
    items, excluded, last_group = [], [], None
    gap_ms, fade_ms = args.gap_ms, args.fade_ms
    ordered = sorted(media.values(), key=lambda m: m["created"])
    groups = assign_groups(ordered, args.group_km)
    for m in ordered:
        s = load_json(ws.derived(m["id"]) / "summary.json")
        if s is None:
            sys.exit("缺少摘要，请先运行 summarize")
        if not s["keep"]:
            excluded.append({"media_id": m["id"], "name": m["name"], "reason": s["reason"]})
            continue
        g = groups[m["id"]]
        if last_group is not None and g != last_group:
            items[-1]["fade_out_ms"] = fade_ms
            items.append({"id": f"g{len(items)}", "type": "gap", "duration_ms": gap_ms,
                          "background": "black"})
        items.append({
            "id": f"c{len(items)}", "type": "clip", "media_id": m["id"], "name": m["name"],
            "in_ms": s["in_ms"], "out_ms": s["out_ms"], "volume_db": 0,
            "fade_in_ms": fade_ms if g != last_group else 0, "fade_out_ms": 0,
            "group": g,
            "ai": {"title": s["title"], "summary": s["summary"], "keywords": s["keywords"],
                   "reason": s["reason"], "source": s["source"]},
        })
        last_group = g
    if items:
        items[-1]["fade_out_ms"] = fade_ms
    project = {
        "schema_version": SCHEMA_VERSION,
        "project": {"name": args.name, "resolution": [1920, 1080], "fps": 30,
                    "portrait_mode": "blur_fill", "loudness_lufs": -16},
        "tracks": [
            {"id": "main", "kind": "video", "items": items},
            {"id": "music", "kind": "audio", "items": []},
            {"id": "overlay", "kind": "overlay", "items": []},
        ],
        "excluded": excluded,
    }
    save_json(ws.project_file, project)

    lines = [f"# {args.name} · 初剪", "", "| # | 分组 | 片段 | 区间 | 标题 | 摘要 |", "|---|---|---|---|---|---|"]
    total = 0
    log(f"\n初剪：{args.name}")
    for it in items:
        if it["type"] == "gap":
            log(f"   ── 间隔 {it['duration_ms']/1000:.1f}s ──")
            total += it["duration_ms"]
            continue
        n = it["out_ms"] - it["in_ms"]
        total += n
        log(f"  {it['id']:>3}  {it['group']}  {it['name']:<14} "
            f"{it['in_ms']/1000:5.1f}–{it['out_ms']/1000:5.1f}s  {it['ai']['title']}")
        lines.append(f"| {it['id']} | {it['group']} | {it['name']} | "
                     f"{it['in_ms']/1000:.1f}–{it['out_ms']/1000:.1f}s | {it['ai']['title']} | "
                     f"{it['ai']['summary']} |")
    for e in excluded:
        log(f"   ✕   {e['name']:<14} 已排除：{e['reason']}")
    lines += ["", "## 已排除", *[f"- {e['name']}：{e['reason']}" for e in excluded]]
    (ws.root / "roughcut.md").write_text("\n".join(lines) + "\n")
    log(f"总时长约 {total/1000:.1f}s → {ws.project_file}（可直接编辑后再 render）")


# ───────────────────────── render ─────────────────────────

def seg_key(*parts) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


def render_clip(it: dict, m: dict, src: Path, W: int, H: int, F: int, mode: str,
                hdr_chain: str | None, preset: str, crf: str, out: Path) -> None:
    length = (it["out_ms"] - it["in_ms"]) / 1000
    fi, fo = it.get("fade_in_ms", 0) / 1000, it.get("fade_out_ms", 0) / 1000
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
    cmd = ["-ss", f"{it['in_ms']/1000:.3f}", "-t", f"{length:.3f}", "-i", str(src)]
    if not m["has_audio"]:
        cmd += ["-f", "lavfi", "-t", f"{length:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    cmd += ["-filter_complex", f"{v}[v];{a}[a]", "-map", "[v]", "-map", "[a]",
            "-t", f"{length:.3f}", "-r", str(F), "-c:v", "libx264", "-preset", preset,
            "-crf", crf, "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(out)]
    ffmpeg(*cmd)


def render_gap(it: dict, W: int, H: int, F: int, preset: str, out: Path) -> None:
    d = it["duration_ms"] / 1000
    ffmpeg("-f", "lavfi", "-i", f"color=c={it.get('background', 'black')}:s={W}x{H}:r={F}:d={d}",
           "-f", "lavfi", "-t", f"{d}", "-i", "anullsrc=r=48000:cl=stereo",
           "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", preset,
           "-c:a", "aac", "-b:a", "192k", "-t", f"{d}", str(out))


def srt_time(sec: float) -> str:
    ms = int(round(sec * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def cmd_render(ws: Workspace, args) -> None:
    project = load_json(ws.project_file)
    if project is None:
        sys.exit("缺少 project.json，请先运行 roughcut")
    if project.get("schema_version") != SCHEMA_VERSION:
        sys.exit(f"project.json 的 schema_version 不是 {SCHEMA_VERSION}")
    media = ws.media()
    cfg = project["project"]
    W, H = cfg["resolution"]
    F = cfg["fps"]
    if args.draft:
        W, H, preset, crf = 640, 360, "ultrafast", "30"
    else:
        preset, crf = "medium", "20"
    hdr_chain = hdr_to_sdr_chain()
    seg_dir = ws.root / "derived" / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)

    items = project["tracks"][0]["items"]
    segments, srt, offset, n = [], [], 0.0, 0
    for it in items:
        if it["type"] == "gap":
            out = seg_dir / f"gap_{seg_key(it, W, H, F)}.mp4"
            if not out.exists():
                render_gap(it, W, H, F, preset, out)
            segments.append(out)
            offset += it["duration_ms"] / 1000
            continue
        m = media[it["media_id"]]
        src = ws.derived(m["id"]) / "proxy.mp4" if args.draft else Path(m["path"])
        if not src.exists():
            sys.exit(f"找不到素材: {src}")
        if it["out_ms"] <= it["in_ms"]:
            sys.exit(f"{it['id']}: out_ms 必须大于 in_ms")
        render_fields = {k: it.get(k) for k in
                         ("media_id", "in_ms", "out_ms", "volume_db", "fade_in_ms", "fade_out_ms")}
        key = seg_key(render_fields, W, H, F, cfg["portrait_mode"], args.draft, str(src))
        out = seg_dir / f"clip_{key}.mp4"
        if out.exists():
            log(f"  {it['id']:>3} {m['name']:<16} 缓存命中")
        else:
            log(f"  {it['id']:>3} {m['name']:<16} 渲染 {it['in_ms']/1000:.1f}–{it['out_ms']/1000:.1f}s")
            chain = hdr_chain if (m["hdr"] and not args.draft) else None
            render_clip(it, m, src, W, H, F, cfg["portrait_mode"], chain, preset, crf, out)
        segments.append(out)
        # 字幕：把转录时间映射到成片时间线
        t_in, t_out = it["in_ms"] / 1000, it["out_ms"] / 1000
        tr = load_json(ws.derived(m["id"]) / "transcript.json", {"segments": []})
        for s in tr["segments"]:
            a, b = max(s["start"], t_in), min(s["end"], t_out)
            if b - a > 0.3:
                n += 1
                srt.append(f"{n}\n{srt_time(offset + a - t_in)} --> "
                           f"{srt_time(offset + b - t_in)}\n{s['text']}\n")
        offset += t_out - t_in

    ws.out.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w\-]+", "_", cfg["name"]).strip("_") or "output"
    stem += "_draft" if args.draft else ""
    concat_list = seg_dir / "concat.txt"
    concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in segments))
    joined = seg_dir / f"{stem}_joined.mp4"
    log("  拼接…")
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
    if srt:
        (ws.out / f"{stem}.srt").write_text("\n".join(srt))
    log(f"完成：{final}  （{offset:.1f}s，{W}x{H}）" + (f"  字幕：{stem}.srt" if srt else ""))


# ───────────────────────── all / CLI ─────────────────────────

def cmd_all(ws: Workspace, args) -> None:
    for step in (cmd_scan, cmd_proxy, cmd_analyze, cmd_transcribe, cmd_summarize, cmd_roughcut):
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
    p.add_argument("--language", default="zh", help="语言代码，传空字符串则自动检测")
    p.add_argument("--model", help="Whisper 模型名或 HF 仓库")
    p = add("summarize", cmd_summarize, "AI 摘要与初剪建议")
    p.add_argument("--force", action="store_true")
    p.add_argument("--offline", action="store_true", help="不调用 Claude，只用规则")
    p = add("roughcut", cmd_roughcut, "生成 project.json")
    p.add_argument("--force", action="store_true")
    p.add_argument("--name", default="My Trip")
    p.add_argument("--gap-ms", type=int, default=1000)
    p.add_argument("--fade-ms", type=int, default=400)
    p.add_argument("--group-km", type=float, default=30, help="同组地点半径（公里）")
    p = add("render", cmd_render, "渲染成片")
    p.add_argument("--draft", action="store_true", help="用代理文件快速出 640x360 草稿")
    p = add("all", cmd_all, "scan → roughcut 一条龙")
    p.add_argument("folder")
    p.add_argument("--force", action="store_true")
    p.add_argument("--language", default="zh")
    p.add_argument("--model")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--name", default="My Trip")
    p.add_argument("--gap-ms", type=int, default=1000)
    p.add_argument("--fade-ms", type=int, default=400)
    p.add_argument("--group-km", type=float, default=30, help="同组地点半径（公里）")

    args = ap.parse_args()
    if getattr(args, "language", None) == "":
        args.language = None
    try:
        args.fn(Workspace(Path(args.work)), args)
    except RuntimeError as e:
        sys.exit(f"错误: {e}")


if __name__ == "__main__":
    main()
